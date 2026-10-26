"""真实 Redis client 生命周期合约测试。"""

from collections.abc import Awaitable
from typing import cast
from uuid import uuid4

from agentsystem.redis.client import close_redis_client, create_redis_client
from tests.redis_config import local_redis_config


async def test_client_connects_to_local_docker_redis() -> None:
    """工厂返回的 asyncio client 必须能 ping 并干净关闭连接池。"""
    client = await create_redis_client(local_redis_config())

    try:
        assert await cast(Awaitable[bool], client.ping()) is True
    finally:
        await close_redis_client(client)


async def test_client_blocking_read_outlives_base_socket_timeout() -> None:
    """阻塞命令窗口必须计入 socket timeout，空闲 XREAD 不应断开。"""
    config = local_redis_config(socket_timeout_seconds=0.05)
    client = await create_redis_client(
        config,
        max_block_ms=100,
    )

    try:
        result = await client.xread(
            {f"test:blocking-client:{uuid4().hex}": "$"},
            block=100,
        )
        assert result == []
    finally:
        await close_redis_client(client)
