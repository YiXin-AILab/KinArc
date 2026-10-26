"""Repeat Tool Reminder 内置 Plugin 测试。"""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import agentsystem
import pytest
from agentsystem.common.models import JsonValue, OutputMode, SessionContext
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    is_model_visible,
    is_public_visible,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
    TurnMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.models import PluginContribution
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.runtime.state import AgentState, SessionStatus

_NOW = datetime(2026, 8, 31, tzinfo=UTC)
_SESSION = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def _load_repeat_plugin(
    options: Mapping[str, Mapping[str, JsonValue]],
) -> PluginContribution:
    services = PluginSetupServices()
    services.freeze()
    package_root = Path(agentsystem.__file__).parent
    source = next(
        item
        for item in discover_builtin_plugins(package_root)
        if item.plugin_id == "builtin.runtime.repeat-tool-reminder"
    )
    return load_plugin_contributions(
        (source,),
        agent_id="agent",
        options=options,
        services=services,
    )[0]


def _call(
    call_id: str,
    *,
    tool_name: str = "search",
    arguments: Mapping[str, JsonValue] | None = None,
    raw_arguments: str | None = None,
) -> ToolCallBlock:
    resolved = (
        {"query": "value"} if arguments is None and raw_arguments is None else arguments
    )
    raw = raw_arguments if raw_arguments is not None else '{"query":"value"}'
    return ToolCallBlock(
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments=resolved,
        raw_arguments=raw,
    )


def _assistant(
    message_id: str,
    *blocks: TextBlock | ToolCallBlock,
    run_id: str = "run",
) -> Message:
    return Message(
        message_id=message_id,
        session_id="session",
        role=Role.ASSISTANT,
        content=blocks,
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.ALL,
        model="model",
        run_id=run_id,
        created_at=_NOW,
    )


def _service_message(message_id: str) -> Message:
    return Message(
        message_id=message_id,
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="existing reminder"),),
        origin="repeat-tool-reminder",
        visibility=MessageVisibility.MODEL,
        run_id="run",
        created_at=_NOW,
    )


def _tool_message(message_id: str, call_id: str) -> Message:
    return Message(
        message_id=message_id,
        session_id="session",
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id=call_id,
                tool_name="search",
                content="result",
            ),
        ),
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=_NOW,
    )


def _context(
    messages: tuple[Message, ...],
    *,
    turn_index: int = 4,
) -> MiddlewareContext:
    state = AgentState(
        session_context=_SESSION,
        status=SessionStatus.RUNNING,
        run_id="run",
        output_mode=OutputMode.COMPLETE,
        turn_index=turn_index,
        started_at=_NOW,
        active_started_at=_NOW,
    )
    return MiddlewareContext(
        invocation_id="dispatch",
        point=MiddlewarePoint.BEFORE_TURN,
        ctx=_SESSION,
        run_id="run",
        trace_id="trace",
        turn_index=turn_index,
        state_snapshot=state,
        payload=TurnMiddlewarePayload(messages=messages),
    )


def _manager(contribution: PluginContribution) -> MiddlewareManager:
    registry = MiddlewareRegistry()
    registry.register_many(contribution.middlewares)
    registry.freeze()
    return MiddlewareManager(registry)


async def _dispatch(
    messages: tuple[Message, ...],
    options: Mapping[str, Mapping[str, JsonValue]] | None = None,
    *,
    turn_index: int = 4,
) -> MiddlewareDecision:
    contribution = _load_repeat_plugin(options or {})
    return await _manager(contribution).dispatch(
        _context(messages, turn_index=turn_index)
    )


def test_repeat_plugin_registers_default_and_can_be_disabled() -> None:
    """默认注册 FAIL_OPEN BEFORE_TURN，null 时完全关闭。"""
    contribution = _load_repeat_plugin({})

    assert len(contribution.middlewares) == 1
    registration = contribution.middlewares[0]
    assert registration.point is MiddlewarePoint.BEFORE_TURN
    assert registration.failure_mode is MiddlewareFailureMode.FAIL_OPEN
    assert (
        _load_repeat_plugin({"repeat-tool-reminder": {"remind_at": None}}).middlewares
        == ()
    )


@pytest.mark.parametrize(
    "options",
    (
        {"repeat-tool-reminder": {"remind_at": 1}},
        {"repeat-tool-reminder": {"thresholds": [3, 5]}},
    ),
)
def test_repeat_plugin_rejects_invalid_options(
    options: Mapping[str, Mapping[str, JsonValue]],
) -> None:
    """单阈值边界和未知配置在装配期失败。"""
    with pytest.raises(ValueError):
        _load_repeat_plugin(options)


