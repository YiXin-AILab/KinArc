"""ScheduleCoordinator 当前分钟扫描测试。"""

import asyncio
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    ReadyEntry,
    ResumeReady,
    ResumeRequest,
)
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.models import ScheduleSessionMode, ScheduleTaskType
from agentsystem.schedule.scheduler import CronSchedule, ScheduleCoordinator
from agentsystem.schedule.service import ScheduleService

_NOW = datetime(2026, 8, 18, 1, 0, 37, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="current-session",
)


class RecordingQueue:
    """记录 Scheduler 通过 Service 投递的消息。"""

    def __init__(self) -> None:
        self.pushed: list[InboundMessage] = []

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        self.pushed.append(message)

    async def push_resume(self, request: ResumeRequest) -> None:
        raise AssertionError(request)

    async def read_ready(
        self,
        consumer_id: str,
        *,
        agent_ids: Sequence[str],
        count: int,
        block_ms: int,
    ) -> tuple[ReadyEntry, ...]:
        raise AssertionError((consumer_id, agent_ids, count, block_ms))

    async def requeue_resume(self, ready: ResumeReady) -> None:
        raise AssertionError(ready)

    async def claim(
        self,
        ctx: SessionContext,
        modes: frozenset[InboundMode],
        *,
        owner_token: str,
    ) -> tuple[InboundMessage, ...]:
        raise AssertionError((ctx, modes, owner_token))

    async def complete(
        self,
        ctx: SessionContext,
        message_ids: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None:
        raise AssertionError((ctx, message_ids, owner_token))

    async def list_pending(self, ctx: SessionContext) -> tuple[InboundMessage, ...]:
        raise AssertionError(ctx)


def _ids(values: tuple[str, ...]) -> Iterator[str]:
    yield from values


async def test_scan_traverses_pages_and_dispatches_each_occurrence_once() -> None:
    """一轮扫描遍历全部页，同分钟重复扫描不重投。"""
    cron = CronSchedule("Asia/Shanghai")
    manager = LocalScheduleManager(cron=cron)
    queue = RecordingQueue()
    service_ids = _ids(("task-a", "task-b", "task-c"))
    service = ScheduleService(
        manager=manager,
        cron=cron,
        clock=lambda: _NOW,
        id_factory=lambda: next(service_ids),
        queue=queue,
        operation_timeout_seconds=1,
    )
    for name in ("A", "B", "C"):
        await service.create_task(
            _CONTEXT,
            name=name,
            prompt=name,
            cron_expression="0 9 * * *",
            session_mode=ScheduleSessionMode.ISOLATED,
        )
    run_ids = _ids(
        (
            "run-a",
            "session-a",
            "run-b",
            "session-b",
            "run-c",
            "session-c",
            "run-a2",
            "session-a2",
            "run-b2",
            "session-b2",
            "run-c2",
            "session-c2",
        )
    )
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return _NOW

    coordinator = ScheduleCoordinator(
        manager=manager,
        cron=cron,
        service=service,
        clock=clock,
        id_factory=lambda: next(run_ids),
        scan_batch_size=1,
        scan_interval_seconds=1,
    )

    await coordinator.scan_once()
    await coordinator.scan_once()

    assert clock_calls == 2
    assert len(queue.pushed) == 3
    assert {message.message_id for message in queue.pushed} == {
        "schedule-run:run-a",
        "schedule-run:run-b",
        "schedule-run:run-c",
    }


async def test_one_time_task_completes_and_is_not_recreated() -> None:
    """one-time Task 首次物化后不再进入扫描。"""
    cron = CronSchedule("Asia/Shanghai")
    manager = LocalScheduleManager(cron=cron)
    queue = RecordingQueue()
    service_ids = _ids(("task-1",))
    service = ScheduleService(
        manager=manager,
        cron=cron,
        clock=lambda: _NOW,
        id_factory=lambda: next(service_ids),
        queue=queue,
        operation_timeout_seconds=1,
    )
    await service.create_task(
        _CONTEXT,
        name="once",
        prompt="once",
        cron_expression="0 9 * * *",
        task_type=ScheduleTaskType.ONE_TIME,
    )
    run_ids = _ids(("run-1", "session-1"))
    coordinator = ScheduleCoordinator(
        manager=manager,
        cron=cron,
        service=service,
        clock=lambda: _NOW,
        id_factory=lambda: next(run_ids),
        scan_batch_size=100,
        scan_interval_seconds=1,
    )

    await coordinator.scan_once()
    await coordinator.scan_once()

    assert len(queue.pushed) == 1


async def test_close_interrupts_scheduler_wait() -> None:
    """close 应立即结束长间隔等待。"""
    cron = CronSchedule("Asia/Shanghai")
    manager = LocalScheduleManager(cron=cron)
    queue = RecordingQueue()
    identifiers = _ids(("unused",))
    service = ScheduleService(
        manager=manager,
        cron=cron,
        clock=lambda: _NOW,
        id_factory=lambda: next(identifiers),
        queue=queue,
        operation_timeout_seconds=1,
    )
    coordinator = ScheduleCoordinator(
        manager=manager,
        cron=cron,
        service=service,
        clock=lambda: _NOW,
        id_factory=lambda: next(identifiers),
        scan_batch_size=100,
        scan_interval_seconds=60,
    )

    task = asyncio.create_task(coordinator.run())
    await asyncio.sleep(0)
    coordinator.close()

    await asyncio.wait_for(task, timeout=1)
