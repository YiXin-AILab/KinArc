"""测试环境的显式 Redis Standalone 配置工具。"""

import os
from urllib.parse import urlparse

from agentsystem.config.models import (
    ClusterRedisConfig,
    RedisClusterNode,
    StandaloneRedisConfig,
)


def local_redis_config(
    *,
    key_prefix: str = "agent",
    socket_timeout_seconds: float = 5.0,
) -> StandaloneRedisConfig:
    """从测试专用 URL 生成生产模型，不向生产代码保留 URL 兼容。"""
    parsed = urlparse(os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"))
    return StandaloneRedisConfig(
        type="standalone",
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.removeprefix("/") or "0"),
        username=parsed.username,
        password=parsed.password,
        key_prefix=key_prefix,
        socket_timeout_seconds=socket_timeout_seconds,
    )


def cluster_redis_config(
    *,
    key_prefix: str = "agent",
) -> ClusterRedisConfig | None:
    """解析 opt-in Cluster seed；未配置时由调用方决定跳过测试。"""
    raw_nodes = os.environ.get("TEST_REDIS_CLUSTER_NODES")
    if raw_nodes is None:
        return None
    nodes: list[RedisClusterNode] = []
    for raw_node in raw_nodes.split(","):
        host, separator, raw_port = raw_node.strip().rpartition(":")
        if not separator or not host:
            raise ValueError("TEST_REDIS_CLUSTER_NODES 必须使用 host:port 格式")
        nodes.append(RedisClusterNode(host=host, port=int(raw_port)))
    return ClusterRedisConfig(
        type="cluster",
        startup_nodes=tuple(nodes),
        username=os.environ.get("TEST_REDIS_CLUSTER_USERNAME"),
        password=os.environ.get("TEST_REDIS_CLUSTER_PASSWORD"),
        key_prefix=key_prefix,
    )