@pytest.mark.parametrize(
    ("count", "expected"),
    (
        (2, MiddlewareDecisionType.CONTINUE),
        (3, MiddlewareDecisionType.MESSAGES),
        (4, MiddlewareDecisionType.CONTINUE),
    ),
)
async def test_repeat_plugin_only_reminds_at_exact_default_threshold(
    count: int,
    expected: MiddlewareDecisionType,
) -> None:
    """默认只在连续第 3 次命中一次。"""
    messages = tuple(
        _assistant(f"assistant-{index}", _call(f"call-{index}"))
        for index in range(count)
    )

    decision = await _dispatch(messages)

    assert decision.type is expected
    if expected is MiddlewareDecisionType.MESSAGES:
        assert isinstance(decision.payload, MessagesMiddlewarePayload)
        message = decision.payload.messages[0]
        assert message.role is Role.USER
        assert message.origin == "repeat-tool-reminder"
        assert message.visibility is MessageVisibility.MODEL
        assert is_model_visible(message)
        assert not is_public_visible(message)
        assert message.run_id == "run"
        block = message.content[0]
        assert isinstance(block, TextBlock)
        assert block.text.startswith("<system-reminder>\n你已连续 3 次")
        assert block.text.endswith("\n</system-reminder>")


async def test_repeat_plugin_honors_custom_threshold() -> None:
    """每 Agent 自定义阈值替代默认值。"""
    messages = tuple(
        _assistant(f"assistant-{index}", _call(f"call-{index}")) for index in range(5)
    )

    decision = await _dispatch(
        messages,
        {"repeat-tool-reminder": {"remind_at": 5}},
    )

    assert decision.type is MiddlewareDecisionType.MESSAGES


async def test_repeat_plugin_uses_current_run_and_ignores_non_calls() -> None:
    """其他 run、ToolResult、USER 和普通文本不改变当前 run 的调用序列。"""
    messages = (
        _assistant("foreign", _call("foreign-call"), run_id="other-run"),
        _assistant("assistant-1", _call("call-1")),
        _tool_message("tool-1", "call-1"),
        _service_message("previous-reminder"),
        _assistant("assistant-2", TextBlock(text="thinking"), _call("call-2")),
        _assistant("assistant-3", _call("call-3")),
    )

    decision = await _dispatch(messages)

    assert decision.type is MiddlewareDecisionType.MESSAGES


@pytest.mark.parametrize(
    "calls",
    (
        (
            _call("one", tool_name="search"),
            _call("two", tool_name="lookup"),
            _call("three", tool_name="lookup"),
        ),
        (
            _call("one", arguments={"items": [1, 2]}),
            _call("two", arguments={"items": [2, 1]}),
            _call("three", arguments={"items": [2, 1]}),
        ),
        (
            _call("one", arguments={"value": 1}),
            _call("two", arguments={"value": 2}),
            _call("three", arguments={"value": 2}),
        ),
    ),
)
async def test_repeat_plugin_stops_at_different_suffix_signature(
    calls: tuple[ToolCallBlock, ...],
) -> None:
    """工具名、数组顺序或值变化都会截断连续 suffix。"""
    decision = await _dispatch((_assistant("parallel", *calls),))

    assert decision.type is MiddlewareDecisionType.CONTINUE


async def test_repeat_plugin_canonicalizes_nested_object_keys() -> None:
    """深层对象 key 顺序不影响相同调用签名。"""
    messages = (
        _assistant(
            "one",
            _call("one", arguments={"outer": {"a": 1, "b": 2}}),
        ),
        _assistant(
            "two",
            _call("two", arguments={"outer": {"b": 2, "a": 1}}),
        ),
        _assistant(
            "three",
            _call("three", arguments={"outer": {"a": 1, "b": 2}}),
        ),
    )

    decision = await _dispatch(messages)

    assert decision.type is MiddlewareDecisionType.MESSAGES


async def test_repeat_plugin_uses_exact_raw_arguments_after_parse_failure() -> None:
    """无解析参数时完整 raw_arguments 精确参与比较。"""
    matching = tuple(
        _assistant(
            f"assistant-{index}",
            _call(
                f"call-{index}",
                arguments=None,
                raw_arguments='{"query":',
            ),
        )
        for index in range(3)
    )
    different = matching[:-1] + (
        _assistant(
            "assistant-different",
            _call("call-different", arguments=None, raw_arguments='{"query": '),
        ),
    )

    assert (await _dispatch(matching)).type is MiddlewareDecisionType.MESSAGES
    assert (await _dispatch(different)).type is MiddlewareDecisionType.CONTINUE


async def test_repeat_plugin_message_id_is_deterministic_and_hides_arguments() -> None:
    """同一次调用稳定幂等，且提醒不重复大参数内容。"""
    secret = "large-private-argument"
    messages = tuple(
        _assistant(
            f"assistant-{index}",
            _call(f"call-{index}", arguments={"query": secret}),
        )
        for index in range(3)
    )
    manager = _manager(_load_repeat_plugin({}))
    context = _context(messages)

    first = await manager.dispatch(context)
    second = await manager.dispatch(context)

    assert isinstance(first.payload, MessagesMiddlewarePayload)
    assert isinstance(second.payload, MessagesMiddlewarePayload)
    assert first.payload.messages[0].message_id == second.payload.messages[0].message_id
    block = first.payload.messages[0].content[0]
    assert isinstance(block, TextBlock)
    assert secret not in block.text
