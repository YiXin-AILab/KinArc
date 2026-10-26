"""Evolution Run 生命周期：进程内单 Run 注册表、独立线程执行与日志事件进度。

每个 Run 在独立 daemon 线程中运行自己的 asyncio loop，避免
``CandidateRepository`` 的同步文件 I/O 阻塞 Server 事件循环。
进度事件来自挂到 ``evolution`` logger 的日志 Handler（引擎无回调钩子），
解析 key=value 文本协议为结构化 ``ProgressEvent``；V1 全局只允许一个
活跃 Run（日志无 run 路由键，多 Run 无法归属事件）。
"""

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from agentsystem.common.logging import log_unexpected_exception
from agentsystem.config.models import RuntimeConfig
from evolution.agentsystem_improver import AgentSystemImprover
from evolution.candidates import CandidateRepository
from evolution.engine import EvolutionEngine
from evolution.improver import Improver
from evolution.models import OptimizationResult
from evolution.task import ResolvedTaskConfig, load_task, load_task_config

from server.platform.evolution.store import EvolutionStore

LOGGER = logging.getLogger(__name__)

_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TERMINAL_EVENTS = frozenset({"run_completed", "run_failed", "run_stopped"})


class RunStatus(StrEnum):
    """一次 Evolution Run 的生命周期状态。"""

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """一条结构化进度事件；cursor 在单个 Run 内单调递增。"""

    cursor: int
    ts: float
    event: str
    message: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RunView:
    """Run 状态的只读快照。"""

    run_id: str
    task_id: str
    status: RunStatus
    started_at: float
    finished_at: float | None
    stop_reason: str | None
    error: str | None
    best_candidate_id: str | None
    validation_score: float | None
    event_count: int


@dataclass(slots=True)
class _RunHandle:
    run_id: str
    task_id: str
    task_dir: Path
    status: RunStatus = RunStatus.STARTING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    result: OptimizationResult | None = None
    cursor: int = 0
    buffer: deque[ProgressEvent] = field(default_factory=deque)
    subscribers: list[
        tuple[asyncio.AbstractEventLoop, asyncio.Queue[ProgressEvent | None]]
    ] = field(default_factory=list)
    loop: asyncio.AbstractEventLoop | None = None
    engine_task: asyncio.Task[OptimizationResult] | None = None
    log_handler: logging.Handler | None = None
    log_prev_level: int | None = None
    stop_requested: bool = False


type ImproverFactory = Callable[
    [ResolvedTaskConfig],
    Awaitable[AbstractAsyncContextManager[Improver]],
]


def parse_progress_message(message: str) -> tuple[str, dict[str, str]]:
    """把 ``event key=value ...`` 文本协议解析为事件名与字段。

    不符合协议的消息（如多词警告文本）原样透传为 ``log`` 事件。
    """
    tokens = message.split()
    if (
        len(tokens) >= 2
        and _EVENT_NAME_RE.fullmatch(tokens[0])
        and all("=" in token for token in tokens[1:])
    ):
        fields = {}
        for token in tokens[1:]:
            key, _, value = token.partition("=")
            fields[key] = value
        return tokens[0], fields
    return "log", {}


class _ProgressLogHandler(logging.Handler):
    """run 期间挂在 evolution logger 上的只观察 Handler（线程安全）。"""

    def __init__(self, registry: "EvolutionRunRegistry", handle: _RunHandle) -> None:
        super().__init__(level=logging.INFO)
        self._registry = registry
        self._handle = handle

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - Handler 不得向 logging 框架抛出异常
            return
        event, fields = parse_progress_message(message)
        self._registry.append_event(self._handle, event, message, fields)


