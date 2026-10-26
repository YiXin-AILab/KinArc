"""无批次屏障 Dispatcher 的调度、隔离和停机测试。"""

import asyncio
from collections.abc import Sequence
from typing import cast

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
)
from agentsystem.inbox.models import NormalReady, ReadyEntry, ResumeReady
from agentsystem.runtime.session import (
    AgentSession,
    SessionExecutionKind,
    SessionExecutionResult,
)
from agentsystem.service.agents import AgentRegistry, AgentRuntimeBundle
from agentsystem.service.dispatcher import TaskDispatcher
from tests.unit.runtime.port_stubs import InboxStub

_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


class _Queue(InboxStub):
    def __init__(self, entries: tuple[ReadyEntry, ...]) -> None:
        self.entries = entries
        self.requeued: list[ResumeReady] = []
        self.requeued_event = asyncio.Event()
        self.requested_agent_ids: tuple[str, ...] | None = None
        self.read_count = 0

    async def read_ready(
        self,
        consumer_id: str,
        *,
        agent_ids: Sequence[str],
        count: int,
        block_ms: int,
    ) -> tuple[ReadyEntry, ...]:
        assert consumer_id == "worker-1"
        assert count == 1 and block_ms == 10
        self.requested_agent_ids = tuple(agent_ids)
        self.read_count += 1
        entries, self.entries = self.entries, ()
        return entries

    async def requeue_resume(self, ready: ResumeReady) -> None:
        self.requeued.append(ready)
        self.requeued_event.set()


class _Sessions:
    def __init__(self) -> None:
        self.normal: list[SessionContext] = []
        self.resumed: list[ResumeReady] = []

    async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
        self.normal.append(ctx)
        return SessionExecutionResult(kind=SessionExecutionKind.EXECUTED)

    async def try_resume(self, ready: ResumeReady) -> SessionExecutionResult:
        self.resumed.append(ready)
        return SessionExecutionResult(kind=SessionExecutionKind.BUSY)


def _bundle(agent_id: str, sessions: object) -> AgentRuntimeBundle:
    return AgentRuntimeBundle(
        agent_config=AgentConfig(
            agent_id=agent_id,
            agent_name="Agent",
            prompt="prompt.md",
            outbound=AgentOutboundConfig(type="log"),
            llm_model=ModelParamsConfig(model="openai/test"),
        ),
        session=cast(AgentSession, sessions),
    )


def _registry(sessions: object) -> AgentRegistry:
    return AgentRegistry((_bundle("agent", sessions),))


def _dispatcher(
    inbox: _Queue,
    sessions: object,
    *,
    agent_ids: tuple[str, ...] = ("agent",),
    resume_requeue_delay_ms: int = 20,
) -> TaskDispatcher:
    """构造使用固定测试消费参数的 Dispatcher。"""
    return TaskDispatcher(
        inbox=inbox,
        agents=_registry(sessions),
        agent_ids=agent_ids,
        consumer_id="worker-1",
        ready_block_ms=10,
        resume_requeue_delay_ms=resume_requeue_delay_ms,
    )


async def test_poll_once_schedules_ready_without_waiting_for_session() -> None:
    """单次读取只安排任务，不能等待长 Session 完成。"""
    ready = NormalReady(**_CONTEXT.model_dump())
    queue = _Queue((ready,))
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    class Sessions(_Sessions):
        async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
            del ctx
            started.set()
            await release.wait()
            completed.set()
            return SessionExecutionResult(kind=SessionExecutionKind.EXECUTED)

    dispatcher = _dispatcher(queue, Sessions())

    count = await dispatcher.poll_once()

    assert count == 1
    await asyncio.wait_for(started.wait(), timeout=0.1)
    assert completed.is_set() is False
    release.set()
    await asyncio.wait_for(completed.wait(), timeout=0.1)


async def test_dispatcher_resolves_session_from_registry_by_agent_id() -> None:
    """Dispatcher 直接按 Ready agent_id 选择 Registry 中的 Session。"""
    first = _Sessions()
    second = _Sessions()
    context = _CONTEXT.model_copy(update={"agent_id": "second"})
    queue = _Queue((NormalReady(**context.model_dump()),))
    dispatcher = TaskDispatcher(
        inbox=queue,
        agents=AgentRegistry((_bundle("first", first), _bundle("second", second))),
        agent_ids=("first", "second"),
        consumer_id="worker-1",
        ready_block_ms=10,
        resume_requeue_delay_ms=20,
    )

    assert await dispatcher.poll_once() == 1
    async with asyncio.timeout(0.1):
        while not second.normal:
            await asyncio.sleep(0)

    assert first.normal == []
    assert second.normal == [context]


