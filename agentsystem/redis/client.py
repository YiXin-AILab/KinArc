"""redis-py asyncio client 的创建与启动期连接验证。"""

from collections.abc import Awaitable, Mapping
from contextlib import suppress
from typing import NotRequired, TypedDict, cast

from redis.asyncio import Redis, RedisCluster
from redis.asyncio.cluster import ClusterNode

from agentsystem.config.models import (
    ClusterRedisConfig,
    RuntimeRedisConfig,
    StandaloneRedisConfig,
)

type RedisClient = Redis | RedisCluster


class _CommonClientOptions(TypedDict):
    """描述两种异步 client 构造器共用的连接选项，不持有连接资源。

    Attributes:
        username: 可选认证用户名。
        password: 可选认证密码，仅传递给 client 构造器。
        decode_responses: 是否解码响应；本模块使用 bytes 响应。
        socket_connect_timeout: 建连超时秒数。
        socket_timeout: 命令 socket 超时秒数，包含阻塞读取余量。
        max_connections: 可选连接池上限。
    """

    username: str | None
    password: str | None
    decode_responses: bool
    socket_connect_timeout: float
    socket_timeout: float
    max_connections: NotRequired[int]


async def create_redis_client(
    config: RuntimeRedisConfig,
    *,
    max_connections: int | None = None,
    max_block_ms: int = 0,
) -> RedisClient:
    """创建拥有连接池的 Redis client，并在返回前完成一次 ping。

    启动校验失败时先尝试释放连接资源，再原样传播异常或取消。

    Args:
        config: Redis 连接配置。
        max_connections: 连接池上限；`None` 使用 redis-py 默认值。
        max_block_ms: 此 client 执行的最长 Redis 阻塞命令窗口。

    Returns:
        已完成连接验证的异步 Redis client，由调用方负责关闭。

    Raises:
        ValueError: max_block_ms 为负数，或 Standalone 配置指向 Cluster。
        TypeError: 配置不属于支持的 Redis 模式。
    """
    if max_block_ms < 0:
        raise ValueError("max_block_ms 不能为负数")
    blocking_slack_seconds = max(config.socket_timeout_seconds, 1.0)
    socket_timeout_seconds = max_block_ms / 1_000 + blocking_slack_seconds
    password = (
        config.password.get_secret_value() if config.password is not None else None
    )
    common_options: _CommonClientOptions = {
        "username": config.username,
        "password": password,
        "decode_responses": False,
        "socket_connect_timeout": config.socket_timeout_seconds,
        "socket_timeout": socket_timeout_seconds,
    }
    if max_connections is not None:
        common_options["max_connections"] = max_connections
    if isinstance(config, StandaloneRedisConfig):
        client: RedisClient = Redis(
            host=config.host,
            port=config.port,
            db=config.database,
            **common_options,
        )
    elif isinstance(config, ClusterRedisConfig):
        client = RedisCluster(
            startup_nodes=[
                ClusterNode(node.host, node.port) for node in config.startup_nodes
            ],
            db=0,
            require_full_coverage=True,
            **common_options,
        )
    else:
        raise TypeError(f"不支持的 Redis 配置类型: {type(config).__name__}")
    try:
        await cast(Awaitable[bool], client.ping())
        if isinstance(config, StandaloneRedisConfig):
            cluster_info = cast(
                Mapping[str, object],
                await await_redis(client.info("cluster")),
            )
            if cluster_info.get("cluster_enabled") in (1, "1", b"1", True):
                raise ValueError("Redis Standalone 配置指向了 Cluster 节点")
    except BaseException:
        with suppress(BaseException):
            await close_redis_client(client)
        raise
    return client


async def close_redis_client(client: RedisClient) -> None:
    """关闭 Application 持有的 Redis 或 RedisCluster 连接资源。

    Args:
        client: 由应用统一创建并负责关闭的异步 Redis client。
    """
    if isinstance(client, RedisCluster):
        await client.aclose()
        return
    await client.aclose(close_connection_pool=True)


async def create_cluster_node_client(config: ClusterRedisConfig) -> Redis:
    """为 Cluster 经典 Pub/Sub 创建指向首个 seed 的普通 Redis client。

    redis-py 7.2 的异步 RedisCluster 不提供 Pub/Sub API；经典 Pub/Sub 会由
    Redis Cluster 在节点间传播，因此控制面只需连接一个已配置 seed。

    Args:
        config: 已校验的 Cluster 配置，使用首个 startup node。

    Returns:
        探活成功的普通 Redis client，调用方负责关闭其连接池。
    """
    node = config.startup_nodes[0]
    password = (
        config.password.get_secret_value() if config.password is not None else None
    )
    client = Redis(
        host=node.host,
        port=node.port,
        db=0,
        username=config.username,
        password=password,
        decode_responses=False,
        socket_connect_timeout=config.socket_timeout_seconds,
        socket_timeout=config.socket_timeout_seconds,
    )
    try:
        await cast(Awaitable[bool], client.ping())
    except BaseException:
        with suppress(BaseException):
            await client.aclose(close_connection_pool=True)
        raise
    return client


async def await_redis[ResultT](
    value: Awaitable[ResultT] | ResultT,
) -> ResultT:
    """收敛 redis-py asyncio 类型声明中的同步/异步返回联合。

    Args:
        value: redis-py 返回的可等待对象或已经取得的结果。

    Returns:
        等待完成后的结果，或原本已取得的同步值；异常原样传播。
    """
    if isinstance(value, Awaitable):
        return await value
    return value
