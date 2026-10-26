"""AgentSession 事件包装、状态快照和关键投影屏障测试。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from agentsystem.common.errors import RunError
from agentsystem.common.logging import setup_logging
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
    RuntimeLoggingConfig,
)
from agentsystem.context.plugins.provider_middleware import (
    register as register_provider_middleware,
)
from agentsystem.context.providers import ContextProvider, ContextProviderRunner
from agentsystem.context.system_prompt import SystemPromptBuilder, system_message_id
from agentsystem.inbox.models import RunRequest
from agentsystem.lock.lock import LockLease, ReleaseIfIdleResult
from agentsystem.message.models import (
    InjectedItem,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.projector import MessageProjector
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
    SystemPromptBuildPayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventFactory,
    AgentEventPayload,
    RunCompleted,
    RunStarted,
)
from agentsystem.runtime.loop import (
    AgentLoop,
    LoopInput,
    NewRunInput,
)
from agentsystem.runtime.session import (
    AgentSession,
    _CancellationBarrier,
    _DeferredCancellation,
    _LeaseGuard,
)
from agentsystem.runtime.state import (
    AgentState,
    SessionStatus,
    complete_run,
    run_timing,
    start_run,
)
from agentsystem.skill.index import SkillIndex
from agentsystem.state.projector import StateProjector
from agentsystem.tool.registry import ToolRegistry
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
_REQUEST = RunRequest(
    ctx=_CONTEXT,
    run_id="run-1",
    trace_id="trace-1",
    output_mode=OutputMode.COMPLETE,
)
_CONFIG = AgentConfig(
    agent_id="agent",
    agent_name="Agent",
    prompt="prompt",
    outbound=AgentOutboundConfig(type="log"),
    llm_model=ModelParamsConfig(model="model"),
)


class _Loop(AgentLoop):
    def __init__(self) -> None:
        self._state = AgentState(session_context=_CONTEXT)
        self.resumed_after_barrier = False
        self.closed = False

    @property
    def state(self) -> AgentState:
        return self._state

    async def run(
        self,
        request: RunRequest,
        loop_input: LoopInput,
        emit: Callable[[AgentEventPayload], Awaitable[None]],
    ) -> AgentState:
        try:
            self._state = start_run(
                self._state,
                run_id=request.run_id,
                output_mode=request.output_mode,
                now=_NOW,
            )
            assert isinstance(loop_input, NewRunInput)
            await emit(RunStarted(initial_messages=loop_input.messages))
            self.resumed_after_barrier = True
        finally:
            self.closed = True
        return self._state.model_copy(deep=True)

    async def finalize(
        self,
        request: RunRequest,
        *,
        target_status: SessionStatus,
        error: RunError,
        emit: Callable[[AgentEventPayload], Awaitable[None]],
    ) -> AgentState:
        """本测试替身不应进入 terminal finalize。"""
        del request, target_status, error, emit
        if self.closed:
            raise AssertionError("closed loop cannot be finalized")
        return self._state.model_copy(deep=True)


class _NoopProjector:
    async def project(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _StateRecorder:
    def __init__(self, pipeline: "_Pipeline") -> None:
        self._pipeline = pipeline

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
        previous_state: AgentState,
        current_state: AgentState,
    ) -> None:
        assert ctx == _CONTEXT
        self._pipeline.calls.append((event, previous_state, current_state))
        if self._pipeline.error is not None:
            raise self._pipeline.error


class _Pipeline:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[AgentEventEnvelope, AgentState, AgentState]] = []
        self.message = cast(MessageProjector, _NoopProjector())
        self.state = cast(StateProjector, _StateRecorder(self))
        self.trace = cast(TraceProjector, _NoopProjector())
        self.public = _NoopProjector()


class _UnusedLock:
    async def acquire(self, ctx: SessionContext) -> LockLease | None:
        raise AssertionError(f"unexpected acquire: {ctx.session_id}")

    async def heartbeat(self, ctx: SessionContext, owner_token: str) -> bool:
        raise AssertionError(f"unexpected heartbeat: {ctx.session_id}:{owner_token}")

    async def release(self, ctx: SessionContext, owner_token: str) -> bool:
        raise AssertionError(f"unexpected release: {ctx.session_id}:{owner_token}")

    async def release_if_idle(
        self,
        ctx: SessionContext,
        owner_token: str,
    ) -> ReleaseIfIdleResult:
        raise AssertionError(
            f"unexpected release_if_idle: {ctx.session_id}:{owner_token}"
        )


class _Messages(MessageManagerStub):
    def __init__(self) -> None:
        self.items: dict[str, Message] = {}
        self.commits = 0

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        assert ctx == _CONTEXT
        return self.items.get(message_id)

    async def commit(
        self,
        ctx: SessionContext,
        messages: tuple[Message, ...],
    ) -> None:
        assert ctx == _CONTEXT
        self.commits += 1
        for message in messages:
            self.items[message.message_id] = message


class _Builder(SystemPromptBuilder):
    def __init__(self) -> None:
        self.calls = 0

    async def build(
        self,
        ctx: SessionContext,
        *,
        agent_config: AgentConfig,
    ) -> Message:
        assert ctx == _CONTEXT and agent_config == _CONFIG
        self.calls += 1
        return Message(
            message_id=system_message_id(ctx),
            session_id=ctx.session_id,
            role=Role.SYSTEM,
            content=(TextBlock(text="system"),),
            origin=MessageOrigin.SYSTEM,
            created_at=_NOW,
            visibility=MessageVisibility.MODEL,
        )


class _DirectProjector:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self._name = name
        self._calls = calls
        self._error = error
        self._block = block

    async def project(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._calls.append(self._name)
        if self._block:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error


def _direct_session(
    calls: list[str],
    *,
    failing: str | None = None,
    blocking: str | None = None,
    timeout: float = 0.1,
) -> AgentSession:
    def projector(name: str) -> _DirectProjector:
        return _DirectProjector(
            name,
            calls,
            error=RuntimeError(name) if failing == name else None,
            block=blocking == name,
        )

    return AgentSession(
        message_projector=cast(MessageProjector, projector("message")),
        state_projector=cast(StateProjector, projector("state")),
        trace_projector=cast(TraceProjector, projector("trace")),
        public_projector=projector("public"),
        projector_timeout_seconds=timeout,
        lock=_UnusedLock(),
        state_manager=StateManagerStub(),
        inbox=InboxStub(),
        loop_builder=lambda _: _Loop(),
        messages=MessageManagerStub(),
        system_prompt_builder=_Builder(),
        agent_config=_CONFIG,
    )


def _run_started_event() -> AgentEventEnvelope:
    return AgentEventFactory(
        session_id="session",
        run_id="run-1",
        trace_id="trace-1",
        clock=lambda: _NOW,
    ).create(RunStarted(initial_messages=()))


async def test_session_directly_projects_in_fixed_order() -> None:
    calls: list[str] = []
    session = _direct_session(calls)
    previous = AgentState(session_context=_CONTEXT)
    current = previous.model_copy(update={"turn_index": 1})

    await session._project_event(_CONTEXT, _run_started_event(), previous, current)

    assert calls == ["message", "state", "trace", "public"]


async def test_cancellation_barrier_finishes_before_propagating_requests() -> None:
    """用户与宿主同时取消也不能让事件提交逃逸到锁生命周期之外。"""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def commit() -> None:
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(_CancellationBarrier.run(commit()))
    await started.wait()
    task.cancel("user")
    task.cancel("host")
    release.set()

    with pytest.raises(_DeferredCancellation) as raised:
        await task

    assert raised.value.requests == 2
    assert finished.is_set()


@pytest.mark.parametrize("failing", ["message", "state", "trace", "public"])
async def test_session_projector_failure_is_fail_close(failing: str) -> None:
    calls: list[str] = []
    session = _direct_session(calls, failing=failing)
    previous = AgentState(session_context=_CONTEXT)
    current = previous.model_copy(update={"turn_index": 1})

    with pytest.raises(RuntimeError, match=failing):
        await session._project_event(
            _CONTEXT,
            _run_started_event(),
            previous,
            current,
        )

    order = ["message", "state", "trace", "public"]
    assert calls == order[: order.index(failing) + 1]


async def test_session_projector_timeout_is_fail_close() -> None:
    calls: list[str] = []
    session = _direct_session(calls, blocking="trace", timeout=0.001)
    state = AgentState(session_context=_CONTEXT)

    with pytest.raises(TimeoutError):
        await session._project_event(_CONTEXT, _run_started_event(), state, state)

    assert calls == ["message", "trace"]


def _session(
    pipeline: _Pipeline,
    *,
    middleware: MiddlewareManager | None = None,
    loop_builder: Callable[[AgentState], AgentLoop] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> AgentSession:
    return AgentSession(
        message_projector=pipeline.message,
        state_projector=pipeline.state,
        trace_projector=pipeline.trace,
        public_projector=pipeline.public,
        projector_timeout_seconds=0.1,
        lock=_UnusedLock(),
        state_manager=StateManagerStub(),
        inbox=InboxStub(),
        loop_builder=loop_builder or (lambda _: _Loop()),
        messages=MessageManagerStub(),
        system_prompt_builder=_Builder(),
        agent_config=_CONFIG,
        middleware_manager=middleware,
        id_factory=id_factory,
    )


async def test_bootstrap_builds_once_and_reuses_existing_system_message() -> None:
    """Session 私有初始化逻辑重复执行时不得创建第二条 SYSTEM Message。"""
    messages = _Messages()
    builder = _Builder()
    session = AgentSession(
        message_projector=_Pipeline().message,
        state_projector=_Pipeline().state,
        trace_projector=_Pipeline().trace,
        public_projector=_Pipeline().public,
        projector_timeout_seconds=0.1,
        lock=_UnusedLock(),
        state_manager=StateManagerStub(),
        inbox=InboxStub(),
        loop_builder=lambda _: _Loop(),
        messages=messages,
        system_prompt_builder=builder,
        agent_config=_CONFIG,
    )

    await session._ensure_bootstrapped(_CONTEXT)
    await session._ensure_bootstrapped(_CONTEXT)

    assert builder.calls == 1
    assert messages.commits == 1
    assert tuple(messages.items) == (system_message_id(_CONTEXT),)


async def test_prompt_hook_retries_before_save_and_is_frozen_after_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def after(ctx: MiddlewareContext) -> MiddlewareDecision:
        nonlocal calls
        calls += 1
        assert ctx.run_id is None and ctx.state_snapshot is None
        return MiddlewareDecision(
            type=MiddlewareDecisionType.UPDATE,
            payload=SystemPromptBuildPayload(prompt=f"attempt-{calls}"),
        )

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="after",
            middleware_id="after",
            point=MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
            scope=MiddlewareScope.GLOBAL,
            handler=after,
        )
    )
    registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    builder = SystemPromptBuilder(
        base_prompt="base",
        tool_registry=tools,
        skill_index=SkillIndex.load(roots=(tmp_path,), enabled_skills=()),
        middleware_manager=MiddlewareManager(registry),
    )
    messages = _Messages()
    session = _session(_Pipeline())
    monkeypatch.setattr(session, "_system_prompt_builder", builder)
    monkeypatch.setattr(session, "_messages", messages)
    commit = messages.commit

    async def fail(ctx: SessionContext, items: tuple[Message, ...]) -> None:
        raise ValueError("injected commit failure")

    monkeypatch.setattr(messages, "commit", fail)
    with pytest.raises(ValueError, match="commit failure"):
        await session._ensure_bootstrapped(_CONTEXT)
    assert messages.items == {}
    monkeypatch.setattr(messages, "commit", commit)
    await session._ensure_bootstrapped(_CONTEXT)
    await session._ensure_bootstrapped(_CONTEXT)
    assert calls == 2 and messages.commits == 1
    assert messages.items[system_message_id(_CONTEXT)].content == (
        TextBlock(text="attempt-2"),
    )


async def test_session_projects_previous_and_post_state_before_resuming_loop() -> None:
    """Session 提交 post-state 成功后，Loop 才能越过 await emit 屏障。"""
    loop = _Loop()
    pipeline = _Pipeline()
    session = _session(pipeline)
    result = await session._execute_loop(
        loop,
        _REQUEST,
        NewRunInput(messages=(_user_message(),)),
    )

    assert loop.resumed_after_barrier is True
    assert loop.closed is True
    assert result == loop.state
    event, previous, current = pipeline.calls[0]
    assert event.run_id == "run-1"
    assert event.trace_id == "trace-1"
    assert event.turn_index is None
    assert previous.run_id is None
    assert current.run_id == "run-1"


async def test_registered_loop_binds_run_and_trace_log_context(tmp_path: Path) -> None:
    """一次执行段内的日志必须携带既有 RunRequest 标识并在结束后恢复。"""
    logger = logging.getLogger("tests.session.context.run")

    class ContextLoop(_Loop):
        async def run(
            self,
            request: RunRequest,
            loop_input: LoopInput,
            emit: Callable[[AgentEventPayload], Awaitable[None]],
        ) -> AgentState:
            del request, loop_input, emit
            logger.info("inside run")
            return self._state

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    session = _session(_Pipeline())

    try:
        await session._execute_registered_loop(
            ContextLoop(),
            _REQUEST,
            NewRunInput(messages=()),
            _LeaseGuard(),
        )
        logger.info("outside run")
    finally:
        runtime.close()

    lines = (tmp_path / "logs" / "agentsystem.log").read_text().splitlines()
    inside, outside = lines
    assert "run=run-1 segment=trace-1" in inside
    assert "run=" not in outside
    assert "segment=" not in outside


async def test_terminal_settlement_binds_run_and_new_trace_context(
    tmp_path: Path,
) -> None:
    """旧 run 的终态收敛必须绑定本次新执行段标识。"""
    logger = logging.getLogger("tests.session.context.finalize")

    class FinalizeLoop(_Loop):
        async def finalize(
            self,
            request: RunRequest,
            *,
            target_status: SessionStatus,
            error: RunError,
            emit: Callable[[AgentEventPayload], Awaitable[None]],
        ) -> AgentState:
            del request, target_status, error, emit
            logger.info("inside finalize")
            return self._state

    runtime = setup_logging(
        RuntimeLoggingConfig(console_enabled=False, directory=Path("logs")),
        project_root=tmp_path,
    )
    session = _session(
        _Pipeline(),
        loop_builder=lambda _: FinalizeLoop(),
        id_factory=lambda: "trace-final",
    )
    state = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-final",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )

    try:
        await session._finalize_active_run(
            _CONTEXT,
            state,
            target_status=SessionStatus.CANCELED,
            error=RunError(
                code="run_canceled",
                message="canceled",
            ),
        )
        logger.info("outside finalize")
    finally:
        runtime.close()

    lines = (tmp_path / "logs" / "agentsystem.log").read_text().splitlines()
    inside, outside = lines
    assert "run=run-final segment=trace-final" in inside
    assert "run=" not in outside
    assert "segment=" not in outside


async def test_critical_projection_failure_closes_loop_without_resuming() -> None:
    """关键投影失败必须中止 Loop，禁止执行提交屏障之后的外部操作。"""
    loop = _Loop()
    session = _session(_Pipeline(RuntimeError("state CAS failed")))
    with pytest.raises(RuntimeError, match="state CAS failed"):
        await session._execute_loop(
            loop,
            _REQUEST,
            NewRunInput(messages=(_user_message(),)),
        )

    assert loop.resumed_after_barrier is False
    assert loop.closed is True


async def test_after_run_middleware_runs_after_terminal_projection() -> None:
    """AFTER_RUN 只能在终态 State 投影成功后观察结果。"""
    pipeline = _Pipeline()
    observed_terminal_counts: list[int] = []
    provider_terminal_counts: list[int] = []

    class Provider(ContextProvider):
        provider_name = "observer"

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            del ctx, message
            return None

        async def put(self, ctx: SessionContext, *, message: Message) -> None:
            del ctx, message
            provider_terminal_counts.append(
                sum(
                    call[2].status is SessionStatus.SUCCEEDED for call in pipeline.calls
                )
            )

    async def observe(context: MiddlewareContext) -> MiddlewareDecision:
        assert context.point is MiddlewarePoint.AFTER_RUN
        observed_terminal_counts.append(
            sum(call[2].status is SessionStatus.SUCCEEDED for call in pipeline.calls)
        )
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    runner = ContextProviderRunner(timeout_seconds=0.1)
    runner.register_many((Provider(),))
    runner.freeze()
    services = PluginSetupServices()
    services.register(ContextProviderRunner, runner)
    services.freeze()
    setup = PluginSetupContext(
        plugin_id="builtin.context.provider-middleware",
        agent_id="agent",
        origin=PluginOrigin.BUILTIN,
        options={},
        services=services,
    )
    register_provider_middleware(setup)
    provider_put = setup._seal_and_extract().middlewares[0]
    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="after-run",
            middleware_id="after-run",
            point=MiddlewarePoint.AFTER_RUN,
            scope=MiddlewareScope.GLOBAL,
            handler=observe,
        )
    )
    registry.register(provider_put)
    registry.freeze()

    class _TerminalLoop(_Loop):
        async def run(
            self,
            request: RunRequest,
            loop_input: LoopInput,
            emit: Callable[[AgentEventPayload], Awaitable[None]],
        ) -> AgentState:
            self._state = start_run(
                self._state,
                run_id=request.run_id,
                output_mode=request.output_mode,
                now=_NOW,
            )
            assert isinstance(loop_input, NewRunInput)
            await emit(RunStarted(initial_messages=loop_input.messages))
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

    session = _session(
        pipeline,
        middleware=MiddlewareManager(registry),
    )

    await session._execute_loop(
        _TerminalLoop(),
        _REQUEST,
        NewRunInput(messages=(_user_message(),)),
    )

    assert observed_terminal_counts == [1]
    assert provider_terminal_counts == [1]


def _user_message() -> Message:
    return Message(
        message_id="user-1",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
