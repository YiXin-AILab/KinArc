"""Turn Reminder 内置 Plugin 测试。"""

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


def _load_plugins(
    plugin_ids: tuple[str, ...],
    options: Mapping[str, Mapping[str, JsonValue]],
) -> tuple[PluginContribution, ...]:
    services = PluginSetupServices()
    services.freeze()
    sources = tuple(
        source
        for source in discover_builtin_plugins(Path(agentsystem.__file__).parent)
        if source.plugin_id in plugin_ids
    )
    assert tuple(source.plugin_id for source in sources) == plugin_ids
    return load_plugin_contributions(
        sources,
        agent_id="agent",
        options=options,
        services=services,
    )


def _turn_contribution(
    options: Mapping[str, Mapping[str, JsonValue]] | None = None,
) -> PluginContribution:
    return _load_plugins(("builtin.runtime.turn-reminder",), options or {})[0]


def _context(
    turn_index: int | None,
    messages: tuple[Message, ...] = (),
) -> MiddlewareContext:
    state = AgentState(
        session_context=_SESSION,
        status=SessionStatus.RUNNING,
        run_id="run",
        output_mode=OutputMode.COMPLETE,
        turn_index=turn_index or 0,
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


def _manager(contributions: tuple[PluginContribution, ...]) -> MiddlewareManager:
    registry = MiddlewareRegistry()
    for contribution in contributions:
        registry.register_many(contribution.middlewares)
    registry.freeze()
    return MiddlewareManager(registry)


async def _dispatch(
    turn_index: int | None,
    options: Mapping[str, Mapping[str, JsonValue]] | None = None,
) -> MiddlewareDecision:
    manager = _manager((_turn_contribution(options),))
    return await manager.dispatch(_context(turn_index))


def test_turn_plugin_registers_default_and_can_be_disabled() -> None:
    """默认注册 FAIL_OPEN BEFORE_TURN，null 时完全关闭。"""
    contribution = _turn_contribution()

    assert len(contribution.middlewares) == 1
    registration = contribution.middlewares[0]
    assert registration.point is MiddlewarePoint.BEFORE_TURN
    assert registration.failure_mode is MiddlewareFailureMode.FAIL_OPEN
    assert _turn_contribution({"turn-reminder": {"remind_at": None}}).middlewares == ()


@pytest.mark.parametrize(
    "options",
    (
        {"turn-reminder": {"remind_at": 0}},
        {"turn-reminder": {"thresholds": [10, 20]}},
    ),
)
def test_turn_plugin_rejects_invalid_options(
    options: Mapping[str, Mapping[str, JsonValue]],
) -> None:
    """单阈值边界和未知配置在装配期失败。"""
    with pytest.raises(ValueError):
        _turn_contribution(options)


@pytest.mark.parametrize(
    ("turn_index", "expected"),
    (
        (255, MiddlewareDecisionType.CONTINUE),
        (256, MiddlewareDecisionType.MESSAGES),
        (257, MiddlewareDecisionType.CONTINUE),
    ),
)
async def test_turn_plugin_default_threshold_boundary(
    turn_index: int,
    expected: MiddlewareDecisionType,
) -> None:
    """默认第 256 轮首次提醒，相邻轮次不提醒。"""
    decision = await _dispatch(turn_index)

    assert decision.type is expected
    if expected is MiddlewareDecisionType.MESSAGES:
        assert isinstance(decision.payload, MessagesMiddlewarePayload)
        message = decision.payload.messages[0]
        assert message.session_id == "session"
        assert message.role is Role.USER
        assert message.origin == "turn-reminder"
        assert message.visibility is MessageVisibility.MODEL
        assert is_model_visible(message)
        assert not is_public_visible(message)
        assert message.run_id == "run"
        assert message.model is None
        assert message.stop_reason is None
        assert message.error is None
        block = message.content[0]
        assert isinstance(block, TextBlock)
        assert block.text.startswith("<system-reminder>\n当前是第 256 轮。")
        assert block.text.endswith("\n</system-reminder>")


async def test_turn_plugin_honors_custom_threshold() -> None:
    """每 Agent 自定义阈值替代默认值。"""
    decision = await _dispatch(7, {"turn-reminder": {"remind_at": 7}})

    assert decision.type is MiddlewareDecisionType.MESSAGES


@pytest.mark.parametrize("threshold", (1, 7, 256))
async def test_turn_plugin_repeats_every_five_turns(threshold: int) -> None:
    """阈值前不提醒，阈值及其后每五轮提醒，间隔轮次不提醒。"""
    manager = _manager(
        (_turn_contribution({"turn-reminder": {"remind_at": threshold}}),)
    )
    reminded_turns = []
    message_ids = []
    for turn in range(max(1, threshold - 5), threshold + 12):
        decision = await manager.dispatch(_context(turn))
        if decision.type is MiddlewareDecisionType.MESSAGES:
            assert isinstance(decision.payload, MessagesMiddlewarePayload)
            message = decision.payload.messages[0]
            assert isinstance(message.content[0], TextBlock)
            text = message.content[0].text
            assert text.startswith(f"<system-reminder>\n当前是第 {turn} 轮。")
            assert text.endswith("\n</system-reminder>")
            if turn > threshold:
                assert f"你已超出 {turn - threshold} 轮。" in text
            else:
                assert "你已超出" not in text
            reminded_turns.append(turn)
            message_ids.append(message.message_id)
    assert reminded_turns == [threshold, threshold + 5, threshold + 10]
    assert len(set(message_ids)) == 3


async def test_turn_plugin_message_id_is_deterministic() -> None:
    """同一次 Middleware invocation 产生稳定消息 ID。"""
    manager = _manager((_turn_contribution(),))
    context = _context(256)

    first = await manager.dispatch(context)
    second = await manager.dispatch(context)

    assert isinstance(first.payload, MessagesMiddlewarePayload)
    assert isinstance(second.payload, MessagesMiddlewarePayload)
    assert first.payload.messages[0].message_id == second.payload.messages[0].message_id


async def test_turn_plugin_missing_turn_isolated_by_fail_open() -> None:
    """缺失 turn_index 时 Handler 失败，但 Manager 按 FAIL_OPEN 放行。"""
    decision = await _dispatch(None)

    assert decision.type is MiddlewareDecisionType.CONTINUE


def _assistant_call(index: int) -> Message:
    return Message(
        message_id=f"assistant-{index}",
        session_id="session",
        role=Role.ASSISTANT,
        content=(
            ToolCallBlock(
                tool_call_id=f"call-{index}",
                tool_name="search",
                arguments={"query": "value"},
                raw_arguments='{"query":"value"}',
            ),
        ),
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.ALL,
        model="model",
        run_id="run",
        created_at=_NOW,
    )


async def test_turn_and_repeat_plugins_append_messages_in_registry_order() -> None:
    """两个独立 Plugin 同时命中时按稳定 middleware_id 顺序聚合。"""
    plugin_ids = (
        "builtin.runtime.repeat-tool-reminder",
        "builtin.runtime.turn-reminder",
    )
    contributions = _load_plugins(
        plugin_ids,
        {
            "repeat-tool-reminder": {"remind_at": 3},
            "turn-reminder": {"remind_at": 4},
        },
    )
    manager = _manager(contributions)

    decision = await manager.dispatch(
        _context(4, tuple(_assistant_call(index) for index in range(3)))
    )

    assert decision.type is MiddlewareDecisionType.MESSAGES
    assert isinstance(decision.payload, MessagesMiddlewarePayload)
    assert len(decision.payload.messages) == 2
    assert [message.origin for message in decision.payload.messages] == [
        "repeat-tool-reminder",
        "turn-reminder",
    ]
