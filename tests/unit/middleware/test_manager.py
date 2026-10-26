"""Middleware 注册、排序、决策与失败策略测试。"""

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from agentsystem.common.errors import RunError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    ErrorMiddlewarePayload,
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewareHandler,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
    RunMiddlewarePayload,
    ToolCallMiddlewarePayload,
    ToolResultMiddlewarePayload,
    TurnMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.runtime.state import AgentState, SessionStatus

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_SESSION = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def state() -> AgentState:
    """返回固定 RUNNING state。"""
    return AgentState(
        session_context=_SESSION,
        status=SessionStatus.RUNNING,
        run_id="run",
        output_mode=OutputMode.COMPLETE,
        started_at=_NOW,
        active_started_at=_NOW,
    )


def run_payload(text: str = "one") -> RunMiddlewarePayload:
    """返回包含单条 USER Message 的 Run payload。"""
    message = Message(
        message_id=text,
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text=text),),
        origin=MessageOrigin.USER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    return RunMiddlewarePayload(messages=(message,), output_mode=OutputMode.COMPLETE)


def test_turn_payloads_and_messages_decision_are_explicit() -> None:
    """Turn 输入和 Message 输出使用不同的明确 payload。"""
    message = Message(
        message_id="reminder",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="review progress"),),
        origin="test-middleware",
        visibility=MessageVisibility.MODEL,
        run_id="run",
        created_at=_NOW,
    )

    turn = TurnMiddlewarePayload(messages=(message,))
    messages_to_append = MessagesMiddlewarePayload(messages=(message,))

    assert turn.messages == (message,)
    assert messages_to_append.messages == (message,)
    assert MiddlewarePoint.BEFORE_TURN.value == "before_turn"
    assert MiddlewarePoint.AFTER_TURN.value == "after_turn"
    assert MiddlewareDecisionType.MESSAGES.value == "messages"


def test_messages_payload_rejects_empty_batch() -> None:
    """空 Message 输出必须使用 CONTINUE 表达。"""
    with pytest.raises(ValueError, match="不能为空"):
        MessagesMiddlewarePayload(messages=())


def middleware_context(
    point: MiddlewarePoint = MiddlewarePoint.BEFORE_RUN,
    payload: (
        RunMiddlewarePayload
        | ToolCallMiddlewarePayload
        | ErrorMiddlewarePayload
        | TurnMiddlewarePayload
        | None
    ) = None,
) -> MiddlewareContext:
    """构造一次中间件派发上下文。"""
    return MiddlewareContext(
        invocation_id="dispatch",
        point=point,
        ctx=_SESSION,
        run_id="run",
        trace_id="trace",
        state_snapshot=state().model_copy(deep=True),
        payload=payload or run_payload(),
    )


def service_message(message_id: str) -> Message:
    """返回 Middleware 可追加的模型可见 USER Message。"""
    return Message(
        message_id=message_id,
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text=message_id),),
        origin="test-middleware",
        visibility=MessageVisibility.MODEL,
        run_id="run",
        created_at=_NOW,
    )


def turn_context(
    point: MiddlewarePoint = MiddlewarePoint.BEFORE_TURN,
) -> MiddlewareContext:
    """构造 Turn 挂载点上下文。"""
    return middleware_context(
        point,
        TurnMiddlewarePayload(messages=run_payload().messages),
    )


def turn_registry(
    *items: tuple[str, int, MiddlewareHandler],
    point: MiddlewarePoint = MiddlewarePoint.BEFORE_TURN,
) -> MiddlewareRegistry:
    """按给定 metadata 构造冻结的 Turn Registry。"""
    registry = MiddlewareRegistry()
    for middleware_id, priority, handler in items:
        registry.register(
            MiddlewareRegistration(
                name=middleware_id,
                middleware_id=middleware_id,
                point=point,
                scope=MiddlewareScope.GLOBAL,
                priority=priority,
                handler=handler,
            )
        )
    registry.freeze()
    return registry


async def test_before_turn_aggregates_messages_without_replacing_input() -> None:
    """MESSAGES 只贡献输出，后续 Handler 仍观察原 Turn 快照。"""
    original = run_payload().messages
    seen: list[tuple[Message, ...]] = []

    def contribute(message_id: str) -> MiddlewareHandler:
        async def invoke(context: MiddlewareContext) -> MiddlewareDecision:
            assert isinstance(context.payload, TurnMiddlewarePayload)
            seen.append(context.payload.messages)
            return MiddlewareDecision(
                type=MiddlewareDecisionType.MESSAGES,
                payload=MessagesMiddlewarePayload(
                    messages=(service_message(message_id),)
                ),
            )

        return invoke

    registry = turn_registry(
        ("a", 0, contribute("a")),
        ("b", 0, contribute("b")),
    )
    decision = await MiddlewareManager(registry).dispatch(
        middleware_context(
            MiddlewarePoint.BEFORE_TURN,
            TurnMiddlewarePayload(messages=original),
        )
    )

    assert seen == [original, original]
    assert decision.type is MiddlewareDecisionType.MESSAGES
    assert isinstance(decision.payload, MessagesMiddlewarePayload)
    assert [item.message_id for item in decision.payload.messages] == ["a", "b"]


