"""SqlScheduleManager 真实 MySQL 并发物化测试。"""

import asyncio

from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.sql.manager import SqlScheduleManager
from agentsystem.schedule.sql.schema import schedule_runs, schedule_tasks
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.schedule_manager_contract import make_run, make_task


async def test_two_sql_managers_materialize_one_occurrence(
    mysql_engine: AsyncEngine,
) -> None:
    """两个节点竞争同一 occurrence 时只返回一个物化胜者。"""
    cron = CronSchedule("Asia/Shanghai")
    first = SqlScheduleManager(mysql_engine, cron=cron, operation_timeout_seconds=2)
    second = SqlScheduleManager(mysql_engine, cron=cron, operation_timeout_seconds=2)
    async with mysql_engine.begin() as connection:
        await connection.execute(delete(schedule_runs))
        await connection.execute(delete(schedule_tasks))
    task = make_task()
    try:
        await first.create_task(task)

        results = await asyncio.gather(
            first.materialize_scheduled(candidate=task, run=make_run(task)),
            second.materialize_scheduled(candidate=task, run=make_run(task)),
        )

        assert sum(result.was_created for result in results) == 1
        assert len({result.run.schedule_run_id for result in results}) == 1
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(delete(schedule_runs))
            await connection.execute(delete(schedule_tasks))
