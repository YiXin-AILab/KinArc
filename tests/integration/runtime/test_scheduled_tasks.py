"""定时任务单节点业务主链路测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import InboundMessage
from agentsystem.inbox.protocol import Inbox
from agentsystem.message.models import TextBlock
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.models import (
    ScheduleScope,
    ScheduleSessionMode,
    ScheduleTaskPatch,
    ScheduleTaskStatus,
    ScheduleTaskType,
)
from agentsystem.schedule.scheduler import CronSchedule, ScheduleCoordinator
from agentsystem.schedule.service import ScheduleService

_NOW = datetime(2026, 8, 18, 1, 0, 30, tzinfo=UTC)
_CTX = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="current-session",
)


class _RecordingQueue:
    """记录 ScheduleService 的直接 MQ 投递。"""

    def __init__(self) -> None:
        self.messages: list[tuple[SessionContext, InboundMessage]] = []

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        self.messages.append((ctx, message))


def _ids(values: tuple[str, ...]) -> Iterator[str]:
    yield from values


async def test_fixed_isolated_manual_and_one_time_share_one_service_path() -> None:
    """会话策略、手动执行和 one-time 均复用物化后直接投递链路。"""
    cron = CronSchedule("Asia/Shanghai")
    manager = LocalScheduleManager(cron=cron)
    queue = _RecordingQueue()
    task_ids = _ids(("fixed", "isolated", "once", "manual-run", "manual-session"))
    service = ScheduleService(
        manager=manager,
        cron=cron,
        clock=lambda: _NOW,
        id_factory=lambda: next(task_ids),
        queue=cast(Inbox, queue),
        operation_timeout_seconds=1,
    )
    fixed = await service.create_task(
        _CTX,
        name="fixed",
        prompt="fixed prompt",
        cron_expression="0 9 * * *",
        session_mode=ScheduleSessionMode.FIXED,
    )
    isolated = await service.create_task(
        _CTX,
        name="isolated",
        prompt="isolated prompt",
        cron_expression="0 9 * * *",
        session_mode=ScheduleSessionMode.ISOLATED,
    )
    once = await service.create_task(
        _CTX,
        name="once",
        prompt="once prompt",
        cron_expression="0 9 * * *",
        task_type=ScheduleTaskType.ONE_TIME,
        session_mode=ScheduleSessionMode.ISOLATED,
    )
    run_ids = _ids(
        (
            "fixed-run",
            "isolated-run",
            "isolated-session",
            "once-run",
            "once-session",
            "fixed-run-repeat",
            "isolated-run-repeat",
            "isolated-session-repeat",
        )
    )
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

    sessions = {
        cast(TextBlock, message.content[0]).text: ctx.session_id
        for ctx, message in queue.messages
    }
    assert sessions["fixed prompt"] == _CTX.session_id
    assert sessions["isolated prompt"] == "isolated-session"
    assert len(queue.messages) == 3
    stored_once = await manager.get_task(
        _scope(),
        once.task_id,
    )
    assert stored_once is not None
    assert stored_once.status is ScheduleTaskStatus.COMPLETED

    disabled = await service.update_task(
        _CTX,
        task_id=isolated.task_id,
        expected_version=isolated.version,
        patch=ScheduleTaskPatch(status=ScheduleTaskStatus.DISABLED),
    )
    assert disabled.status is ScheduleTaskStatus.DISABLED
    await service.run_now(_CTX, task_id=isolated.task_id, invocation_key="run:tool")
    assert len(queue.messages) == 4
    assert fixed.fixed_session_id == _CTX.session_id


def _scope() -> ScheduleScope:
    return ScheduleScope(
        app_id=_CTX.app_id, user_id=_CTX.user_id, agent_id=_CTX.agent_id
    )
