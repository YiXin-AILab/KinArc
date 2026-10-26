"""Memory 工具 Schema 与真实调用上下文。"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.config.models import AgentMemoryConfig
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.plugins.memory_tools import register
from agentsystem.memory.service import MemoryService
from agentsystem.message.models import ToolCallBlock
from agentsystem.plugin.assembler import assemble_agent_capabilities
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.context import ToolInvocationContext, _bind_tool_context
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.registry import ToolRegistry
from pydantic import ValidationError


async def test_adapter_failure_logs_stack_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_content = "private stored content"

    async def fail(**kwargs: object) -> str:
        try:
            raise TypeError(private_content)
        except TypeError as cause:
            raise AdapterError(operation="memory.read") from cause

    store = LocalMemoryStore()
    monkeypatch.setattr(store, "read", AsyncMock(side_effect=fail))
    executor = ToolExecutor(registry(MemoryService(store, config=AgentMemoryConfig())))
    context = ToolInvocationContext(
        session_context=SessionContext(
            app_id="a", user_id="u", agent_id="g", session_id="s"
        ),
        run_id="r",
        runtime_trace_id="t",
        tool_call_id="c",
        tool_name="memory_read",
        turn_index=0,
    )
    with caplog.at_level(logging.ERROR):
        await executor.execute(
            context,
            ToolCallBlock(
                tool_call_id="c",
                tool_name="memory_read",
                raw_arguments='{"scope":"user_global"}',
                arguments={"scope": "user_global"},
            ),
        )
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.exc_info is not None
    assert record.exc_info[1] is not None
    assert record.exc_info[1].__cause__ is not None
    assert "private stored content" not in caplog.text


def registry(service: MemoryService | None) -> ToolRegistry:
    """装配真正的注册入口。"""
    return assemble_agent_capabilities(
        agent_id="g",
        sources=(
            PluginSource(
                plugin_id="builtin.memory",
                origin=PluginOrigin.BUILTIN,
                path=Path(__file__),
                register=register,
            ),
        ),
        plugin_options={},
        context_provider_types=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1,
        max_content_chars=1000,
        plugin_capabilities=((MemoryService, service),) if service else (),
    ).tool_registry


def test_every_tool_parameter_explains_its_semantics() -> None:
    tools = registry(MemoryService(LocalMemoryStore(), config=AgentMemoryConfig()))
    assert tools.rules() == ()
    for name in tools.names():
        definition = tools.get(name)
        assert definition is not None and definition.description
        properties = definition.parameters["properties"]
        assert isinstance(properties, dict)
        for schema in properties.values():
            assert isinstance(schema, dict) and schema.get("description")


async def test_tools_bind_identity_and_delete() -> None:
    tools = registry(MemoryService(LocalMemoryStore(), config=AgentMemoryConfig()))
    assert set(tools.names()) == {"memory_read", "memory_add", "memory_update"}
    ctx = ToolInvocationContext(
        session_context=SessionContext(
            app_id="a", user_id="u", agent_id="g", session_id="s"
        ),
        run_id="r",
        runtime_trace_id="t",
        tool_call_id="c",
        tool_name="memory_add",
        turn_index=0,
    )
    with _bind_tool_context(ctx):
        for name, args in [
            ("memory_add", {"scope": "user_global", "new_content": "abc"}),
            (
                "memory_update",
                {"scope": "user_global", "old_content": "abc", "new_content": ""},
            ),
            ("memory_read", {"scope": "user_global"}),
        ]:
            definition = tools.get(name)
            assert definition is not None and definition.invocation_model is not None
            parsed = definition.invocation_model.model_validate(args)
            output = await definition.handler.invoke(ctx=ctx, arguments=dict(parsed))
            payload = json.loads(output.content)
            assert payload["scope"] == "user_global"
            if name != "memory_read":
                assert "剩余" in payload["notice"]
                assert "截断" not in payload["notice"]
                assert "would_truncate" not in payload
        assert payload == {"scope": "user_global", "content": ""}
    update = tools.get("memory_update")
    assert update is not None and update.invocation_model is not None
    for extra in ({"replace_all": "false"}, {"user_id": "victim"}):
        with pytest.raises(ValidationError):
            update.invocation_model.model_validate(
                {"scope": "user_global", "old_content": "a", "new_content": "", **extra}
            )
    assert registry(None).names() == ()
    assert (
        registry(
            MemoryService(LocalMemoryStore(), config=AgentMemoryConfig(scopes=()))
        ).names()
        == ()
    )
