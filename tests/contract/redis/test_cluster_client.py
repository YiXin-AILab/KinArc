"""真实原生 Redis Cluster client 合约。"""

import pytest
from agentsystem.config.models import ClusterRedisConfig, RedisClusterNode
from agentsystem.redis.client import await_redis, create_redis_client
from pydantic import SecretStr
from redis.asyncio.cluster import RedisCluster
from redis.crc import key_slot
from redis.exceptions import RedisClusterException
from tests.contract.redis.conftest import delete_cluster_prefix
from tests.redis_config import cluster_redis_config


async def test_cluster_client_discovers_all_primaries_and_routes_slots(
    redis_cluster_client: RedisCluster,
    redis_prefix: str,
) -> None:
    """多个 seed 应发现完整拓扑，并将不同槽 key 路由到对应主节点。"""
    first = f"{redis_prefix}:{{session-a}}:value"
    second = f"{redis_prefix}:{{session-b}}:value"
    assert key_slot(first.encode()) != key_slot(second.encode())

    try:
        await await_redis(redis_cluster_client.set(first, "first"))
        await await_redis(redis_cluster_client.set(second, "second"))

        assert await await_redis(redis_cluster_client.get(first)) == b"first"
        assert await await_redis(redis_cluster_client.get(second)) == b"second"
        primaries = [
            node
            for node in redis_cluster_client.get_nodes()
            if node.server_type == "primary"
        ]
        assert len(primaries) == 3
    finally:
        await delete_cluster_prefix(redis_cluster_client, redis_prefix)


async def test_cluster_authentication_failure_does_not_leak_password() -> None:
    """错误共享凭证应启动失败，且异常文本不得包含原始密码。"""
    config = cluster_redis_config()
    if config is None:
        pytest.skip("需要 TEST_REDIS_CLUSTER_NODES")
    secret = "cluster-test-secret"
    invalid = config.model_copy(update={"password": SecretStr(secret)})

    with pytest.raises(RedisClusterException) as captured:
        await create_redis_client(invalid)

    assert secret not in str(captured.value)


async def test_cluster_config_rejects_standalone_deployment() -> None:
    """Cluster 模式不能把普通 Standalone 节点当作拓扑 seed。"""
    config = ClusterRedisConfig(
        type="cluster",
        startup_nodes=(
            RedisClusterNode(host="127.0.0.1", port=6379),
            RedisClusterNode(host="127.0.0.1", port=6380),
        ),
        socket_timeout_seconds=0.1,
    )

    with pytest.raises(RedisClusterException):
        await create_redis_client(config)
