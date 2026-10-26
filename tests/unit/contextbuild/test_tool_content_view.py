"""tool_content_view Builtin Tool 查询协议测试。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import agentsystem
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    ToolCallBlock,
    ToolResultBlock,
)
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.tool.context import ToolInvocationContext
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolDefinition, ToolSource, ToolSuspension
from agentsystem.tool.registry import ToolRegistry
from tests.unit.runtime.port_stubs import MessageManagerStub

CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


class _Messages(MessageManagerStub):
    """返回指定历史或抛出指定 AdapterError。"""

    def __init__(
        self,
        messages: tuple[Message, ...] = (),
        *,
        error: AdapterError | None = None,
    ) -> None:
        self._messages = messages
        self._error = error

    async def list_messages(
        self,
        ctx: SessionContext,
    ) -> tuple[Message, ...]:
        assert ctx == CONTEXT
        if self._error is not None:
            raise self._error
        return self._messages


def _tool_content_view_plugin(
    manager: MessageManagerStub | None,
) -> tuple[ToolDefinition, ...]:
    source = next(
        source
        for source in discover_builtin_plugins(Path(agentsystem.__file__).parent)
        if source.plugin_id == "builtin.context.tool-content-view-tool"
    )
    services = PluginSetupServices()
    if manager is not None:
        services.register(MessageManager, cast(Any, manager))
    services.freeze()
    return load_plugin_contributions(
        (source,),
        agent_id="agent",
        options={},
        services=services,
    )[0].tools


def _assistant(message_id: str, raw_arguments: str) -> Message:
    return Message(
        message_id=message_id,
        session_id=CONTEXT.session_id,
        role=Role.ASSISTANT,
        content=(
            ToolCallBlock(
                tool_call_id="call-1",
                tool_name="web_search",
                arguments={"query": raw_arguments},
                raw_arguments=raw_arguments,
            ),
        ),
        origin=MessageOrigin.MODEL,
        run_id="run-1",
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
        visibility=MessageVisibility.ALL,
    )


def _tool(message_id: str, content: str) -> Message:
    return Message(
        message_id=message_id,
        session_id=CONTEXT.session_id,
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id="call-1",
                tool_name="web_search",
                content=content,
            ),
        ),
        origin=MessageOrigin.TOOL,
        run_id="run-1",
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
        visibility=MessageVisibility.ALL,
    )


async def _execute(
    definition: ToolDefinition,
    arguments: dict[str, object],
) -> tuple[ToolResultBlock, dict[str, Any]]:
    registry = ToolRegistry()
    registry.register(definition)
    registry.freeze()
    call = ToolCallBlock(
        tool_call_id="view-call",
        tool_name=definition.name,
        arguments=cast(dict[str, Any], arguments),
        raw_arguments=json.dumps(arguments),
    )
    invocation = ToolInvocationContext(
        session_context=CONTEXT,
        run_id="run-2",
        runtime_trace_id="trace-2",
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        turn_index=2,
    )

    result = await ToolExecutor(registry).execute(invocation, call)

    assert not isinstance(result, ToolSuspension)
    payload = (
        {}
        if result.block.is_error
        else cast(dict[str, Any], json.loads(result.block.content))
    )
    return result.block, payload


def test_tool_content_view_registers_only_with_message_manager() -> None:
    """缺少 MessageManager 时不注册，存在时暴露稳定模型协议。"""
    assert _tool_content_view_plugin(None) == ()

    tools = _tool_content_view_plugin(MessageManagerStub())

    assert len(tools) == 1
    definition = tools[0]
    assert definition.name == "tool_content_view"
    assert definition.source is ToolSource.BUILTIN
    assert definition.parameters["required"] == ["tool_call_id"]
    assert definition.parameters["additionalProperties"] is False
    properties = cast(dict[str, dict[str, Any]], definition.parameters["properties"])
    assert properties["offset"]["minimum"] == 0
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 8_000


async def test_tool_content_view_returns_latest_original_content_and_pages() -> None:
    """同 ID 取稳定顺序最新项，并按 Unicode 字符分页原始内容。"""
    definition = _tool_content_view_plugin(
        _Messages(
            (
                _assistant("assistant-old", '{"query":"old"}'),
                _tool("tool-old", "旧结果"),
                _assistant("assistant-new", '{"query":"原始参数"}'),
                _tool("tool-new", "甲乙丙丁戊"),
            )
        )
    )[0]

    _, arguments = await _execute(
        definition,
        {"tool_call_id": "call-1", "part": "arguments"},
    )
    _, page = await _execute(
        definition,
        {
            "tool_call_id": "call-1",
            "part": "result",
            "offset": 1,
            "limit": 2,
        },
    )

    assert arguments["content"] == '{"query":"原始参数"}'
    assert arguments["source"] == {"tool_call_id": "call-1", "part": "arguments"}
    assert list(arguments) == ["source", "range", "content"]
    assert list(page) == ["source", "range", "content"]
    assert page == {
        "content": "乙丙",
        "source": {"tool_call_id": "call-1", "part": "result"},
        "range": {"offset": 1, "end_offset": 3, "total_chars": 5, "next_offset": 3},
    }
    _, last_page = await _execute(
        definition,
        {"tool_call_id": page["source"]["tool_call_id"], "offset": 3, "limit": 2},
    )
    assert last_page["content"] == "丁戊"
    assert last_page["range"] == {
        "offset": 3,
        "end_offset": 5,
        "total_chars": 5,
        "next_offset": None,
    }


async def test_tool_content_view_returns_empty_tail_and_expected_miss() -> None:
    """越过末尾为空成功页，未知调用返回稳定 ToolError。"""
    definition = _tool_content_view_plugin(_Messages((_tool("tool", "甲乙"),)))[0]

    tail_block, tail = await _execute(
        definition,
        {
            "tool_call_id": "call-1",
            "part": "result",
            "offset": 10,
        },
    )
    missing_block, _ = await _execute(
        definition,
        {"tool_call_id": "missing", "part": "result"},
    )
    missing_part_block, _ = await _execute(
        definition,
        {"tool_call_id": "call-1", "part": "arguments"},
    )

    assert tail_block.is_error is False
    assert tail["content"] == ""
    assert tail["range"]["next_offset"] is None
    assert missing_block.is_error is True
    assert missing_block.error_code == "tool_content_not_found"
    assert missing_part_block.is_error is True
    assert missing_part_block.error_code == "tool_content_not_found"


async def test_tool_content_view_rejects_its_own_call_content() -> None:
    """恢复工具自身的结果不能成为嵌套读取目标。"""
    stored = _tool("view-result", "恢复出来的一页")
    block = cast(ToolResultBlock, stored.content[0]).model_copy(
        update={"tool_name": "tool_content_view"}
    )
    definition = _tool_content_view_plugin(
        _Messages((stored.model_copy(update={"content": (block,)}),))
    )[0]

    result, _ = await _execute(definition, {"tool_call_id": "call-1"})

    assert result.is_error is True
    assert result.error_code == "tool_content_recursive_read"


async def test_tool_content_view_maps_storage_failure_to_tool_error() -> None:
    """MessageManager 失败只形成现有 tool_error，不泄漏 Adapter 详情。"""
    definition = _tool_content_view_plugin(
        _Messages(
            error=AdapterError(
                operation="message.list",
                message="storage failed",
            )
        )
    )[0]

    block, _ = await _execute(definition, {"tool_call_id": "call-1"})

    assert block.is_error is True
    assert block.error_code == "tool_history_unavailable"