async def test_before_turn_block_discards_accumulated_messages() -> None:
    """后续 BLOCK 优先于尚未提交的 Message accumulator。"""
    calls: list[str] = []

    async def contribute(_: MiddlewareContext) -> MiddlewareDecision:
        calls.append("contribute")
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=(service_message("new"),)),
        )

    async def block(_: MiddlewareContext) -> MiddlewareDecision:
        calls.append("block")
        return MiddlewareDecision(
            type=MiddlewareDecisionType.BLOCK,
            reason_code="turn_policy_blocked",
        )

    registry = turn_registry(("contribute", 10, contribute), ("block", 0, block))
    decision = await MiddlewareManager(registry).dispatch(turn_context())

    assert calls == ["contribute", "block"]
    assert decision.type is MiddlewareDecisionType.BLOCK
    assert decision.payload is None


@pytest.mark.parametrize(
    ("point", "decision_type"),
    (
        (MiddlewarePoint.BEFORE_TURN, MiddlewareDecisionType.MODIFY),
        (MiddlewarePoint.BEFORE_RUN, MiddlewareDecisionType.MESSAGES),
    ),
)
async def test_turn_decision_matrix_rejects_disallowed_results(
    point: MiddlewarePoint,
    decision_type: MiddlewareDecisionType,
) -> None:
    """MESSAGES 和 MODIFY 只出现在各自明确允许的挂载点。"""
    payload = (
        MessagesMiddlewarePayload(messages=(service_message("new"),))
        if decision_type is MiddlewareDecisionType.MESSAGES
        else TurnMiddlewarePayload(messages=run_payload().messages)
    )

    async def decide(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=decision_type, payload=payload)

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="invalid",
            middleware_id="invalid",
            point=point,
            scope=MiddlewareScope.GLOBAL,
            handler=decide,
        )
    )
    registry.freeze()
    context = (
        turn_context() if point is MiddlewarePoint.BEFORE_TURN else middleware_context()
    )

    with pytest.raises(ValueError):
        await MiddlewareManager(registry).dispatch(context)


@pytest.mark.parametrize(
    "message",
    (
        service_message("valid").model_copy(update={"session_id": "other"}),
        service_message("valid").model_copy(update={"run_id": "other"}),
        service_message("valid").model_copy(update={"role": Role.ASSISTANT}),
        service_message("valid").model_copy(
            update={"visibility": MessageVisibility.ALL}
        ),
        service_message("valid").model_copy(update={"model": "model"}),
        service_message("valid").model_copy(update={"content": ()}),
    ),
)
async def test_before_turn_rejects_invalid_message_to_append(
    message: Message,
) -> None:
    """Runtime 只接纳身份和 Message 领域约束都有效的提醒。"""

    async def contribute(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=(message,)),
        )

    with pytest.raises(ValueError):
        await MiddlewareManager(
            turn_registry(("invalid-message", 0, contribute))
        ).dispatch(turn_context())


async def test_before_turn_rejects_non_message_and_duplicate_ids() -> None:
    """Message 类型和原始/累计 ID 冲突都在 Manager 边界失败。"""

    async def non_message(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(
                messages=(cast(Message, "not-a-message"),)
            ),
        )

    with pytest.raises(ValueError, match="完整 Message"):
        await MiddlewareManager(
            turn_registry(("non-message", 0, non_message))
        ).dispatch(turn_context())

    for batch in (
        (service_message("one"), service_message("one")),
        (service_message("one"),),
    ):

        async def duplicate(
            _: MiddlewareContext,
            batch: tuple[Message, ...] = batch,
        ) -> MiddlewareDecision:
            return MiddlewareDecision(
                type=MiddlewareDecisionType.MESSAGES,
                payload=MessagesMiddlewarePayload(messages=batch),
            )

        with pytest.raises(ValueError, match="重复"):
            await MiddlewareManager(
                turn_registry(("duplicate", 0, duplicate))
            ).dispatch(turn_context())

    async def first(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=(service_message("shared"),)),
        )

    async def second(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=(service_message("shared"),)),
        )

    with pytest.raises(ValueError, match="重复"):
        await MiddlewareManager(
            turn_registry(("first", 10, first), ("second", 0, second))
        ).dispatch(turn_context())


