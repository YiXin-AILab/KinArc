"""Redis Standalone 与 Cluster client 工厂单元测试。"""

from typing import Any, cast

import pytest
from agentsystem.config.models import (
    ClusterRedisConfig,
    RedisClusterNode,
    StandaloneRedisConfig,
)
from agentsystem.redis import client as client_module
from agentsystem.redis.client import RedisClient
from pydantic import SecretStr


class FakeRedis:
    """记录 Standalone client 构造和关闭参数。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.close_connection_pool: bool | None = None
        self.ping_error: BaseException | None = None
        self.cluster_enabled = 0

    async def ping(self) -> bool:
        """模拟可配置的连接检查。"""
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def aclose(self, *, close_connection_pool: bool = False) -> None:
        """记录连接池所有权参数。"""
        self.close_connection_pool = close_connection_pool

    async def info(self, section: str) -> dict[str, int]:
        """返回目标节点的 Cluster 模式标识。"""
        assert section == "cluster"
        return {"cluster_enabled": self.cluster_enabled}


class FakeRedisCluster:
    """记录 Cluster client 构造和无参数关闭。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.is_closed = False
        self.ping_error: BaseException | None = None

    async def ping(self) -> bool:
        """模拟可配置的 Cluster 连接检查。"""
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def aclose(self) -> None:
        """记录 Cluster client 已关闭。"""
        self.is_closed = True


async def test_standalone_factory_maps_explicit_connection_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone 必须映射主机、凭证、DB 和阻塞超时。"""
    monkeypatch.setattr(client_module, "Redis", FakeRedis)
    config = StandaloneRedisConfig(
        type="standalone",
        host="redis.local",
        port=6380,
        database=4,
        username="runtime",
        password=SecretStr("secret"),
        socket_timeout_seconds=2,
    )

    result = await client_module.create_redis_client(config, max_block_ms=250)

    assert isinstance(result, FakeRedis)
    assert "read_from_replicas" not in result.kwargs
    assert "load_balancing_strategy" not in result.kwargs
    assert result.kwargs == {
        "host": "redis.local",
        "port": 6380,
        "db": 4,
        "username": "runtime",
        "password": "secret",
        "decode_responses": False,
        "socket_connect_timeout": 2.0,
        "socket_timeout": 2.25,
    }


async def test_cluster_factory_maps_all_startup_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cluster 必须使用所有 seed、DB 0、主节点读取和完整 slot 覆盖。"""
    monkeypatch.setattr(client_module, "RedisCluster", FakeRedisCluster)
    config = ClusterRedisConfig(
        type="cluster",
        startup_nodes=(
            RedisClusterNode(host="redis-1", port=7000),
            RedisClusterNode(host="redis-2", port=7001),
        ),
        username="runtime",
        password=SecretStr("secret"),
    )

    result = await client_module.create_redis_client(
        config,
        max_connections=17,
        max_block_ms=100,
    )

    assert isinstance(result, FakeRedisCluster)
    startup_nodes = result.kwargs.pop("startup_nodes")
    assert [(node.host, node.port) for node in startup_nodes] == [
        ("redis-1", 7000),
        ("redis-2", 7001),
    ]
    assert result.kwargs == {
        "username": "runtime",
        "password": "secret",
        "db": 0,
        "decode_responses": False,
        "socket_connect_timeout": 5.0,
        "socket_timeout": 5.1,
        "require_full_coverage": True,
        "max_connections": 17,
    }


async def test_cluster_pubsub_client_connects_to_first_seed_as_plain_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async RedisCluster 不支持 Pub/Sub 时使用 Application-owned 节点 client。"""
    monkeypatch.setattr(client_module, "Redis", FakeRedis)
    config = ClusterRedisConfig(
        type="cluster",
        startup_nodes=(
            RedisClusterNode(host="redis-1", port=7000),
            RedisClusterNode(host="redis-2", port=7001),
        ),
        username="runtime",
        password=SecretStr("secret"),
        socket_timeout_seconds=2,
    )

    result = await client_module.create_cluster_node_client(config)

    assert isinstance(result, FakeRedis)
    assert result.kwargs == {
        "host": "redis-1",
        "port": 7000,
        "db": 0,
        "username": "runtime",
        "password": "secret",
        "decode_responses": False,
        "socket_connect_timeout": 2.0,
        "socket_timeout": 2.0,
    }


async def test_standalone_factory_rejects_cluster_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone 配置指向 Cluster 节点时应在启动阶段失败。"""
    created: list[FakeRedis] = []

    class ClusterNodeRedis(FakeRedis):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.cluster_enabled = 1
            created.append(self)

    monkeypatch.setattr(client_module, "Redis", ClusterNodeRedis)
    config = StandaloneRedisConfig(type="standalone", host="cluster-node")

    with pytest.raises(ValueError, match="Standalone.*Cluster"):
        await client_module.create_redis_client(config)

    assert created[0].close_connection_pool is True


@pytest.mark.parametrize("is_cluster", [False, True])
async def test_failed_ping_closes_partially_created_client(
    monkeypatch: pytest.MonkeyPatch,
    is_cluster: bool,
) -> None:
    """PING 失败必须按 client 类型关闭其连接资源并保留原异常。"""
    fake_type: type[FakeRedis] | type[FakeRedisCluster]
    config: StandaloneRedisConfig | ClusterRedisConfig
    if is_cluster:
        fake_type = FakeRedisCluster
        monkeypatch.setattr(client_module, "RedisCluster", fake_type)
        config = ClusterRedisConfig(
            type="cluster",
            startup_nodes=(
                RedisClusterNode(host="redis-1", port=7000),
                RedisClusterNode(host="redis-2", port=7001),
            ),
        )
    else:
        fake_type = FakeRedis
        monkeypatch.setattr(client_module, "Redis", fake_type)
        config = StandaloneRedisConfig(type="standalone", host="localhost")

    failure = ConnectionError("ping failed")
    created: list[FakeRedis | FakeRedisCluster] = []
    original_init = fake_type.__init__

    def capture_init(self: Any, **kwargs: object) -> None:
        original_init(self, **kwargs)
        self.ping_error = failure
        created.append(self)

    monkeypatch.setattr(fake_type, "__init__", capture_init)

    with pytest.raises(ConnectionError, match="ping failed") as captured:
        await client_module.create_redis_client(config)

    assert captured.value is failure
    assert len(created) == 1
    if is_cluster:
        assert isinstance(created[0], FakeRedisCluster)
        assert created[0].is_closed is True
    else:
        assert isinstance(created[0], FakeRedis)
        assert created[0].close_connection_pool is True


async def test_close_helper_uses_each_client_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一关闭入口必须适配两种 redis-py aclose 签名。"""
    monkeypatch.setattr(client_module, "RedisCluster", FakeRedisCluster)
    standalone = FakeRedis()
    cluster = FakeRedisCluster()

    await client_module.close_redis_client(cast(RedisClient, standalone))
    await client_module.close_redis_client(cast(RedisClient, cluster))

    assert standalone.close_connection_pool is True
    assert cluster.is_closed is True
