"""真实 Redis 下 Memory 原子提交合约。"""

import os

import pytest
from agentsystem.memory.redis.store import RedisMemoryStore
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from tests.contract.memory_store_contract import (
    assert_concurrent_edits,
    assert_memory_store_contract,
)
from tests.contract.redis.conftest import delete_cluster_prefix, delete_prefix

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_REDIS_URL"), reason="需要 TEST_REDIS_URL"
)


async def test_memory_contract(redis_client: Redis, redis_prefix: str) -> None:
    first = RedisMemoryStore(redis_client, prefix=redis_prefix, timeout_seconds=5)
    second = RedisMemoryStore(redis_client, prefix=redis_prefix, timeout_seconds=5)
    try:
        await assert_memory_store_contract(first, app_id="contract")
        for index in range(20):
            await assert_concurrent_edits(first, second, app_id=f"race-{index}")
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_cluster_memory_contract(
    redis_cluster_client: RedisCluster, redis_prefix: str
) -> None:
    """原生 Cluster 上的 Memory Lua 与并发更新保持原子性。"""
    first = RedisMemoryStore(
        redis_cluster_client, prefix=redis_prefix, timeout_seconds=5
    )
    second = RedisMemoryStore(
        redis_cluster_client, prefix=redis_prefix, timeout_seconds=5
    )
    try:
        await assert_memory_store_contract(first, app_id="contract")
        await assert_concurrent_edits(first, second, app_id="race")
    finally:
        await delete_cluster_prefix(redis_cluster_client, redis_prefix)