async def test_before_turn_handler_failure_is_fail_open() -> None:
    """提醒 Handler 故障记录后不阻止后续贡献。"""

    async def broken(_: MiddlewareContext) -> MiddlewareDecision:
        raise RuntimeError("broken reminder")

    async def healthy(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=(service_message("healthy"),)),
        )

    decision = await MiddlewareManager(
        turn_registry(("broken", 10, broken), ("healthy", 0, healthy))
    ).dispatch(turn_context())

    assert decision.type is MiddlewareDecisionType.MESSAGES
    assert isinstance(decision.payload, MessagesMiddlewarePayload)
    assert decision.payload.messages[0].message_id == "healthy"


async def test_after_turn_isolates_invalid_decision_and_handler_failure() -> None:
    """完成后观察点执行完整链且永远返回 CONTINUE。"""
    calls: list[str] = []

    async def invalid(_: MiddlewareContext) -> MiddlewareDecision:
        calls.append("invalid")
        return MiddlewareDecision(type=MiddlewareDecisionType.BLOCK)

    async def broken(_: MiddlewareContext) -> MiddlewareDecision:
        calls.append("broken")
        raise RuntimeError("observer failed")

    async def reached(_: MiddlewareContext) -> MiddlewareDecision:
        calls.append("reached")
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    registry = turn_registry(
        ("invalid", 20, invalid),
        ("broken", 10, broken),
        ("reached", 0, reached),
        point=MiddlewarePoint.AFTER_TURN,
    )

    decision = await MiddlewareManager(registry).dispatch(
        turn_context(MiddlewarePoint.AFTER_TURN)
    )

    assert calls == ["invalid", "broken", "reached"]
    assert decision.type is MiddlewareDecisionType.CONTINUE


async def test_resolves_scope_and_runs_by_priority_then_id() -> None:
    """GLOBAL/APP 合并后统一排序，其他 app 注册不执行。"""
    calls: list[str] = []

    def handler(name: str, replacement: str) -> MiddlewareHandler:
        async def invoke(context: MiddlewareContext) -> MiddlewareDecision:
            calls.append(name)
            assert context.invocation_id != "dispatch"
            return MiddlewareDecision(
                type=MiddlewareDecisionType.MODIFY,
                payload=run_payload(replacement),
            )

        return invoke

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="b",
            middleware_id="b",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            priority=10,
            handler=handler("b", "two"),
        )
    )
    registry.register(
        MiddlewareRegistration(
            name="a",
            middleware_id="a",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.APP,
            app_id="app",
            priority=10,
            handler=handler("a", "three"),
        )
    )
    registry.register(
        MiddlewareRegistration(
            name="other",
            middleware_id="other",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.APP,
            app_id="other",
            priority=100,
            handler=handler("other", "bad"),
        )
    )
    registry.freeze()

    decision = await MiddlewareManager(registry).dispatch(middleware_context())

    assert calls == ["a", "b"]
    assert decision.type is MiddlewareDecisionType.MODIFY
    assert isinstance(decision.payload, RunMiddlewarePayload)
    assert decision.payload.messages[0].message_id == "two"


async def test_timeout_fail_open_continues_and_fail_closed_blocks() -> None:
    """每个 handler 独立 timeout，并服从失败模式。"""

    async def slow(_: MiddlewareContext) -> MiddlewareDecision:
        await asyncio.sleep(0.05)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    async def reached(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.BLOCK,
            reason_code="explicit_block",
        )

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="open",
            middleware_id="open",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            timeout_ms=1,
            failure_mode=MiddlewareFailureMode.FAIL_OPEN,
            handler=slow,
        )
    )
    registry.register(
        MiddlewareRegistration(
            name="reached",
            middleware_id="reached",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            priority=-1,
            handler=reached,
        )
    )
    registry.freeze()
    decision = await MiddlewareManager(registry).dispatch(middleware_context())
    assert decision.reason_code == "explicit_block"

    closed = MiddlewareRegistry()
    closed.register(
        MiddlewareRegistration(
            name="closed",
            middleware_id="closed",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            timeout_ms=1,
            failure_mode=MiddlewareFailureMode.FAIL_CLOSED,
            handler=slow,
        )
    )
    closed.freeze()
    decision = await MiddlewareManager(closed).dispatch(middleware_context())
    assert decision.type is MiddlewareDecisionType.BLOCK
    assert decision.reason_code == "middleware_execution_failed"


