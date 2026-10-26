"""真实 Redis Todo 合约。"""

import os

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.todo.redis.store import RedisTodoStore
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from tests.contract.redis.conftest import delete_cluster_prefix, delete_prefix
from tests.contract.todo_store_contract import (
    assert_todo_concurrency,
    assert_todo_contract,
)


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="需要 TEST_REDIS_URL")
async def test_todo_contract(redis_client: Redis, redis_prefix: str) -> None:
    first = RedisTodoStore(redis_client, prefix=redis_prefix, timeout_seconds=5)
    second = RedisTodoStore(redis_client, prefix=redis_prefix, timeout_seconds=5)
    ctx = SessionContext(app_id="contract", user_id="u", agent_id="g", session_id="s")
    try:
        await assert_todo_contract(first, ctx)
        for index in range(10):
            await assert_todo_concurrency(
                first, second, ctx.model_copy(update={"session_id": f"race-{index}"})
            )
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_todo_cluster_contract(
    redis_cluster_client: RedisCluster, redis_prefix: str
) -> None:
    store = RedisTodoStore(redis_cluster_client, prefix=redis_prefix, timeout_seconds=5)
    ctx = SessionContext(app_id="cluster", user_id="u", agent_id="g", session_id="s")
    try:
        await assert_todo_contract(store, ctx)
        await assert_todo_concurrency(store, store, ctx)
    finally:
        await delete_cluster_prefix(redis_cluster_client, redis_prefix)
