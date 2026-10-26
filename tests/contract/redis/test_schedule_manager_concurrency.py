"""RedisScheduleManager 多实例原子物化测试。"""

import asyncio

from agentsystem.schedule.redis.manager import RedisScheduleManager
from agentsystem.schedule.scheduler import CronSchedule
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix
from tests.contract.schedule_manager_contract import make_run, make_task


async def test_two_managers_materialize_one_occurrence(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """两个节点同时物化时只有一个 was_created 胜者。"""
    cron = CronSchedule("Asia/Shanghai")
    first = RedisScheduleManager(
        redis_client,
        prefix=redis_prefix,
        cron=cron,
        operation_timeout_seconds=1,
    )
    second = RedisScheduleManager(
        redis_client,
        prefix=redis_prefix,
        cron=cron,
        operation_timeout_seconds=1,
    )
    task = make_task(task_id="task-1")
    try:
        await first.create_task(task)
        results = await asyncio.gather(
            first.materialize_scheduled(candidate=task, run=make_run(task)),
            second.materialize_scheduled(candidate=task, run=make_run(task)),
        )
        assert sum(result.was_created for result in results) == 1
        assert len({result.run.schedule_run_id for result in results}) == 1
    finally:
        await delete_prefix(redis_client, redis_prefix)
