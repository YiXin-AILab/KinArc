"""真实 Redis Standalone 和 Cluster 对话目录合约。"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.message.redis.manager import RedisMessageManager
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from tests.contract.redis.conftest import delete_cluster_prefix, delete_prefix
from tests.contract.session_catalog_contract import SessionCatalogContract


async def test_delete_legacy_history_without_catalog(
    redis_client: Redis, redis_prefix: str, catalog_context: SessionContext
) -> None:
    """尚未回填目录的历史会话也必须能显式删除消息。"""
    from agentsystem.message.redis.keys import MessageRedisKeys
    from tests.unit.service.test_titles import message

    ctx = catalog_context
    keys = MessageRedisKeys(redis_prefix)
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    history = message("旧历史").model_copy(update={"session_id": ctx.session_id})
    try:
        await manager.commit(ctx, (history,))
        await redis_client.delete(
            keys.sessions(ctx.app_id, ctx.user_id),
            keys.session_index(ctx.app_id, ctx.user_id),
            keys.session_index(ctx.app_id, ctx.user_id, ctx.agent_id),
        )
        await manager.delete_session(ctx, hard_delete=True)
        assert await manager.list_messages(ctx) == ()
    finally:
        await delete_prefix(redis_client, redis_prefix)


@pytest.fixture
def catalog_context() -> SessionContext:
    return SessionContext(
        app_id=uuid4().hex, user_id="user", agent_id="agent", session_id="s"
    )


@pytest.fixture
async def catalog(
    redis_client: Redis, redis_prefix: str
) -> AsyncIterator[MessageManager]:
    try:
        yield RedisMessageManager(redis_client, prefix=redis_prefix)
    finally:
        await delete_prefix(redis_client, redis_prefix)


class TestRedisSessionCatalog(SessionCatalogContract):
    """在 Standalone 上执行全部目录合约。"""


class TestClusterSessionCatalog(SessionCatalogContract):
    """在原生 Cluster 上执行全部目录合约。"""

    @pytest.fixture
    async def catalog(
        self, redis_cluster_client: RedisCluster, redis_prefix: str
    ) -> AsyncIterator[MessageManager]:
        try:
            yield RedisMessageManager(redis_cluster_client, prefix=redis_prefix)
        finally:
            await delete_cluster_prefix(redis_cluster_client, redis_prefix)


async def test_index_type_error_does_not_leave_partial_record(
    redis_client: Redis, redis_prefix: str, catalog_context: SessionContext
) -> None:
    """Lua 命令错误不回滚，必须在第一条写入前发现非法索引类型。"""
    from agentsystem.common.errors import AdapterError
    from agentsystem.message.redis.keys import MessageRedisKeys
    from tests.contract.session_catalog_contract import NOW

    ctx = catalog_context
    keys = MessageRedisKeys(redis_prefix)
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    try:
        await redis_client.set(
            keys.session_index(ctx.app_id, ctx.user_id, ctx.agent_id), "wrong-type"
        )
        with pytest.raises(AdapterError):
            await manager.ensure_session(ctx, title="title", now=NOW)
        assert await manager.get_session(ctx) is None
        assert (
            await redis_client.zcard(keys.session_index(ctx.app_id, ctx.user_id)) == 0
        )
    finally:
        await delete_prefix(redis_client, redis_prefix)
