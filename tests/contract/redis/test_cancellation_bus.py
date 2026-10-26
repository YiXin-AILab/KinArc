"""真实 Redis Pub/Sub 分布式取消合约测试。"""

import asyncio
from datetime import UTC, datetime

import pytest
from agentsystem.cancellation.models import CancellationCommand
from agentsystem.cancellation.redis_bus import RedisCancellationBus
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.models import SessionContext
from agentsystem.redis.client import (
    close_redis_client,
    create_cluster_node_client,
)
from redis.asyncio import Redis
from tests.redis_config import cluster_redis_config

_CTX = SessionContext(
    app_id="app",
    agent_id="agent",
    user_id="user",
    session_id="session",
)


async def test_classic_pubsub_broadcast_reaches_every_runtime_node(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """所有在线 Worker 收到广播，但只有精确活动 run 会被取消。"""
    first_registry = RunTaskRegistry()
    second_registry = RunTaskRegistry()
    first_task = asyncio.create_task(asyncio.Event().wait())
    second_task = asyncio.create_task(asyncio.Event().wait())
    first_registry.register(_CTX, "run", first_task)
    second_registry.register(_CTX, "run", second_task)
    first_bus = RedisCancellationBus(
        redis_client,
        prefix=redis_prefix,
        registry=first_registry,
    )
    second_bus = RedisCancellationBus(
        redis_client,
        prefix=redis_prefix,
        registry=second_registry,
    )
    first_subscriber = asyncio.create_task(first_bus.run())
    second_subscriber = asyncio.create_task(second_bus.run())
    await asyncio.gather(first_bus.wait_ready(), second_bus.wait_ready())

    receivers = await first_bus.publish(
        CancellationCommand(
            **_CTX.model_dump(),
            run_id="run",
            requested_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
    )

    assert receivers >= 2
    results = await asyncio.wait_for(
        asyncio.gather(first_task, second_task, return_exceptions=True),
        timeout=1,
    )
    assert all(isinstance(result, asyncio.CancelledError) for result in results)

    await asyncio.gather(first_bus.close(), second_bus.close())
    await asyncio.wait_for(
        asyncio.gather(first_subscriber, second_subscriber),
        timeout=1,
    )


async def test_cluster_classic_pubsub_propagates_between_seed_nodes() -> None:
    """不同 Cluster 节点上的普通 client 必须收到同一个经典 Pub/Sub 广播。"""
    config = cluster_redis_config(key_prefix="cluster-cancel")
    if config is None:
        pytest.skip("需要 TEST_REDIS_CLUSTER_NODES")
    reversed_config = config.model_copy(
        update={"startup_nodes": tuple(reversed(config.startup_nodes))}
    )
    first_client = await create_cluster_node_client(config)
    second_client = await create_cluster_node_client(reversed_config)
    first_registry = RunTaskRegistry()
    second_registry = RunTaskRegistry()
    first_task = asyncio.create_task(asyncio.Event().wait())
    second_task = asyncio.create_task(asyncio.Event().wait())
    first_registry.register(_CTX, "cluster-run", first_task)
    second_registry.register(_CTX, "cluster-run", second_task)
    first_bus = RedisCancellationBus(
        first_client,
        prefix="cluster-cancel",
        registry=first_registry,
    )
    second_bus = RedisCancellationBus(
        second_client,
        prefix="cluster-cancel",
        registry=second_registry,
    )
    subscribers = (
        asyncio.create_task(first_bus.run()),
        asyncio.create_task(second_bus.run()),
    )
    try:
        await asyncio.gather(first_bus.wait_ready(), second_bus.wait_ready())
        receivers = await first_bus.publish(
            CancellationCommand(
                **_CTX.model_dump(),
                run_id="cluster-run",
                requested_at=datetime(2026, 8, 13, tzinfo=UTC),
            )
        )
        assert receivers >= 1
        results = await asyncio.wait_for(
            asyncio.gather(first_task, second_task, return_exceptions=True),
            timeout=1,
        )
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
    finally:
        await asyncio.gather(first_bus.close(), second_bus.close())
        await asyncio.gather(*subscribers, return_exceptions=True)
        await asyncio.gather(
            close_redis_client(first_client),
            close_redis_client(second_client),
        )
