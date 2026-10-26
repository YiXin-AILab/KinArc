"""SqlScheduleManager 真实 MySQL 共享合约。"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from agentsystem.schedule.manager import ScheduleManager
from agentsystem.schedule.models import ScheduleScope, ScheduleTaskQuery
from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.sql.manager import SqlScheduleManager
from agentsystem.schedule.sql.schema import schedule_runs, schedule_tasks
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.schedule_manager_contract import (
    NOW,
    ScheduleManagerContract,
    make_task,
)


class TestSqlScheduleManager(ScheduleManagerContract):
    """在真实 MySQL 上运行共享 ScheduleManager 合约。"""

    async def test_unicode_owner_prefix_and_long_text_round_trip(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """前缀相同、大小写不同的用户仍隔离，长正文及微秒独立列不丢失。"""
        first = make_task(
            user_id="同" * 70 + "A",
            prompt="资料" * 40000,
            created_at=NOW + timedelta(microseconds=123456),
        )
        second = make_task(user_id="同" * 70 + "a")
        await schedule_manager.create_task(first)
        await schedule_manager.create_task(second)
        scope = ScheduleScope(
            app_id=first.app_id, user_id=first.user_id, agent_id=first.agent_id
        )
        assert await schedule_manager.get_task(scope, first.task_id) == first
        assert (
            await schedule_manager.list_tasks(scope, ScheduleTaskQuery())
        ).items == (first,)
        assert await schedule_manager.get_task(scope, second.task_id) is None

    @pytest.fixture
    async def schedule_manager(
        self,
        mysql_engine: AsyncEngine,
    ) -> AsyncIterator[ScheduleManager]:
        async with mysql_engine.begin() as connection:
            await connection.execute(delete(schedule_runs))
            await connection.execute(delete(schedule_tasks))
        manager = SqlScheduleManager(
            mysql_engine,
            cron=CronSchedule("Asia/Shanghai"),
            operation_timeout_seconds=2,
        )
        try:
            yield manager
        finally:
            async with mysql_engine.begin() as connection:
                await connection.execute(delete(schedule_runs))
                await connection.execute(delete(schedule_tasks))
