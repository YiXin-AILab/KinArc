"""RedisSessionLock 的真实 Redis 合约测试。"""

import asyncio

from agentsystem.common.models import SessionContext
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.lock.lock import ReleaseIfIdleResult
from agentsystem.lock.redis_session_lock import RedisSessionLock
from agentsystem.redis.client import await_redis
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix

CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)
OTHER_CONTEXT = CONTEXT.model_copy(update={"session_id": "other-session"})


async def test_same_session_has_one_owner_and_other_session_is_independent(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """同一 session 单写，不同 session 的锁互不阻塞。"""
    inbox_keys = InboxRedisKeys(redis_prefix)
    lock = RedisSessionLock(
        redis_client,
        prefix=redis_prefix,
        inbox_order_key=inbox_keys.order,
    )
    try:
        first = await lock.acquire(CONTEXT)
        competing = await lock.acquire(CONTEXT)
        independent = await lock.acquire(OTHER_CONTEXT)

        assert first is not None
        assert competing is None
        assert independent is not None
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_only_owner_can_heartbeat_or_release(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """错误 owner token 不得续期或释放锁。"""
    inbox_keys = InboxRedisKeys(redis_prefix)
    lock = RedisSessionLock(
        redis_client,
        prefix=redis_prefix,
        inbox_order_key=inbox_keys.order,
    )
    try:
        lease = await lock.acquire(CONTEXT)
        assert lease is not None
        assert await lock.heartbeat(CONTEXT, "wrong-token") is False
        assert await lock.release(CONTEXT, "wrong-token") is False
        assert await lock.heartbeat(CONTEXT, lease.owner_token) is True
        assert await lock.release(CONTEXT, lease.owner_token) is True
        assert await lock.acquire(CONTEXT) is not None
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_lock_can_be_reacquired_after_ttl(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """owner 崩溃后，TTL 到期允许新 owner 接管。"""
    inbox_keys = InboxRedisKeys(redis_prefix)
    lock = RedisSessionLock(
        redis_client,
        prefix=redis_prefix,
        inbox_order_key=inbox_keys.order,
        ttl_ms=50,
    )
    try:
        assert await lock.acquire(CONTEXT) is not None
        await asyncio.sleep(0.08)
        assert await lock.acquire(CONTEXT) is not None
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_release_if_idle_closes_inbox_race_atomically(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Inbox 非空时保留锁，空闲时比较 token 后释放。"""
    inbox_keys = InboxRedisKeys(redis_prefix)
    lock = RedisSessionLock(
        redis_client,
        prefix=redis_prefix,
        inbox_order_key=inbox_keys.order,
    )
    try:
        lease = await lock.acquire(CONTEXT)
        assert lease is not None
        await await_redis(redis_client.rpush(inbox_keys.order(CONTEXT), "message-1"))

        dirty = await lock.release_if_idle(CONTEXT, lease.owner_token)
        assert dirty is ReleaseIfIdleResult.DIRTY
        assert await lock.acquire(CONTEXT) is None

        await await_redis(redis_client.delete(inbox_keys.order(CONTEXT)))
        released = await lock.release_if_idle(CONTEXT, lease.owner_token)
        assert released is ReleaseIfIdleResult.RELEASED
        assert await lock.acquire(CONTEXT) is not None
    finally:
        await delete_prefix(redis_client, redis_prefix)
