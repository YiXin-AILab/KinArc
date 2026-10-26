"""真实 Redis 合约测试的共享连接与隔离 fixture。"""

from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

import pytest
from agentsystem.redis.client import (
    RedisClient,
    await_redis,
    close_redis_client,
    create_redis_client,
)
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from tests.redis_config import cluster_redis_config, local_redis_config


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """连接本机 Docker Redis，并在用例后关闭连接池。"""
    client = cast(Redis, await create_redis_client(local_redis_config()))
    try:
        yield client
    finally:
        await close_redis_client(client)


@pytest.fixture
async def redis_cluster_client() -> AsyncIterator[RedisCluster]:
    """连接显式启用的真实原生 Redis Cluster。"""
    config = cluster_redis_config()
    if config is None:
        pytest.skip("需要 TEST_REDIS_CLUSTER_NODES")
    client = cast(RedisCluster, await create_redis_client(config))
    try:
        yield client
    finally:
        await close_redis_client(client)


@pytest.fixture
def redis_prefix() -> str:
    """为每个测试生成无需清库的唯一 key prefix。"""
    return f"test-agent-{uuid4().hex}"


async def delete_prefix(client: RedisClient, prefix: str) -> None:
    """只清理由当前测试创建的 key。"""
    cursor = 0
    while True:
        result = await await_redis(client.scan(cursor, match=f"{prefix}:*"))
        cursor, keys = cast(tuple[int, list[bytes]], result)
        if keys:
            await await_redis(client.delete(*keys))
        if cursor == 0:
            return


async def delete_cluster_prefix(client: RedisCluster, prefix: str) -> None:
    """遍历全部 Cluster 主节点并逐 key 清理测试 namespace。"""
    async for key in client.scan_iter(match=f"{prefix}:*"):
        await await_redis(client.delete(key))