async def test_poll_once_starts_all_returned_stream_entries_without_slots() -> None:
    """来自多个 Stream 的 Ready 必须都能立即进入 Session，而非等待本地槽位。"""
    contexts = tuple(
        _CONTEXT.model_copy(update={"session_id": f"session-{index}"})
        for index in range(4)
    )
    queue = _Queue(tuple(NormalReady(**ctx.model_dump()) for ctx in contexts))
    all_started = asyncio.Event()
    release = asyncio.Event()

    class Sessions(_Sessions):
        active = 0

        async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
            del ctx
            self.active += 1
            if self.active == 4:
                all_started.set()
            await release.wait()
            self.active -= 1
            return SessionExecutionResult(kind=SessionExecutionKind.EXECUTED)

    dispatcher = _dispatcher(queue, Sessions())

    assert await dispatcher.poll_once() == 4
    await asyncio.wait_for(all_started.wait(), timeout=0.1)
    release.set()


async def test_busy_resume_is_requeued_only_after_configured_delay() -> None:
    """锁忙 Resume 必须异步退避，不能在处理任务内立即形成重投循环。"""
    resume = ResumeReady(
        **_CONTEXT.model_dump(),
        resume_id="resume-1",
        run_id="run-1",
        resume_token="token-1",
    )
    queue = _Queue((resume,))
    sessions = _Sessions()
    dispatcher = _dispatcher(queue, sessions, resume_requeue_delay_ms=30)

    assert await dispatcher.poll_once() == 1
    async with asyncio.timeout(0.1):
        while not sessions.resumed:
            await asyncio.sleep(0)
    assert queue.requeued == []

    await asyncio.sleep(0.01)
    assert queue.requeued == []
    await asyncio.wait_for(queue.requeued_event.wait(), timeout=0.1)
    assert queue.requeued == [resume]


async def test_empty_dispatcher_waits_until_closed_without_reading() -> None:
    """零有效 Agent 时 Dispatcher 不读取 Redis，也不产生忙循环。"""
    queue = _Queue(())
    dispatcher = _dispatcher(queue, _Sessions(), agent_ids=())

    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(0)
    dispatcher.close()
    await task

    assert queue.read_count == 0


