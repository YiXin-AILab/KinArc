"""AgentLoop 与固定 Projector 之间的保存屏障执行驱动。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hmac import compare_digest
from uuid import uuid4

from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.errors import EventCommitError, RunError
from agentsystem.common.logging import bind_log_context
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import AgentConfig
from agentsystem.context.system_prompt import SystemPromptBuilder, system_message_id
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    ResumeReady,
    RunRequest,
)
from agentsystem.inbox.protocol import Inbox
from agentsystem.lock.lock import LockLease, ReleaseIfIdleResult, SessionLock
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import Message, MessageVisibility, Role
from agentsystem.message.projector import MessageProjector
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewarePoint,
    RunResultMiddlewarePayload,
)
from agentsystem.outbound.projector import PublicProjector
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventFactory,
    AgentEventPayload,
    MessagesCompleted,
    RunCanceled,
    RunCompleted,
    RunFailed,
)
from agentsystem.runtime.loop import (
    AgentLoop,
    LoopInput,
    NewRunInput,
    ResumeInput,
)
from agentsystem.runtime.state import AgentState, SessionStatus
from agentsystem.state.manager import StateManager
from agentsystem.state.projector import StateProjector
from agentsystem.trace.projector import TraceProjector

type _LoopBuilder = Callable[[AgentState], AgentLoop]
type _EventProjector = Callable[
    [SessionContext, AgentEventEnvelope, AgentState, AgentState],
    Awaitable[None],
]

LOGGER = logging.getLogger(__name__)


class _DeferredCancellation(asyncio.CancelledError):
    """事件提交闭合后传播的一个或多个外层取消请求。"""

    def __init__(self, requests: int) -> None:
        """保存提交期间收到的取消请求数。"""
        super().__init__()
        self.requests = requests


class _CancellationBarrier:
    """延迟持锁 Session 的外层取消，使单次事件提交先成功或失败闭合。

    提交运行在独立 Task 中；屏障暂时消费外层取消计数，完成后通过
    _DeferredCancellation 交还请求数量，供 Session 区分用户与宿主取消。
    """

    @staticmethod
    async def run[T](operation: Coroutine[object, object, T]) -> T:
        """延迟外层取消直到 operation 成功或失败。"""
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("事件提交必须运行在 asyncio Task 中")
        commit_task = asyncio.create_task(operation)
        requests = 0
        while not commit_task.done():
            try:
                await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                current_requests = owner.cancelling()
                if current_requests == 0:
                    return commit_task.result()
                requests += current_requests
                for _ in range(current_requests):
                    owner.uncancel()
        result = commit_task.result()
        if requests:
            raise _DeferredCancellation(requests)
        return result


class SessionExecutionKind(StrEnum):
    """表示单次 Session 调度结果，不等同于持久化 run 状态。

    BUSY 仅表示未取得锁，Resume 可重投而普通 Ready 不重投；EXECUTED 和
    IDLE 表示成功释放时是否执行过 run；PENDING、LOST_LOCK 与 INVALID_RESUME
    分别标记挂起、失去执行权和恢复请求不再有效。
    """

    EXECUTED = "executed"
    BUSY = "busy"
    IDLE = "idle"
    PENDING = "pending"
    LOST_LOCK = "lost_lock"
    INVALID_RESUME = "invalid_resume"


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionExecutionResult:
    """返回一次 Session 调度结果，供 Dispatcher 决定后续调度。

    本对象属于内部运行边界，state 仍含内部 run 身份，不是公开响应 DTO。

    Attributes:
        kind: 执行、锁忙、空闲、挂起、失锁或无效恢复结果。
        state: 已读取或执行所得状态；未取得状态时为空。
    """

    kind: SessionExecutionKind
    state: AgentState | None = None


class _LeaseGuard:
    """保存单次租约的本地 heartbeat 控制状态，不拥有锁的存储实现。

    stop 请求结束续约，lost 标记续约返回不再持锁；active_run_id 记录当前
    执行段身份，由 Session 在 run 边界更新。
    """

    def __init__(self) -> None:
        """初始化未停止、未失锁且尚无活动 run 的控制状态。"""
        self.stop = asyncio.Event()
        self.lost = asyncio.Event()
        self.active_run_id: str | None = None


class _SessionEventCommitter:
    """把一个 Loop payload 提交到固定投影流水线并建立保存屏障。"""

    def __init__(
        self,
        *,
        loop: AgentLoop,
        request: RunRequest,
        project_event: _EventProjector,
        inbox: Inbox,
        middleware: MiddlewareManager | None,
        clock: Callable[[], datetime],
        event_id_factory: Callable[[], str],
    ) -> None:
        """保存一次 run 的投影依赖和 previous-state 快照。"""
        self._loop = loop
        self._request = request
        self._project_event = project_event
        self._inbox = inbox
        self._middleware = middleware
        self._factory = AgentEventFactory(
            session_id=request.ctx.session_id,
            run_id=request.run_id,
            trace_id=request.trace_id,
            clock=clock,
            event_id_factory=event_id_factory,
        )
        self._previous = loop.state.model_copy(deep=True)

    async def emit(self, payload: AgentEventPayload) -> None:
        """提交事件；关键失败使用专属控制异常越过 Loop 错误收敛。"""
        current = self._loop.state.model_copy(deep=True)
        envelope = self._factory.create(
            payload,
            turn_index=_event_turn_index(current),
        )
        await _CancellationBarrier.run(
            self._commit(payload, envelope, current),
        )
        self._previous = current

    async def _commit(
        self,
        payload: AgentEventPayload,
        envelope: AgentEventEnvelope,
        current: AgentState,
    ) -> None:
        """在不可逃逸的子 Task 中完成一次固定顺序提交。"""
        try:
            await self._project_event(
                self._request.ctx,
                envelope,
                self._previous,
                current,
            )
            if isinstance(payload, MessagesCompleted) and payload.inbound_modes:
                await self._complete_inbound(payload)
            if isinstance(payload, (RunCompleted, RunFailed, RunCanceled)):
                await self._after_run(current, payload)
        except EventCommitError:
            raise
        except Exception as error:
            raise EventCommitError(error) from error

    async def _complete_inbound(self, payload: MessagesCompleted) -> None:
        """在 Message 保存后完成 Inbox 交接。"""
        if self._request.inbox_owner_token is None:
            raise RuntimeError("Inbox 用户消息缺少 inbox/owner")
        message_ids = tuple(
            message.message_id
            for message in payload.messages
            if message.message_id in payload.inbound_modes
        )
        await self._inbox.complete(
            self._request.ctx,
            message_ids,
            owner_token=self._request.inbox_owner_token,
        )

    async def _after_run(
        self,
        state: AgentState,
        payload: RunCompleted | RunFailed | RunCanceled,
    ) -> None:
        """在终态保存屏障后 fail-open 派发 AFTER_RUN。"""
        if self._middleware is None:
            return
        message = payload.final_message if isinstance(payload, RunCompleted) else None
        error = payload.error if isinstance(payload, (RunFailed, RunCanceled)) else None
        status = state.status
        if status not in {
            SessionStatus.SUCCEEDED,
            SessionStatus.FAILED,
            SessionStatus.CANCELED,
        }:
            raise RuntimeError("AFTER_RUN 要求 terminal state")
        try:
            await self._middleware.dispatch(
                MiddlewareContext(
                    invocation_id="runtime-after-run",
                    point=MiddlewarePoint.AFTER_RUN,
                    ctx=self._request.ctx,
                    run_id=self._request.run_id,
                    trace_id=self._request.trace_id,
                    turn_index=state.turn_index or None,
                    state_snapshot=state.model_copy(deep=True),
                    payload=RunResultMiddlewarePayload(
                        status=status,
                        message=message,
                        error=error,
                    ),
                )
            )
        except Exception:
            return


class AgentSession:
    """管理会话执行权、run 链及固定顺序的事件保存屏障。

    同一 Agent 可共享此入口对象，每次调用使用独立锁租约、heartbeat 和 Loop。
    普通 Ready 锁忙即返回；取得锁后按 run 边界排空 Inbox，再原子检查空闲释放。
    事件按 Message → State → Trace → Public 提交，任一步失败停止后续投影，
    不回滚前序写入。外部连接生命周期由装配层负责。
    """

    def __init__(
        self,
        *,
        message_projector: MessageProjector,
        state_projector: StateProjector,
        trace_projector: TraceProjector,
        public_projector: PublicProjector,
        projector_timeout_seconds: float,
        lock: SessionLock,
        state_manager: StateManager,
        inbox: Inbox,
        loop_builder: _LoopBuilder,
        messages: MessageManager,
        system_prompt_builder: SystemPromptBuilder,
        agent_config: AgentConfig,
        output_mode: OutputMode = OutputMode.COMPLETE,
        cancellation_registry: RunTaskRegistry | None = None,
        middleware_manager: MiddlewareManager | None = None,
        heartbeat_interval_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """保存运行端口和工厂，不获取锁或创建 run。

        Args:
            message_projector: 定稿消息及压缩快照投影器。
            state_projector: 完整状态快照投影器。
            trace_projector: 内部 Trace 投影器。
            public_projector: 唯一公开输出投影器。
            projector_timeout_seconds: 每个投影步骤的超时秒数。
            lock: 会话租约、续约和原子空闲释放端口。
            state_manager: 权威执行快照读取和初始化端口。
            inbox: 普通输入领取、交接与恢复队列端口。
            loop_builder: 根据当前快照创建独立 Loop 的工厂。
            messages: SYSTEM 初始化及消息查询端口。
            system_prompt_builder: 首次构造基础 SYSTEM 消息的依赖。
            agent_config: 本 Session 入口所属 Agent 的固定配置。
            output_mode: 新 run 使用的输出模式。
            cancellation_registry: 运行任务的用户取消注册表。
            middleware_manager: 终态保存后派发 AFTER_RUN 的可选依赖。
            heartbeat_interval_seconds: 锁续约间隔秒数。
            clock: 事件和状态时间来源，默认当前 UTC 时间。
            event_id_factory: 每条事件的身份工厂。
            id_factory: run 和执行段 Trace 的身份工厂。

        Raises:
            ValueError: heartbeat 间隔或投影超时非正数。
        """
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds 必须为正数")
        if projector_timeout_seconds <= 0:
            raise ValueError("projector_timeout_seconds 必须为正数")
        self._message_projector = message_projector
        self._state_projector = state_projector
        self._trace_projector = trace_projector
        self._public_projector = public_projector
        self._projector_timeout = projector_timeout_seconds
        self._lock = lock
        self._states = state_manager
        self._inbox = inbox
        self._build_loop = loop_builder
        self._messages = messages
        self._system_prompt_builder = system_prompt_builder
        self._agent_config = agent_config
        self._output_mode = output_mode
        self._cancellations = cancellation_registry or RunTaskRegistry()
        self._middleware = middleware_manager
        self._heartbeat_interval = heartbeat_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_id_factory = event_id_factory or (lambda: str(uuid4()))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
        """非阻塞竞争 session lock，并在锁内串行驱动普通输入。

        Args:
            ctx: 要执行的完整会话定位。

        Returns:
            调度结果；锁忙立即返回 BUSY，挂起时停止当前 run 链。

        存储、投影和控制流异常向调用方传播；退出时停止 heartbeat 并尝试释放
        尚未释放的租约。只有成功领取非空输入后才分配新的 run 身份。
        """
        lease = await self._lock.acquire(ctx)
        if lease is None:
            return SessionExecutionResult(kind=SessionExecutionKind.BUSY)
        guard, heartbeat_task = self._start_heartbeat(ctx, lease)
        lock_released = False
        try:
            state = await self._states.get(ctx)
            if state is None:
                state = AgentState(session_context=ctx)
                await self._states.create(state)
            result, lock_released = await self._drain_inbox(
                ctx=ctx,
                state=state,
                lease=lease,
                has_executed_run=False,
                guard=guard,
            )
            return result
        finally:
            try:
                await self._stop_heartbeat(guard, heartbeat_task)
            finally:
                if not lock_released:
                    await self._lock.release(ctx, lease.owner_token)

    async def try_resume(self, ready: ResumeReady) -> SessionExecutionResult:
        """非阻塞恢复同一 run，终态后继续驱动普通 Inbox。

        Args:
            ready: 独立恢复队列中的 run 身份和单次凭证。

        Returns:
            锁忙返回 BUSY，凭证无效返回 INVALID_RESUME，其余返回执行结果。
            Dispatcher 仅对 BUSY 结果延迟重投。

        持锁重新校验状态、run、凭证和期限；恢复沿用 run_id 并分配新 trace_id。
        依赖失败和取消向外传播，退出时停止续约并尝试释放租约。
        """
        ctx = _resume_context(ready)
        lease = await self._lock.acquire(ctx)
        if lease is None:
            return SessionExecutionResult(kind=SessionExecutionKind.BUSY)
        guard, heartbeat_task = self._start_heartbeat(ctx, lease)
        lock_released = False
        try:
            state = await self._states.get(ctx)
            if state is None or not _valid_resume(state, ready, self._clock()):
                return SessionExecutionResult(
                    kind=SessionExecutionKind.INVALID_RESUME,
                    state=state,
                )
            if state.output_mode is None:
                raise RuntimeError("合法 PENDING state 缺少 output_mode")
            await self._ensure_bootstrapped(ctx)
            trace_id = self._id_factory()
            request = RunRequest(
                ctx=ctx,
                run_id=ready.run_id,
                trace_id=trace_id,
                output_mode=state.output_mode,
                inbox_owner_token=lease.owner_token,
            )
            with _bind_run_log_context(request):
                LOGGER.info(
                    "run_resumed input_kind=resume resume_id=%s",
                    ready.resume_id,
                )
            guard.active_run_id = ready.run_id
            try:
                state = await self._execute_registered_loop(
                    self._build_loop(state),
                    request,
                    ResumeInput(resume_token=ready.resume_token),
                    guard,
                )
            finally:
                guard.active_run_id = None
                self._cancellations.clear(ctx, ready.run_id)
            if state.status is SessionStatus.PENDING:
                lock_released = await self._lock.release(ctx, lease.owner_token)
                return SessionExecutionResult(
                    kind=SessionExecutionKind.PENDING,
                    state=state,
                )
            result, lock_released = await self._drain_inbox(
                ctx=ctx,
                state=state,
                lease=lease,
                has_executed_run=True,
                guard=guard,
            )
            return result
        finally:
            try:
                await self._stop_heartbeat(guard, heartbeat_task)
            finally:
                if not lock_released:
                    await self._lock.release(ctx, lease.owner_token)

    async def cancel_pending(
        self,
        ctx: SessionContext,
        run_id: str,
    ) -> bool:
        """持锁取消无活动 Task 的 PENDING run，并继续驱动已排队输入。

        Args:
            ctx: 目标会话定位。
            run_id: 请求取消的内部 run 身份，防止取消更新后的 run。

        Returns:
            已取消或同一 run 已为 CANCELED 时为 True；锁忙、身份不匹配或
            状态不可取消时为 False。

        Raises:
            ValueError: run_id 为空。

        终态投影和后续 Inbox 驱动的异常向外传播；返回前清理锁租约。
        """
        if not run_id:
            raise ValueError("cancel run_id 不能为空")
        lease = await self._lock.acquire(ctx)
        if lease is None:
            return False
        guard, heartbeat_task = self._start_heartbeat(ctx, lease)
        lock_released = False
        try:
            state = await self._states.get(ctx)
            if state is None or state.run_id != run_id:
                return False
            if state.status is SessionStatus.CANCELED:
                return True
            if state.status is not SessionStatus.PENDING:
                return False
            with bind_log_context(
                app_id=ctx.app_id,
                user_id=ctx.user_id,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
                run_id=run_id,
            ):
                LOGGER.info(
                    "run_cancel_received previous_status=%s",
                    state.status.value,
                )
            state = await self._finalize_active_run(
                ctx,
                state,
                target_status=SessionStatus.CANCELED,
                error=RunError(
                    code="run_canceled",
                    message="run 已被用户取消",
                ),
            )
            _, lock_released = await self._drain_inbox(
                ctx=ctx,
                state=state,
                lease=lease,
                has_executed_run=True,
                guard=guard,
            )
            return True
        finally:
            try:
                await self._stop_heartbeat(guard, heartbeat_task)
            finally:
                if not lock_released:
                    await self._lock.release(ctx, lease.owner_token)

    async def _drain_inbox(
        self,
        *,
        ctx: SessionContext,
        state: AgentState,
        lease: LockLease,
        has_executed_run: bool,
        guard: _LeaseGuard,
    ) -> tuple[SessionExecutionResult, bool]:
        """在 run 边界领取输入，并用原子空闲释放关闭入站竞争窗口。

        DIRTY 表示读取空队列后又有新输入，必须继续检查；返回值第二项表示
        已成功释放租约，供外层 finally 避免重复释放。
        """
        while True:
            if guard.lost.is_set():
                return (
                    SessionExecutionResult(
                        kind=SessionExecutionKind.LOST_LOCK,
                        state=state,
                    ),
                    False,
                )
            inbound = await self._inbox.claim(
                ctx,
                frozenset({InboundMode.STEER, InboundMode.QUEUE}),
                owner_token=lease.owner_token,
            )
            if not inbound:
                release_result = await self._lock.release_if_idle(
                    ctx,
                    lease.owner_token,
                )
                if release_result is ReleaseIfIdleResult.DIRTY:
                    continue
                kind = _released_kind(release_result, has_executed_run)
                return (
                    SessionExecutionResult(kind=kind, state=state),
                    release_result is ReleaseIfIdleResult.RELEASED,
                )
            await self._ensure_bootstrapped(ctx)
            if state.status in {SessionStatus.PENDING, SessionStatus.RUNNING}:
                target_status, error = _superseded_run_terminal(state)
                state = await self._finalize_active_run(
                    ctx,
                    state,
                    target_status=target_status,
                    error=error,
                )
            state = await self._start_run_from_inbox(
                ctx,
                state,
                inbound,
                guard,
                lease.owner_token,
            )
            has_executed_run = True
            if state.status is SessionStatus.PENDING:
                lock_released = await self._lock.release(ctx, lease.owner_token)
                return (
                    SessionExecutionResult(
                        kind=SessionExecutionKind.PENDING,
                        state=state,
                    ),
                    lock_released,
                )

    async def _ensure_bootstrapped(self, ctx: SessionContext) -> None:
        """确保 session 仅存在一条由当前 Agent 构造的 SYSTEM Message。"""
        if ctx.agent_id != self._agent_config.agent_id:
            raise ValueError("SessionContext agent_id 与 AgentConfig 不一致")
        message_id = system_message_id(ctx)
        existing = await self._messages.get(ctx, message_id)
        if existing is not None:
            if (
                existing.role is not Role.SYSTEM
                or existing.session_id != ctx.session_id
            ):
                raise RuntimeError("固定 system_message_id 已被不兼容 Message 占用")
            return
        message = await self._system_prompt_builder.build(
            ctx,
            agent_config=self._agent_config,
        )
        if message.message_id != message_id or message.role is not Role.SYSTEM:
            raise RuntimeError("SystemPromptBuilder 返回了无效 SYSTEM Message")
        await self._messages.commit(ctx, (message,))

    async def _start_run_from_inbox(
        self,
        ctx: SessionContext,
        state: AgentState,
        inbound: tuple[InboundMessage, ...],
        guard: _LeaseGuard,
        inbox_owner_token: str,
    ) -> AgentState:
        run_id = self._id_factory()
        trace_id = self._id_factory()
        messages = tuple(self._inbound_message(item, run_id) for item in inbound)
        request = RunRequest(
            ctx=ctx,
            run_id=run_id,
            trace_id=trace_id,
            output_mode=self._output_mode,
            inbox_owner_token=inbox_owner_token,
        )
        with _bind_run_log_context(request):
            LOGGER.info(
                "run_started input_kind=message message_ids=%s",
                ",".join(item.message_id for item in inbound),
            )
        guard.active_run_id = run_id
        try:
            return await self._execute_registered_loop(
                self._build_loop(state),
                request,
                NewRunInput(
                    messages=messages,
                    inbound_modes={item.message_id: item.mode for item in inbound},
                ),
                guard,
            )
        finally:
            guard.active_run_id = None
            self._cancellations.clear(ctx, run_id)

    async def _execute_loop(
        self,
        loop: AgentLoop,
        request: RunRequest,
        loop_input: LoopInput,
    ) -> AgentState:
        """每次事件提交成功后才允许 Loop 继续执行。"""
        committer = self._event_committer(loop, request)
        try:
            return await loop.run(
                request,
                loop_input,
                committer.emit,
            )
        except EventCommitError as error:
            if error.cause is None:
                raise
            raise error.cause from None

    async def _execute_registered_loop(
        self,
        loop: AgentLoop,
        request: RunRequest,
        loop_input: LoopInput,
        guard: _LeaseGuard,
    ) -> AgentState:
        """注册单 run Task，并只把纯用户取消收敛为业务终态。"""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AgentSession 必须运行在 asyncio Task 中")
        with _bind_run_log_context(request):
            self._cancellations.register(request.ctx, request.run_id, task)
            try:
                return await self._execute_loop(loop, request, loop_input)
            except asyncio.CancelledError as error:
                if not self._consume_only_user_cancellation(request, error):
                    raise
                if guard.lost.is_set():
                    raise
                state = await self._states.get(request.ctx)
                if state is None or state.run_id != request.run_id:
                    raise
                if state.status in {
                    SessionStatus.SUCCEEDED,
                    SessionStatus.FAILED,
                    SessionStatus.CANCELED,
                }:
                    return state
                LOGGER.info(
                    "run_cancel_received previous_status=%s",
                    state.status.value,
                )
                return await self._finalize_active_run(
                    request.ctx,
                    state,
                    target_status=SessionStatus.CANCELED,
                    error=RunError(
                        code="run_canceled",
                        message="run 已被用户取消",
                    ),
                    trace_id=request.trace_id,
                )
            finally:
                self._cancellations.clear(request.ctx, request.run_id)

    def _consume_only_user_cancellation(
        self,
        request: RunRequest,
        error: asyncio.CancelledError,
    ) -> bool:
        """消费 Registry 所属的一次取消，并让任何并发宿主取消优先。"""
        if not self._cancellations.consume_user_cancellation(
            request.ctx,
            request.run_id,
        ):
            return False
        if isinstance(error, _DeferredCancellation):
            return error.requests == 1
        task = asyncio.current_task()
        if task is None:
            return False
        return task.uncancel() == 0

    async def _finalize_active_run(
        self,
        ctx: SessionContext,
        state: AgentState,
        *,
        target_status: SessionStatus,
        error: RunError,
        trace_id: str | None = None,
    ) -> AgentState:
        """使用旧 run 把活动状态收敛到指定唯一终态。"""
        if state.run_id is None or state.output_mode is None:
            raise RuntimeError("活动 State 缺少 run_id 或 output_mode")
        request = RunRequest(
            ctx=ctx,
            run_id=state.run_id,
            trace_id=trace_id or self._id_factory(),
            output_mode=state.output_mode,
        )
        with _bind_run_log_context(request):
            loop = self._build_loop(state)
            committer = self._event_committer(loop, request)
            try:
                return await loop.finalize(
                    request,
                    target_status=target_status,
                    error=error,
                    emit=committer.emit,
                )
            except EventCommitError as commit_error:
                if commit_error.cause is None:
                    raise
                raise commit_error.cause from None

    def _event_committer(
        self,
        loop: AgentLoop,
        request: RunRequest,
    ) -> _SessionEventCommitter:
        """构造一次 Loop 调用使用的事件提交边界。"""
        return _SessionEventCommitter(
            loop=loop,
            request=request,
            project_event=self._project_event,
            inbox=self._inbox,
            middleware=self._middleware,
            clock=self._clock,
            event_id_factory=self._event_id_factory,
        )

    async def _project_event(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
        previous_state: AgentState,
        current_state: AgentState,
    ) -> None:
        """按固定顺序投影；任一异常或超时立即停止后续步骤。"""
        async with asyncio.timeout(self._projector_timeout):
            await self._message_projector.project(ctx, event)
        if previous_state != current_state:
            async with asyncio.timeout(self._projector_timeout):
                await self._state_projector.project(
                    ctx,
                    event,
                    previous_state,
                    current_state,
                )
        async with asyncio.timeout(self._projector_timeout):
            await self._trace_projector.project(ctx, event)
        async with asyncio.timeout(self._projector_timeout):
            await self._public_projector.project(ctx, event)

    def _inbound_message(self, inbound: InboundMessage, run_id: str) -> Message:
        """把 Inbox DTO 转为当前 run 的输入 Message，并在领取时补时间。"""
        return Message(
            message_id=inbound.message_id,
            session_id=inbound.session_id,
            role=Role(inbound.role.value),
            content=inbound.content,
            origin=inbound.origin,
            visibility=MessageVisibility.ALL,
            run_id=run_id,
            created_at=inbound.created_at or self._clock(),
        )

    def _start_heartbeat(
        self,
        ctx: SessionContext,
        lease: LockLease,
    ) -> tuple[_LeaseGuard, asyncio.Task[None]]:
        guard = _LeaseGuard()
        task = asyncio.create_task(
            self._heartbeat_loop(ctx, lease, guard),
            name=f"session-heartbeat:{ctx.session_id}",
        )
        return guard, task

    async def _heartbeat_loop(
        self,
        ctx: SessionContext,
        lease: LockLease,
        guard: _LeaseGuard,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    guard.stop.wait(),
                    timeout=self._heartbeat_interval,
                )
                return
            except TimeoutError:
                pass
            if await self._lock.heartbeat(ctx, lease.owner_token):
                continue
            guard.lost.set()
            return

    @staticmethod
    async def _stop_heartbeat(
        guard: _LeaseGuard,
        task: asyncio.Task[None],
    ) -> None:
        guard.stop.set()
        await task


def _superseded_run_terminal(
    state: AgentState,
) -> tuple[SessionStatus, RunError]:
    """返回新输入取代活动 run 时使用的唯一终态和公开错误。"""
    if state.status is SessionStatus.PENDING:
        return (
            SessionStatus.CANCELED,
            RunError(
                code="superseded_by_input",
                message="挂起 run 已被新的普通输入取代",
            ),
        )
    return (
        SessionStatus.FAILED,
        RunError(
            code="orphaned_run",
            message="检测到失去 owner 的遗留 run",
        ),
    )


def _event_turn_index(state: AgentState) -> int | None:
    if state.turn_index == 0:
        return None
    return state.turn_index


def _resume_context(ready: ResumeReady) -> SessionContext:
    return SessionContext(
        app_id=ready.app_id,
        user_id=ready.user_id,
        agent_id=ready.agent_id,
        session_id=ready.session_id,
    )


def _bind_run_log_context(
    request: RunRequest,
) -> AbstractContextManager[None]:
    """绑定一次已领取 run 的完整业务日志上下文。"""
    ctx = request.ctx
    return bind_log_context(
        app_id=ctx.app_id,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        session_id=ctx.session_id,
        run_id=request.run_id,
        runtime_trace_id=request.trace_id,
    )


def _valid_resume(
    state: AgentState,
    ready: ResumeReady,
    now: datetime,
) -> bool:
    if (
        state.status is not SessionStatus.PENDING
        or state.run_id != ready.run_id
        or state.resume_token is None
        or not compare_digest(state.resume_token, ready.resume_token)
    ):
        return False
    return state.suspend_expires_at is None or now < state.suspend_expires_at


def _released_kind(
    result: ReleaseIfIdleResult,
    has_executed_run: bool,
) -> SessionExecutionKind:
    if result is ReleaseIfIdleResult.NOT_OWNER:
        return SessionExecutionKind.LOST_LOCK
    if has_executed_run:
        return SessionExecutionKind.EXECUTED
    return SessionExecutionKind.IDLE
