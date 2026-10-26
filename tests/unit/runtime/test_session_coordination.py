"""AgentSession 非阻塞锁、Inbox 领取和 run chain 测试。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.errors import RunError
from agentsystem.common.logging import setup_logging
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
    RuntimeLoggingConfig,
)
from agentsystem.context.system_prompt import SystemPromptBuilder, system_message_id
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    InboundRole,
    ResumeReady,
    RunRequest,
)
from agentsystem.lock.lock import LockLease, ReleaseIfIdleResult
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.projector import MessageProjector
from agentsystem.outbound.projector import PublicProjector
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventPayload,
    MessagesCompleted,
    MessageStarted,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunResumed,
    RunStarted,
)
from agentsystem.runtime.loop import (
    AgentLoop,
    LoopInput,
    NewRunInput,
    ResumeInput,
)
from agentsystem.runtime.session import (
    AgentSession,
    SessionExecutionKind,
)
from agentsystem.runtime.state import (
    AgentState,
    SessionStatus,
    cancel_run,
    complete_run,
    fail_run,
    resume_run,
    run_timing,
    start_run,
    suspend_run,
)
from agentsystem.state.manager import StateWriteResult
from agentsystem.state.projector import StateProjector
from agentsystem.trace.projector import TraceProjector
from tests.unit.runtime.port_stubs import (
    InboxStub,
    MessageManagerStub,
    StateManagerStub,
)

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)
_CONFIG = AgentConfig(
    agent_id="agent",
    agent_name="Agent",
    prompt="prompt",
    outbound=AgentOutboundConfig(type="log"),
    llm_model=ModelParamsConfig(model="model"),
)


class _Lock:
    def __init__(
        self,
        lease: LockLease | None,
        *,
        heartbeat_result: bool = True,
        heartbeat_error: Exception | None = None,
    ) -> None:
        self.lease = lease
        self.released = 0
        self.heartbeat_result = heartbeat_result
        self.heartbeat_error = heartbeat_error
        self.heartbeat_count = 0

    async def acquire(self, ctx: SessionContext) -> LockLease | None:
        assert ctx == _CONTEXT
        return self.lease

    async def release(self, ctx: SessionContext, owner_token: str) -> bool:
        assert ctx == _CONTEXT and owner_token == "owner"
        self.released += 1
        return True

    async def heartbeat(self, ctx: SessionContext, owner_token: str) -> bool:
        assert ctx == _CONTEXT and owner_token == "owner"
        self.heartbeat_count += 1
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return self.heartbeat_result

    async def release_if_idle(
        self,
        ctx: SessionContext,
        owner_token: str,
    ) -> ReleaseIfIdleResult:
        assert ctx == _CONTEXT and owner_token == "owner"
        self.released += 1
        return ReleaseIfIdleResult.RELEASED


class _StateManager(StateManagerStub):
    def __init__(self) -> None:
        self.state: AgentState | None = None

    async def get(self, ctx: SessionContext) -> AgentState | None:
        assert ctx == _CONTEXT
        return self.state

    async def create(self, initial_state: AgentState) -> StateWriteResult:
        self.state = initial_state
        return StateWriteResult.APPLIED


class _DelayedFailingStateManager(_StateManager):
    async def get(self, ctx: SessionContext) -> AgentState | None:
        assert ctx == _CONTEXT
        await asyncio.sleep(0.02)
        raise RuntimeError("state failed")


class _Queue(InboxStub):
    def __init__(self, batches: list[tuple[InboundMessage, ...]]) -> None:
        self.batches = batches
        self.claim_count = 0
        self.completed: list[tuple[tuple[str, ...], str]] = []

    async def claim(
        self,
        ctx: SessionContext,
        modes: frozenset[InboundMode],
        *,
        owner_token: str,
    ) -> tuple[InboundMessage, ...]:
        assert ctx == _CONTEXT
        assert modes == frozenset({InboundMode.STEER, InboundMode.QUEUE})
        assert owner_token == "owner"
        self.claim_count += 1
        return self.batches.pop(0)

    async def complete(
        self,
        ctx: SessionContext,
        message_ids: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None:
        assert ctx == _CONTEXT
        self.completed.append((message_ids, owner_token))


class _Loop(AgentLoop):
    def __init__(self, initial: AgentState, delay: float = 0) -> None:
        self._state = initial
        self._delay = delay
        self.received: NewRunInput | None = None
        self.received_resume: ResumeInput | None = None
        self.finalized: tuple[SessionStatus, RunError] | None = None

    @property
    def state(self) -> AgentState:
        return self._state

    async def run(
        self,
        request: RunRequest,
        loop_input: LoopInput,
        emit: Callable[[AgentEventPayload], Awaitable[None]],
    ) -> AgentState:
        if isinstance(loop_input, NewRunInput):
            self.received = loop_input
            self._state = start_run(
                self._state,
                run_id=request.run_id,
                output_mode=request.output_mode,
                now=_NOW,
            )
            await emit(RunStarted(initial_messages=loop_input.messages))
            for message in loop_input.messages:
                await emit(
                    MessageStarted(
                        message=message,
                        mode=loop_input.inbound_modes[message.message_id],
                    )
                )
            await emit(
                MessagesCompleted(
                    messages=loop_input.messages,
                    inbound_modes=loop_input.inbound_modes,
                )
            )
            if self._delay:
                await asyncio.sleep(self._delay)
        else:
            self.received_resume = loop_input
            self._state = resume_run(
                self._state,
                resume_token=loop_input.resume_token,
                now=_NOW,
            )
            await emit(RunResumed())
        final = Message(
            message_id="assistant",
            session_id="session",
            role=Role.ASSISTANT,
            content=(TextBlock(text="done"),),
            origin=MessageOrigin.MODEL,
            run_id=request.run_id,
            created_at=_NOW,
            visibility=MessageVisibility.ALL,
        )
        self._state = complete_run(
            self._state,
            final_message_id=final.message_id,
            now=_NOW,
        )
        await emit(
            RunCompleted(
                final_message=final,
                usage=self._state.usage_total,
                timing=run_timing(self._state),
            )
        )
        return self._state.model_copy(deep=True)

    async def finalize(
        self,
        request: RunRequest,
        *,
        target_status: SessionStatus,
        error: RunError,
        emit: Callable[[AgentEventPayload], Awaitable[None]],
    ) -> AgentState:
        assert request.ctx == _CONTEXT
        self.finalized = (target_status, error)
        if target_status is SessionStatus.CANCELED:
            self._state = cancel_run(self._state, now=_NOW, error=error)
            await emit(
                RunCanceled(
                    error=error,
                    usage=self._state.usage_total,
                    timing=run_timing(self._state),
                )
            )
            return self._state.model_copy(deep=True)
        self._state = fail_run(self._state, error=error, now=_NOW)
        await emit(
            RunFailed(
                error=error,
                usage=self._state.usage_total,
                timing=run_timing(self._state),
            )
        )
        return self._state.model_copy(deep=True)


class _LoopFactory:
    def __init__(self, delay: float = 0) -> None:
        self.loops: list[_Loop] = []
        self.delay = delay

    def __call__(self, initial_state: AgentState) -> _Loop:
        loop = _Loop(initial_state, self.delay)
        self.loops.append(loop)
        return loop


class _EventRecorder:
    def __init__(self, events: list[AgentEventEnvelope]) -> None:
        self._events = events

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        del ctx
        self._events.append(event)


class _NoopProjector:
    async def project(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _Pipeline:
    def __init__(self) -> None:
        self.events: list[AgentEventEnvelope] = []
        self.message = cast(MessageProjector, _EventRecorder(self.events))
        self.state = cast(StateProjector, _NoopProjector())
        self.trace = cast(TraceProjector, _NoopProjector())
        self.public = cast(PublicProjector, _NoopProjector())


class _ProjectorArgs(TypedDict):
    message_projector: MessageProjector
    state_projector: StateProjector
    trace_projector: TraceProjector
    public_projector: PublicProjector
    projector_timeout_seconds: float


def _projectors(pipeline: _Pipeline) -> _ProjectorArgs:
    return {
        "message_projector": pipeline.message,
        "state_projector": pipeline.state,
        "trace_projector": pipeline.trace,
        "public_projector": pipeline.public,
        "projector_timeout_seconds": 0.1,
    }


class _BootstrapMessages(MessageManagerStub):
    def __init__(self) -> None:
        self.get_calls = 0

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        assert ctx == _CONTEXT
        assert message_id == system_message_id(ctx)
        self.get_calls += 1
        return Message(
            message_id=message_id,
            session_id=ctx.session_id,
            role=Role.SYSTEM,
            content=(TextBlock(text="system"),),
            origin=MessageOrigin.SYSTEM,
            created_at=_NOW,
            visibility=MessageVisibility.MODEL,
        )


class _UnusedSystemPromptBuilder(SystemPromptBuilder):
    def __init__(self) -> None:
        pass

    async def build(
        self,
        ctx: SessionContext,
        *,
        agent_config: AgentConfig,
    ) -> Message:
        del ctx, agent_config
        raise AssertionError("existing SYSTEM Message should be reused")


async def test_try_execute_runs_inbox_under_lock_and_releases_if_idle(
    tmp_path: Path,
) -> None:
    """Session 领取输入、内部生成 run/trace、执行终态后原子关门。"""
    user_inbound = InboundMessage(
        message_id="user-inbound",
        session_id="session",
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        created_at=_NOW,
    )
    system_inbound = InboundMessage(
        message_id="system-inbound",
        session_id="session",
        role=InboundRole.SYSTEM,
        content=(TextBlock(text="business notice"),),
        origin="workflow.queue",
        created_at=_NOW,
    )
    lock = _Lock(LockLease(owner_token="owner"))
    queue = _Queue([((user_inbound, system_inbound)), ()])
    factory = _LoopFactory()
    messages = _BootstrapMessages()
    pipeline = _Pipeline()
    session = AgentSession(
        **_projectors(pipeline),
        lock=lock,
        state_manager=_StateManager(),
        inbox=queue,
        loop_builder=factory,
        messages=messages,
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        output_mode=OutputMode.COMPLETE,
        clock=lambda: _NOW,
        id_factory=iter(("run-internal", "trace-internal")).__next__,
    )

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    try:
        result = await session.try_execute(_CONTEXT)
    finally:
        runtime.close()

    assert result.kind is SessionExecutionKind.EXECUTED
    assert result.state is not None
    assert result.state.final_message_id == "assistant"
    assert queue.claim_count == 2
    assert queue.completed == [(("user-inbound", "system-inbound"), "owner")]
    assert lock.released == 1
    assert messages.get_calls == 1
    initial = factory.loops[0].received
    assert initial is not None
    assert [message.message_id for message in initial.messages] == [
        "user-inbound",
        "system-inbound",
    ]
    assert {message.run_id for message in initial.messages} == {"run-internal"}
    assert initial.messages[1].role is Role.SYSTEM
    assert initial.messages[1].origin == "workflow.queue"
    started_roles = [
        event.payload.message.role
        for event in pipeline.events
        if isinstance(event.payload, MessageStarted)
    ]
    assert started_roles == [Role.USER, Role.SYSTEM]
    event_types = [event.payload.type for event in pipeline.events]
    assert event_types[:4] == [
        "run.started",
        "message.started",
        "message.started",
        "messages.completed",
    ]
    log_text = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert (
        "app=app user=user agent=agent session=session run=run-internal "
        "segment=trace-internal" in log_text
    )
    assert (
        "run_started input_kind=message message_ids=user-inbound,system-inbound"
        in log_text
    )


async def test_try_execute_returns_busy_without_touching_inbox() -> None:
    """锁忙是正常分布式竞争结果，不等待、不领取 Inbox。"""
    queue = _Queue([])
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=_Lock(None),
        state_manager=_StateManager(),
        inbox=queue,
        loop_builder=_LoopFactory(),
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        output_mode=OutputMode.COMPLETE,
    )

    result = await session.try_execute(_CONTEXT)

    assert result.kind is SessionExecutionKind.BUSY
    assert queue.claim_count == 0


async def test_try_execute_releases_lock_when_heartbeat_task_fails() -> None:
    lock = _Lock(
        LockLease(owner_token="owner"),
        heartbeat_error=RuntimeError("heartbeat failed"),
    )
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=lock,
        state_manager=_DelayedFailingStateManager(),
        inbox=_Queue([]),
        loop_builder=_LoopFactory(),
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        heartbeat_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await session.try_execute(_CONTEXT)

    assert lock.released == 1


async def test_try_resume_releases_lock_when_heartbeat_task_fails() -> None:
    lock = _Lock(
        LockLease(owner_token="owner"),
        heartbeat_error=RuntimeError("heartbeat failed"),
    )
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=lock,
        state_manager=_DelayedFailingStateManager(),
        inbox=_Queue([]),
        loop_builder=_LoopFactory(),
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        heartbeat_interval_seconds=0.001,
    )
    ready = ResumeReady(
        **_CONTEXT.model_dump(),
        resume_id="resume-1",
        run_id="run-1",
        resume_token="resume-token",
    )

    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await session.try_resume(ready)

    assert lock.released == 1


async def test_cancel_releases_lock_when_heartbeat_task_fails() -> None:
    lock = _Lock(
        LockLease(owner_token="owner"),
        heartbeat_error=RuntimeError("heartbeat failed"),
    )
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=lock,
        state_manager=_DelayedFailingStateManager(),
        inbox=_Queue([]),
        loop_builder=_LoopFactory(),
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        heartbeat_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await session.cancel_pending(_CONTEXT, "run-1")

    assert lock.released == 1


async def test_try_resume_keeps_run_id_and_drains_following_inbox(
    tmp_path: Path,
) -> None:
    """合法 Resume 使用原 run、新 trace，完成后继续锁内检查普通 Inbox。"""
    states = _StateManager()
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-pending",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    states.state = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="resume-token",
        now=_NOW,
    )
    queue = _Queue([()])
    factory = _LoopFactory()
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=_Lock(LockLease(owner_token="owner")),
        state_manager=states,
        inbox=queue,
        loop_builder=factory,
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        clock=lambda: _NOW,
        id_factory=iter(("resume-trace",)).__next__,
    )
    ready = ResumeReady(
        **_CONTEXT.model_dump(),
        resume_id="resume-1",
        run_id="run-pending",
        resume_token="resume-token",
    )

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    try:
        result = await session.try_resume(ready)
    finally:
        runtime.close()

    assert result.kind is SessionExecutionKind.EXECUTED
    assert result.state is not None
    assert result.state.run_id == "run-pending"
    assert result.state.final_message_id == "assistant"
    assert factory.loops[0].received_resume == ResumeInput(resume_token="resume-token")
    assert queue.claim_count == 1
    log_text = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert (
        "app=app user=user agent=agent session=session run=run-pending "
        "segment=resume-trace" in log_text
    )
    assert "run_resumed input_kind=resume resume_id=resume-1" in log_text


async def test_try_resume_rejects_stale_token_without_starting_loop() -> None:
    """非 PENDING、run 不匹配或 token 失效不得启动 Loop，也不得重投。"""
    states = _StateManager()
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-pending",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    states.state = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="expected-token",
        now=_NOW,
    )
    factory = _LoopFactory()
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=_Lock(LockLease(owner_token="owner")),
        state_manager=states,
        inbox=_Queue([]),
        loop_builder=factory,
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
    )
    ready = ResumeReady(
        **_CONTEXT.model_dump(),
        resume_id="resume-1",
        run_id="run-pending",
        resume_token="stale-token",
    )

    result = await session.try_resume(ready)

    assert result.kind is SessionExecutionKind.INVALID_RESUME
    assert factory.loops == []


async def test_lost_heartbeat_stops_before_starting_next_run() -> None:
    """失去锁所有权后不再领取下一批 Inbox，并返回 LOST_LOCK。"""
    inbound = InboundMessage(
        message_id="inbound-1",
        session_id="session",
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        created_at=_NOW,
    )
    lock = _Lock(
        LockLease(owner_token="owner"),
        heartbeat_result=False,
    )
    queue = _Queue([(inbound,), ()])
    factory = _LoopFactory(delay=0.02)
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=lock,
        state_manager=_StateManager(),
        inbox=queue,
        loop_builder=factory,
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        heartbeat_interval_seconds=0.001,
        clock=lambda: _NOW,
        id_factory=iter(("run-1", "trace-1")).__next__,
    )

    result = await session.try_execute(_CONTEXT)

    assert result.kind is SessionExecutionKind.LOST_LOCK
    assert lock.heartbeat_count >= 1
    assert queue.claim_count == 1
    assert factory.loops[0].received is not None


async def test_user_task_cancellation_finalizes_run_and_keeps_session_lock(
    tmp_path: Path,
) -> None:
    """执行节点收到广播后应中断 run、保存唯一 CANCELED 并在收口后释放锁。"""
    inbound = InboundMessage(
        message_id="inbound-1",
        session_id="session",
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        created_at=_NOW,
    )
    lock = _Lock(LockLease(owner_token="owner"))
    states = _StateManager()

    class SavingStateProjector:
        async def project(
            self,
            ctx: SessionContext,
            event: AgentEventEnvelope,
            previous_state: AgentState,
            current_state: AgentState,
        ) -> None:
            del event, previous_state
            assert ctx == _CONTEXT
            states.state = current_state

    pipeline = _Pipeline()
    pipeline.state = cast(StateProjector, SavingStateProjector())
    registry = RunTaskRegistry()
    factory = _LoopFactory(delay=0.2)
    session = AgentSession(
        **_projectors(pipeline),
        lock=lock,
        state_manager=states,
        inbox=_Queue([(inbound,), ()]),
        loop_builder=factory,
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        cancellation_registry=registry,
        heartbeat_interval_seconds=1,
        clock=lambda: _NOW,
        id_factory=iter(("run-1", "trace-1", "cancel-trace")).__next__,
    )

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    try:
        execution = asyncio.create_task(session.try_execute(_CONTEXT))
        while states.state is None or states.state.status is not SessionStatus.RUNNING:
            await asyncio.sleep(0)
        assert registry.request_if_active(_CONTEXT, "run-1") is True
        result = await asyncio.wait_for(execution, timeout=1)
    finally:
        runtime.close()

    assert result.state is not None
    assert result.state.status is SessionStatus.CANCELED
    final_state = await states.get(_CONTEXT)
    assert final_state is not None
    assert final_state.status is SessionStatus.CANCELED
    assert lock.released == 1
    canceled_events = [
        event for event in pipeline.events if isinstance(event.payload, RunCanceled)
    ]
    assert len(canceled_events) == 1
    assert canceled_events[0].trace_id == "trace-1"
    log_text = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert (
        "app=app user=user agent=agent session=session run=run-1 segment=trace-1"
        in log_text
    )
    assert "run_cancel_received previous_status=running" in log_text


async def test_host_cancellation_wins_when_it_races_user_cancellation() -> None:
    """同一交付窗口包含宿主取消时不得伪造业务 CANCELED。"""
    inbound = InboundMessage(
        message_id="inbound-1",
        session_id="session",
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        created_at=_NOW,
    )
    lock = _Lock(LockLease(owner_token="owner"))
    states = _StateManager()

    class SavingStateProjector:
        async def project(
            self,
            ctx: SessionContext,
            event: AgentEventEnvelope,
            previous_state: AgentState,
            current_state: AgentState,
        ) -> None:
            del event, previous_state
            assert ctx == _CONTEXT
            states.state = current_state

    pipeline = _Pipeline()
    pipeline.state = cast(StateProjector, SavingStateProjector())
    registry = RunTaskRegistry()
    session = AgentSession(
        **_projectors(pipeline),
        lock=lock,
        state_manager=states,
        inbox=_Queue([(inbound,)]),
        loop_builder=_LoopFactory(delay=10),
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        cancellation_registry=registry,
        heartbeat_interval_seconds=1,
        clock=lambda: _NOW,
        id_factory=iter(("run-1", "trace-1")).__next__,
    )

    execution = asyncio.create_task(session.try_execute(_CONTEXT))
    while states.state is None or states.state.status is not SessionStatus.RUNNING:
        await asyncio.sleep(0)
    assert registry.request_if_active(_CONTEXT, "run-1") is True
    execution.cancel("host shutdown")

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert states.state is not None
    assert states.state.status is SessionStatus.RUNNING
    assert not any(isinstance(event.payload, RunCanceled) for event in pipeline.events)
    assert lock.released == 1


async def test_new_input_settles_pending_before_starting_new_run() -> None:
    """PENDING 收到普通输入时先取消旧 run，再以新内部 run_id 启动。"""
    states = _StateManager()
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="old-run",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    states.state = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="old-token",
        now=_NOW,
    )
    inbound = InboundMessage(
        message_id="new-input",
        session_id="session",
        content=(TextBlock(text="replace old run"),),
        origin=MessageOrigin.USER,
        created_at=_NOW,
    )
    factory = _LoopFactory()
    session = AgentSession(
        **_projectors(_Pipeline()),
        lock=_Lock(LockLease(owner_token="owner")),
        state_manager=states,
        inbox=_Queue([(inbound,), ()]),
        loop_builder=factory,
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        clock=lambda: _NOW,
        id_factory=iter(("settlement-trace", "new-run", "new-trace")).__next__,
    )

    result = await session.try_execute(_CONTEXT)

    assert result.kind is SessionExecutionKind.EXECUTED
    assert result.state is not None
    assert result.state.run_id == "new-run"
    assert factory.loops[0].finalized is not None
    assert factory.loops[0].finalized[0] is SessionStatus.CANCELED
    assert factory.loops[1].received is not None
    assert factory.loops[1].received.messages[0].run_id == "new-run"


async def test_cancel_settles_pending_run_under_lock(tmp_path: Path) -> None:
    """PENDING 没有活动 Task 时，在锁内投影 RunCanceled 后确认成功。"""
    states = _StateManager()
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-pending",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    states.state = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="token",
        now=_NOW,
    )
    pipeline = _Pipeline()
    factory = _LoopFactory()
    session = AgentSession(
        **_projectors(pipeline),
        lock=_Lock(LockLease(owner_token="owner")),
        state_manager=states,
        inbox=_Queue([()]),
        loop_builder=factory,
        messages=_BootstrapMessages(),
        system_prompt_builder=_UnusedSystemPromptBuilder(),
        agent_config=_CONFIG,
        clock=lambda: _NOW,
        id_factory=iter(("cancel-trace",)).__next__,
    )

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    try:
        accepted = await session.cancel_pending(_CONTEXT, "run-pending")
    finally:
        runtime.close()

    assert accepted is True
    assert factory.loops[0].finalized is not None
    assert factory.loops[0].finalized[0] is SessionStatus.CANCELED
    assert any(isinstance(event.payload, RunCanceled) for event in pipeline.events)
    log_text = (tmp_path / "logs" / "agentsystem.log").read_text()
    assert "app=app user=user agent=agent session=session run=run-pending" in log_text
    assert "run_cancel_received previous_status=pending" in log_text
