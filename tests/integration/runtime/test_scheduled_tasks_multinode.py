"""定时任务多节点扫描、物化和投递边界测试。"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import InboundMessage
from agentsystem.inbox.protocol import Inbox
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.schedule.models import ScheduleRunQuery, ScheduleScope
from agentsystem.schedule.redis.manager import RedisScheduleManager
from agentsystem.schedule.scheduler import CronSchedule, ScheduleCoordinator
from agentsystem.schedule.service import ScheduleService
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix
from tests.redis_config import local_redis_config

_NOW = datetime(2026, 8, 18, 1, 0, 30, tzinfo=UTC)
_CTX = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


class _RecordingQueue:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        del ctx, message
        self.calls += 1
        if self.fail:
            raise RuntimeError("queue unavailable")


def _ids(values: tuple[str, ...]) -> Iterator[str]:
    yield from values


async def test_redundant_scanners_only_winner_pushes_and_failure_is_not_retried() -> (
    None
):
    """唯一物化结果控制投递；胜者投递失败仍不由其他节点接管。"""
    prefix = f"schedule-multinode-{uuid4().hex}"
    redis = cast(Redis, await create_redis_client(local_redis_config()))
    cron = CronSchedule("Asia/Shanghai")
    first = RedisScheduleManager(
        redis, prefix=prefix, cron=cron, operation_timeout_seconds=1
    )
    second = RedisScheduleManager(
        redis, prefix=prefix, cron=cron, operation_timeout_seconds=1
    )
    queue = _RecordingQueue(fail=True)
    first_ids = _ids(("task", "run-a", "session-a"))
    second_ids = _ids(("run-b", "session-b"))
    first_service = _service(first, queue, first_ids)
    second_service = _service(second, queue, second_ids)
    try:
        await first_service.create_task(
            _CTX,
            name="日报",
            prompt="生成日报",
            cron_expression="0 9 * * *",
        )
        coordinators = (
            _coordinator(first, first_service, first_ids),
            _coordinator(second, second_service, second_ids),
        )

        await asyncio.gather(*(item.scan_once() for item in coordinators))

        assert queue.calls == 1
        page = await first.list_runs(
            ScheduleScope(app_id="app", user_id="user", agent_id="agent"),
            ScheduleRunQuery(),
        )
        assert len(page.items) == 1
        assert page.items[0].dispatch_error_code == "schedule_dispatch_failed"
    finally:
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)


def _service(
    manager: RedisScheduleManager,
    queue: _RecordingQueue,
    identifiers: Iterator[str],
) -> ScheduleService:
    return ScheduleService(
        manager=manager,
        cron=CronSchedule("Asia/Shanghai"),
        clock=lambda: _NOW,
        id_factory=lambda: next(identifiers),
        queue=cast(Inbox, queue),
        operation_timeout_seconds=1,
    )


def _coordinator(
    manager: RedisScheduleManager,
    service: ScheduleService,
    identifiers: Iterator[str],
) -> ScheduleCoordinator:
    return ScheduleCoordinator(
        manager=manager,
        cron=CronSchedule("Asia/Shanghai"),
        service=service,
        clock=lambda: _NOW,
        id_factory=lambda: next(identifiers),
        scan_batch_size=100,
        scan_interval_seconds=1,
    )
