"""持续消费共享 Ready Stream、独立调度每个 Session 的 Dispatcher。"""

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from contextlib import AbstractContextManager

from agentsystem.common.logging import bind_log_context, log_unexpected_exception
from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import NormalReady, ReadyEntry, ResumeReady
from agentsystem.inbox.protocol import Inbox
from agentsystem.runtime.session import SessionExecutionKind
from agentsystem.service.agents import AgentRegistry

LOGGER = logging.getLogger(__name__)
_READ_COUNT_PER_STREAM = 1
_READ_ERROR_BACKOFF_SECONDS = 0.1


class TaskDispatcher:
    """消费共享 Ready 并为各 Session 创建独立任务的节点级调度器。

    依赖 Inbox 和已装配 AgentRegistry，不自行限制本地并发数。普通 Ready
    锁忙不重投，Resume Ready 锁忙才延迟重投；close 请求停止读取，run 负责排空。
    """

    def __init__(
        self,
        *,
        inbox: Inbox,
        agents: AgentRegistry,
        agent_ids: Sequence[str],
        consumer_id: str,
        ready_block_ms: int,
        resume_requeue_delay_ms: int,
    ) -> None:
        """配置消费范围与退避时间，尚不启动读取任务。

        Args:
            inbox: Ready 读取与 Resume 重投端口。
            agents: Agent 标识到 Session 入口的只读目录。
            agent_ids: 本节点消费的 Agent 范围，构造时去重并排序。
            consumer_id: Ready 消费者身份。
            ready_block_ms: 单次读取的正数阻塞毫秒数。
            resume_requeue_delay_ms: Resume 锁忙后的非负退避毫秒数。

        Raises:
            ValueError: 消费身份、Agent 身份或时间参数无效。
        """
        if not consumer_id or ready_block_ms <= 0 or resume_requeue_delay_ms < 0:
            raise ValueError("Dispatcher consumer、block 和 Resume delay 必须有效")
        self._inbox = inbox
        self._agents = agents
        self._agent_ids = tuple(sorted(set(agent_ids)))
        if any(not agent_id.strip() for agent_id in self._agent_ids):
            raise ValueError("Dispatcher agent_id 不能为空")
        self._consumer_id = consumer_id
        self._block_ms = ready_block_ms
        self._resume_requeue_delay_seconds = resume_requeue_delay_ms / 1_000
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._resume_retry_tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    async def poll_once(self) -> int:
        """有限读取一批 Ready，为每条结果安排独立执行任务。

        Returns:
            本次读取的条目数；无 Agent 或已请求停止时为零。

        本方法不等待 Session 执行完成；读取异常直接传播给调用方。
        """
        if not self._agent_ids or self._stopping.is_set():
            return 0
        entries = await self._inbox.read_ready(
            self._consumer_id,
            agent_ids=self._agent_ids,
            count=_READ_COUNT_PER_STREAM,
            block_ms=self._block_ms,
        )
        for entry in entries:
            self._track_task(
                self._handle_safely(entry),
                tasks=self._active_tasks,
                name=_task_name("ready", entry),
            )
        return len(entries)

    async def run(self) -> None:
        """持续读取 Ready；正常关闭排空任务，宿主取消则取消子任务。"""
        if not self._agent_ids:
            await self._stopping.wait()
            return
        try:
            await self._read_until_stopping()
            await self._drain_tracked_tasks()
        except asyncio.CancelledError:
            self._cancel_tracked_tasks()
            await self._drain_tracked_tasks()
            raise

    def close(self) -> None:
        """停止开始新的读取迭代，并请求优雅排空已领取任务。"""
        self._stopping.set()

    async def _read_until_stopping(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log_exception(
                    error,
                    operation="dispatcher.read",
                    consumer_id=self._consumer_id,
                )
                await self._wait_read_backoff()

    async def _wait_read_backoff(self) -> None:
        try:
            await asyncio.wait_for(
                self._stopping.wait(),
                timeout=_READ_ERROR_BACKOFF_SECONDS,
            )
        except TimeoutError:
            return

    async def _handle_safely(self, entry: ReadyEntry) -> None:
        """隔离单条 Ready 的普通异常，避免影响其他 Session 和读取循环。"""
        with _ready_log_context(entry):
            try:
                session = self._agents.require(entry.agent_id).session
                if isinstance(entry, NormalReady):
                    await session.try_execute(_context_from_ready(entry))
                    return
                result = await session.try_resume(entry)
                if result.kind is SessionExecutionKind.BUSY:
                    self._schedule_resume_retry(entry)
            except Exception as error:
                _log_exception(
                    error,
                    operation="dispatcher.session",
                )

    def _schedule_resume_retry(self, ready: ResumeReady) -> None:
        self._track_task(
            self._requeue_resume_after_delay(ready),
            tasks=self._resume_retry_tasks,
            name=_task_name("resume-retry", ready),
        )

    async def _requeue_resume_after_delay(self, ready: ResumeReady) -> None:
        with _ready_log_context(ready):
            try:
                await asyncio.sleep(self._resume_requeue_delay_seconds)
                await self._inbox.requeue_resume(ready)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log_exception(
                    error,
                    operation="dispatcher.resume_requeue",
                )

    def _track_task(
        self,
        coroutine: Coroutine[object, object, None],
        *,
        tasks: set[asyncio.Task[None]],
        name: str,
    ) -> None:
        task = asyncio.create_task(coroutine, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _drain_tracked_tasks(self) -> None:
        while tasks := tuple(self._active_tasks | self._resume_retry_tasks):
            await asyncio.gather(*tasks, return_exceptions=True)
            self._active_tasks.difference_update(tasks)
            self._resume_retry_tasks.difference_update(tasks)

    def _cancel_tracked_tasks(self) -> None:
        for task in self._active_tasks | self._resume_retry_tasks:
            task.cancel()


def _context_from_ready(ready: ReadyEntry) -> SessionContext:
    return SessionContext(
        app_id=ready.app_id,
        user_id=ready.user_id,
        agent_id=ready.agent_id,
        session_id=ready.session_id,
    )


def _task_name(kind: str, ready: ReadyEntry) -> str:
    return f"agentsystem-{kind}:{ready.agent_id}:{ready.session_id}"


def _log_exception(error: Exception, *, operation: str, **scope: str) -> None:
    """在 Dispatcher 最终所有权边界保存一次脱敏完整栈。"""
    log_unexpected_exception(
        LOGGER,
        "dispatcher_failed",
        error,
        operation=operation,
        **scope,
    )


def _ready_log_context(ready: ReadyEntry) -> AbstractContextManager[None]:
    """绑定一条 Ready 携带的安全业务标识。"""
    return bind_log_context(
        app_id=ready.app_id,
        user_id=ready.user_id,
        agent_id=ready.agent_id,
        session_id=ready.session_id,
    )