async def test_dispatcher_continues_after_session_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一个 Session 异常不得影响后续 Ready 或终止读取循环。"""
    failed = NormalReady(
        **_CONTEXT.model_copy(update={"session_id": "failed"}).model_dump()
    )
    healthy = NormalReady(
        **_CONTEXT.model_copy(update={"session_id": "healthy"}).model_dump()
    )

    class BatchQueue(_Queue):
        def __init__(self) -> None:
            super().__init__(())
            self.batches = [(failed,), (healthy,)]

        async def read_ready(
            self,
            consumer_id: str,
            *,
            agent_ids: Sequence[str],
            count: int,
            block_ms: int,
        ) -> tuple[ReadyEntry, ...]:
            assert consumer_id == "worker-1"
            assert agent_ids == ("agent",) and count == 1 and block_ms == 10
            self.read_count += 1
            if self.batches:
                return self.batches.pop(0)
            await asyncio.sleep(0.005)
            return ()

    class Sessions(_Sessions):
        dispatcher: TaskDispatcher | None = None

        async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
            if ctx.session_id == "failed":
                private_message = "boom"
                raise RuntimeError(private_message)
            self.normal.append(ctx)
            assert self.dispatcher is not None
            self.dispatcher.close()
            return SessionExecutionResult(kind=SessionExecutionKind.EXECUTED)

    queue = BatchQueue()
    sessions = Sessions()
    dispatcher = _dispatcher(queue, sessions)
    sessions.dispatcher = dispatcher

    await dispatcher.run()

    assert queue.read_count >= 2
    assert [ctx.session_id for ctx in sessions.normal] == ["healthy"]
    assert caplog.text.count("Traceback (most recent call last)") == 1
    assert "operation=dispatcher.session" in caplog.text
    assert "error_id=" in caplog.text
    assert "boom" not in caplog.text


async def test_dispatcher_recovers_after_ready_read_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """短暂 Redis 读取错误不得让仍存活的服务失去 Dispatcher。"""
    ready = NormalReady(**_CONTEXT.model_dump())

    class FailingOnceQueue(_Queue):
        async def read_ready(
            self,
            consumer_id: str,
            *,
            agent_ids: Sequence[str],
            count: int,
            block_ms: int,
        ) -> tuple[ReadyEntry, ...]:
            del consumer_id, agent_ids, count, block_ms
            self.read_count += 1
            if self.read_count == 1:
                private_message = "redis unavailable"
                raise ConnectionError(private_message)
            entries, self.entries = self.entries, ()
            return entries

    class Sessions(_Sessions):
        dispatcher: TaskDispatcher | None = None

        async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
            self.normal.append(ctx)
            assert self.dispatcher is not None
            self.dispatcher.close()
            return SessionExecutionResult(kind=SessionExecutionKind.EXECUTED)

    queue = FailingOnceQueue((ready,))
    sessions = Sessions()
    dispatcher = _dispatcher(queue, sessions)
    sessions.dispatcher = dispatcher

    await asyncio.wait_for(dispatcher.run(), timeout=0.5)

    assert sessions.normal == [_CONTEXT]
    assert "operation=dispatcher.read" in caplog.text
    assert caplog.text.count("Traceback (most recent call last)") == 1
    assert "redis unavailable" not in caplog.text


async def test_close_waits_for_active_session_to_finish() -> None:
    """优雅关闭必须等待已经领取的 Session 自然完成。"""
    ready = NormalReady(**_CONTEXT.model_dump())

    class BlockingQueue(_Queue):
        async def read_ready(
            self,
            consumer_id: str,
            *,
            agent_ids: Sequence[str],
            count: int,
            block_ms: int,
        ) -> tuple[ReadyEntry, ...]:
            del consumer_id, agent_ids, count, block_ms
            self.read_count += 1
            entries, self.entries = self.entries, ()
            if entries:
                return entries
            await asyncio.sleep(0.005)
            return ()

    started = asyncio.Event()
    release = asyncio.Event()

    class Sessions(_Sessions):
        async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
            del ctx
            started.set()
            await release.wait()
            return SessionExecutionResult(kind=SessionExecutionKind.EXECUTED)

    dispatcher = _dispatcher(BlockingQueue((ready,)), Sessions())
    run_task = asyncio.create_task(dispatcher.run())
    await started.wait()
    dispatcher.close()
    await asyncio.sleep(0.01)

    assert run_task.done() is False
    release.set()
    await asyncio.wait_for(run_task, timeout=0.1)


async def test_close_waits_for_delayed_resume_requeue() -> None:
    """优雅关闭必须让已经判定 BUSY 的 Resume 完成延迟重投。"""
    resume = ResumeReady(
        **_CONTEXT.model_dump(),
        resume_id="resume-close",
        run_id="run-close",
        resume_token="token-close",
    )

    class BlockingQueue(_Queue):
        async def read_ready(
            self,
            consumer_id: str,
            *,
            agent_ids: Sequence[str],
            count: int,
            block_ms: int,
        ) -> tuple[ReadyEntry, ...]:
            del consumer_id, agent_ids, count, block_ms
            entries, self.entries = self.entries, ()
            if entries:
                return entries
            await asyncio.sleep(0.005)
            return ()

    queue = BlockingQueue((resume,))
    sessions = _Sessions()
    dispatcher = _dispatcher(queue, sessions, resume_requeue_delay_ms=30)
    run_task = asyncio.create_task(dispatcher.run())
    async with asyncio.timeout(0.1):
        while not sessions.resumed:
            await asyncio.sleep(0)

    assert queue.requeued == []
    dispatcher.close()
    await asyncio.wait_for(run_task, timeout=0.1)
    assert queue.requeued == [resume]


async def test_host_cancellation_cancels_active_session_and_propagates() -> None:
    """宿主强制取消 Dispatcher 时应清理子任务，并保留 CancelledError 语义。"""
    ready = NormalReady(**_CONTEXT.model_dump())

    class BlockingQueue(_Queue):
        async def read_ready(
            self,
            consumer_id: str,
            *,
            agent_ids: Sequence[str],
            count: int,
            block_ms: int,
        ) -> tuple[ReadyEntry, ...]:
            del consumer_id, agent_ids, count, block_ms
            entries, self.entries = self.entries, ()
            if entries:
                return entries
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    started = asyncio.Event()
    child_canceled = asyncio.Event()

    class Sessions(_Sessions):
        async def try_execute(self, ctx: SessionContext) -> SessionExecutionResult:
            del ctx
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_canceled.set()
            raise AssertionError("unreachable")

    dispatcher = _dispatcher(BlockingQueue((ready,)), Sessions())
    run_task = asyncio.create_task(dispatcher.run())
    await started.wait()
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert child_canceled.is_set() is True