class EvolutionRunRegistry:
    """进程内 Evolution Run 注册表；V1 全局单活跃 Run。"""

    def __init__(
        self,
        *,
        project_root: Path,
        store: EvolutionStore,
        runtime_config: RuntimeConfig | None = None,
        environ: Mapping[str, str] | None = None,
        buffer_size: int = 2000,
        improver_factory: ImproverFactory | None = None,
    ) -> None:
        """绑定自进化依赖，初始化运行注册表和事件缓冲配置。

        Args:
            project_root: 项目根目录，用于解析项目资源路径。
            store: 自进化任务与候选产物存储。
            runtime_config: 可选 Runtime 配置，传递给自进化执行链路。
            environ: 用于配置环境变量插值的映射。省略时使用进程环境。
            buffer_size: 单次运行最多保留的进度事件数量。
            improver_factory: 可注入的改进器工厂，省略时使用默认工厂。
        """
        self._project_root = project_root
        self._store = store
        self._runtime_config = runtime_config
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )
        self._buffer_size = buffer_size
        self._improver_factory = improver_factory or self._default_improver_factory
        self._lock = threading.Lock()
        self._runs: dict[str, _RunHandle] = {}

    # ---- 查询 ----

    def get(self, run_id: str) -> RunView:
        """返回指定 Run 的快照；不存在时抛 KeyError（由路由层映射 404）。"""
        return self._view(self._handle(run_id))

    def list_runs(self) -> tuple[RunView, ...]:
        """按启动顺序返回全部 Run 快照。"""
        with self._lock:
            handles = list(self._runs.values())
        return tuple(self._view(handle) for handle in handles)

    def active_run(self) -> RunView | None:
        """返回当前活跃 Run；没有时返回 None。"""
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if handle.status in (RunStatus.STARTING, RunStatus.RUNNING):
                return self._view(handle)
        return None

    def events_after(self, run_id: str, cursor: int) -> tuple[ProgressEvent, ...]:
        """返回 cursor 之后的缓冲事件（用于 SSE 首次回放）。

        Args:
            run_id: 运行标识。
            cursor: 已接收事件的游标，只返回其后的事件。

        Returns:
            缓冲区内游标严格大于输入游标的事件，按原顺序返回。
        """
        handle = self._handle(run_id)
        with self._lock:
            return tuple(event for event in handle.buffer if event.cursor > cursor)

    def is_terminal(self, run_id: str) -> bool:
        """判断 Run 是否已进入终态。"""
        return self._handle(run_id).status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.STOPPED,
        )

    # ---- 生命周期 ----

    def start(self, task_id: str) -> RunView:
        """启动一个 Run；已有活跃 Run 时抛 RuntimeError（路由层映射 409）。"""
        task_dir = self._store.task_dir(task_id)
        with self._lock:
            for handle in self._runs.values():
                if handle.status in (RunStatus.STARTING, RunStatus.RUNNING):
                    raise RuntimeError(f"已有运行中的进化任务：{handle.task_id}")
            run_id = f"run-{uuid.uuid4().hex[:12]}"
            handle = _RunHandle(
                run_id=run_id,
                task_id=task_id,
                task_dir=task_dir,
                buffer=deque(maxlen=self._buffer_size),
            )
            self._runs[run_id] = handle
        log_handler = _ProgressLogHandler(self, handle)
        evolution_logger = logging.getLogger("evolution")
        evolution_logger.addHandler(log_handler)
        handle.log_handler = log_handler
        if evolution_logger.getEffectiveLevel() > logging.INFO:
            handle.log_prev_level = evolution_logger.level
            evolution_logger.setLevel(logging.INFO)
        thread = threading.Thread(
            target=self._thread_main,
            args=(handle,),
            name=f"evolution-{run_id}",
            daemon=True,
        )
        thread.start()
        return self._view(handle)

    def stop(self, run_id: str) -> RunView:
        """请求协作式停止；已终态时抛 RuntimeError（路由层映射 409）。"""
        handle = self._handle(run_id)
        with self._lock:
            if handle.status not in (RunStatus.STARTING, RunStatus.RUNNING):
                raise RuntimeError(f"Run 已结束：{run_id}")
            handle.stop_requested = True
            loop = handle.loop
            engine_task = handle.engine_task
        if loop is not None and engine_task is not None:
            loop.call_soon_threadsafe(engine_task.cancel)
        return self._view(handle)

    # ---- SSE 订阅 ----

    def subscribe(
        self,
        run_id: str,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Queue[ProgressEvent | None]:
        """注册一个订阅队列；终态时会收到 None 哨兵。

        Args:
            run_id: 运行标识。
            loop: 订阅队列所属的事件循环，用于跨线程投递。

        Returns:
            新注册的订阅队列；后续终态通知使用 None 作为结束哨兵。
        """
        handle = self._handle(run_id)
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        with self._lock:
            handle.subscribers.append((loop, queue))
        return queue

    def unsubscribe(
        self,
        run_id: str,
        queue: asyncio.Queue[ProgressEvent | None],
    ) -> None:
        """移除订阅队列（幂等）。

        Args:
            run_id: 运行标识。
            queue: 此前注册的订阅队列。
        """
        handle = self._handle(run_id)
        with self._lock:
            handle.subscribers[:] = [
                (loop, item) for loop, item in handle.subscribers if item is not queue
            ]

    def append_event(
        self,
        handle: _RunHandle,
        event: str,
        message: str,
        fields: Mapping[str, str],
    ) -> None:
        """追加事件到缓冲区并跨线程分发给订阅者。

        Args:
            handle: 目标运行的内部句柄。
            event: 进度事件名称。
            message: 进度事件的消息正文。
            fields: 进度事件的结构化字段。
        """
        with self._lock:
            handle.cursor += 1
            progress = ProgressEvent(
                cursor=handle.cursor,
                ts=time.time(),
                event=event,
                message=message,
                fields=dict(fields),
            )
            handle.buffer.append(progress)
            subscribers = list(handle.subscribers)
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(queue.put_nowait, progress)

    # ---- 内部 ----

    def _handle(self, run_id: str) -> _RunHandle:
        with self._lock:
            handle = self._runs.get(run_id)
        if handle is None:
            raise KeyError(run_id)
        return handle

    def _view(self, handle: _RunHandle) -> RunView:
        return RunView(
            run_id=handle.run_id,
            task_id=handle.task_id,
            status=handle.status,
            started_at=handle.started_at,
            finished_at=handle.finished_at,
            stop_reason=handle.result.stop_reason.value if handle.result else None,
            error=handle.error,
            best_candidate_id=(
                handle.result.best_candidate_id if handle.result else None
            ),
            validation_score=(
                handle.result.validation_score if handle.result else None
            ),
            event_count=handle.cursor,
        )

    def _thread_main(self, handle: _RunHandle) -> None:
        try:
            asyncio.run(self._execute(handle))
        except asyncio.CancelledError:
            # 用户请求停止：CancelledError 作为控制流异常继续传播到线程边界。
            handle.status = RunStatus.STOPPED
            self.append_event(handle, "run_stopped", "run_stopped reason=user", {})
        except Exception as error:
            handle.status = RunStatus.FAILED
            handle.error = str(error)
            log_unexpected_exception(
                LOGGER,
                "evolution_run_failed",
                error,
                operation="evolution.run",
                run_id=handle.run_id,
                task_id=handle.task_id,
            )
            self.append_event(
                handle,
                "run_failed",
                f"run_failed error={type(error).__name__}",
                {"error": str(error)},
            )
        finally:
            handle.finished_at = time.time()
            log_handler = handle.log_handler
            if log_handler is not None:
                evolution_logger = logging.getLogger("evolution")
                evolution_logger.removeHandler(log_handler)
                if handle.log_prev_level is not None:
                    evolution_logger.setLevel(handle.log_prev_level)
            with self._lock:
                subscribers = list(handle.subscribers)
            for loop, queue in subscribers:
                loop.call_soon_threadsafe(queue.put_nowait, None)

    async def _execute(self, handle: _RunHandle) -> None:
        handle.loop = asyncio.get_running_loop()
        handle.status = RunStatus.RUNNING
        config = load_task_config(handle.task_dir, environ=self._environ)
        task = load_task(config)
        repository = CandidateRepository(
            config.root,
            workspace_root=self._project_root / ".evolution",
            initial_target=config.initial_target,
            objective=config.objective,
            read_only=config.workspace.read_only,
            inaccessible=config.workspace.inaccessible,
        )
        async with await self._improver_factory(config) as improver:
            engine = EvolutionEngine(
                repository=repository,
                task=task,
                improver=improver,
                objective=config.objective,
                read_only=config.workspace.read_only,
                inaccessible=config.workspace.inaccessible,
                max_iterations=config.max_iterations,
                max_retries=config.max_retries,
                max_case_concurrency=config.max_case_concurrency,
                retry_delay_seconds=config.retry_delay_seconds,
                rollback_enabled=config.rollback_enabled,
                exploration_patience=config.exploration_patience,
                rollback_changelog_source=config.rollback_changelog_source,
                single_case_min_cases=config.single_case_min_cases,
            )
            if handle.stop_requested:
                raise asyncio.CancelledError
            handle.engine_task = asyncio.current_task()
            handle.result = await engine.run()
            handle.status = RunStatus.COMPLETED

    async def _default_improver_factory(
        self,
        config: ResolvedTaskConfig,
    ) -> AgentSystemImprover:
        if self._runtime_config is None:
            raise RuntimeError("未配置 runtime_config，无法创建默认 Improver")
        return await AgentSystemImprover.create(
            runtime_config=self._runtime_config,
            project_root=self._project_root,
            redis_prefix=_redis_prefix(config.root),
            request_timeout_seconds=config.improver_timeout_seconds,
            environ=self._environ,
        )


def _redis_prefix(experiment_root: Path) -> str:
    digest = hashlib.sha256(str(experiment_root).encode()).hexdigest()[:16]
    return f"evolution:{digest}"


__all__ = [
    "EvolutionRunRegistry",
    "ImproverFactory",
    "ProgressEvent",
    "RunStatus",
    "RunView",
    "parse_progress_message",
]
