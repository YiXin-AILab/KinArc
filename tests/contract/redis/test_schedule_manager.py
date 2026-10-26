"""RedisScheduleManager 真实 Redis 合约。"""

from collections.abc import AsyncIterator

import pytest
from agentsystem.schedule.manager import ScheduleManager
from agentsystem.schedule.redis.manager import RedisScheduleManager
from agentsystem.schedule.scheduler import CronSchedule
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from tests.contract.redis.conftest import delete_cluster_prefix, delete_prefix
from tests.contract.schedule_manager_contract import ScheduleManagerContract


class TestRedisScheduleManager(ScheduleManagerContract):
    """在真实 Redis 上运行共享合约。"""

    @pytest.fixture
    async def schedule_manager(
        self,
        redis_client: Redis,
        redis_prefix: str,
    ) -> AsyncIterator[ScheduleManager]:
        manager = RedisScheduleManager(
            redis_client,
            prefix=redis_prefix,
            cron=CronSchedule("Asia/Shanghai"),
            operation_timeout_seconds=1,
        )
        try:
            yield manager
        finally:
            await delete_prefix(redis_client, redis_prefix)


class TestClusterScheduleManager(ScheduleManagerContract):
    """在原生 Cluster 上执行同一组定时任务合约。"""

    @pytest.fixture
    async def schedule_manager(
        self,
        redis_cluster_client: RedisCluster,
        redis_prefix: str,
    ) -> AsyncIterator[ScheduleManager]:
        try:
            yield RedisScheduleManager(
                redis_cluster_client,
                prefix=redis_prefix,
                cron=CronSchedule("Asia/Shanghai"),
                operation_timeout_seconds=1,
            )
        finally:
            await delete_cluster_prefix(redis_cluster_client, redis_prefix)