async def test_fail_open_handler_bug_logs_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """被 FAIL_OPEN 隔离的程序异常仍必须留下可定位堆栈。"""

    async def broken(_: MiddlewareContext) -> MiddlewareDecision:
        raise RuntimeError("handler bug")

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="broken",
            middleware_id="broken",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            failure_mode=MiddlewareFailureMode.FAIL_OPEN,
            handler=broken,
        )
    )
    registry.freeze()

    with caplog.at_level("ERROR"):
        decision = await MiddlewareManager(registry).dispatch(middleware_context())

    assert decision.type is MiddlewareDecisionType.CONTINUE
    failure = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("middleware_failure ")
    )
    assert failure.exc_info is not None
    assert "error_id=" in failure.getMessage()


async def test_rejects_disallowed_decision_and_missing_payload() -> None:
    """Point 不允许的决策和缺失 MODIFY payload 立即失败。"""

    async def retry(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.RETRY)

    async def missing(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.MODIFY)

    for middleware_id, handler in (("retry", retry), ("missing", missing)):
        registry = MiddlewareRegistry()
        registry.register(
            MiddlewareRegistration(
                name=middleware_id,
                middleware_id=middleware_id,
                point=MiddlewarePoint.BEFORE_RUN,
                scope=MiddlewareScope.GLOBAL,
                handler=handler,
            )
        )
        registry.freeze()
        with pytest.raises(ValueError):
            await MiddlewareManager(registry).dispatch(middleware_context())


async def test_tool_modify_cannot_change_call_identity() -> None:
    """Tool 中间件只能修改参数，不能改变调用 ID 或工具名。"""
    original = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="search",
        arguments={"q": "one"},
        raw_arguments='{"q":"one"}',
    )

    async def mutate(_: MiddlewareContext) -> MiddlewareDecision:
        changed = original.model_copy(update={"tool_call_id": "call-2"})
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MODIFY,
            payload=ToolCallMiddlewarePayload(call=changed),
        )

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="tool",
            middleware_id="tool",
            point=MiddlewarePoint.BEFORE_TOOL_CALL,
            scope=MiddlewareScope.GLOBAL,
            handler=mutate,
        )
    )
    registry.freeze()
    context = middleware_context(
        MiddlewarePoint.BEFORE_TOOL_CALL,
        ToolCallMiddlewarePayload(call=original),
    )

    with pytest.raises(ValueError, match="身份"):
        await MiddlewareManager(registry).dispatch(context)


async def test_tool_on_error_cannot_return_mismatched_tool_result() -> None:
    """ON_ERROR HANDLED 必须同时保持 ToolCall 与 ToolResult 身份。"""
    original = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="lookup",
        arguments={},
        raw_arguments="{}",
    )

    async def mishandle(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.HANDLED,
            payload=ToolResultMiddlewarePayload(
                call=original,
                result=ToolResultBlock(
                    tool_call_id="wrong-call",
                    tool_name="lookup",
                    content="invalid",
                    is_error=False,
                ),
            ),
        )

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="bad-tool-handler",
            middleware_id="bad-tool-handler",
            point=MiddlewarePoint.ON_ERROR,
            scope=MiddlewareScope.GLOBAL,
            handler=mishandle,
        )
    )
    registry.freeze()
    context = middleware_context(
        MiddlewarePoint.ON_ERROR,
        ErrorMiddlewarePayload(
            operation="tool",
            error=RunError(
                code="tool_error",
                message="Tool 执行返回错误",
            ),
            tool_call=original,
        ),
    )

    with pytest.raises(ValueError, match="身份"):
        await MiddlewareManager(registry).dispatch(context)


def test_registry_rejects_invalid_scope_duplicate_and_late_write() -> None:
    """注册错误在启动阶段暴露，冻结后不可写。"""

    async def handler(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    registry = MiddlewareRegistry()
    with pytest.raises(ValueError, match="app_id"):
        registry.register(
            MiddlewareRegistration(
                name="bad",
                middleware_id="bad",
                point=MiddlewarePoint.BEFORE_RUN,
                scope=MiddlewareScope.APP,
                handler=handler,
            )
        )
    valid = MiddlewareRegistration(
        name="valid",
        middleware_id="valid",
        point=MiddlewarePoint.BEFORE_RUN,
        scope=MiddlewareScope.GLOBAL,
        handler=handler,
    )
    registry.register(valid)
    with pytest.raises(ValueError, match="重复"):
        registry.register(valid)
    registry.freeze()
    with pytest.raises(RuntimeError, match="冻结"):
        registry.register(
            MiddlewareRegistration(
                name="late",
                middleware_id="late",
                point=MiddlewarePoint.BEFORE_RUN,
                scope=MiddlewareScope.GLOBAL,
                handler=handler,
            )
        )
