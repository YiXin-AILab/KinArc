"""原生 Redis Cluster 下多 key/Lua/Stream slot 合约。"""

from datetime import UTC, datetime

from agentsystem.common.models import SessionContext
from agentsystem.config.models import PublicStreamConfig
from agentsystem.inbox.models import InboundMessage, InboundMode
from agentsystem.inbox.redis_inbox import RedisInbox
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.lock.lock import ReleaseIfIdleResult
from agentsystem.lock.redis_session_lock import RedisSessionLock
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.outbound.redis_event_bus import RedisSessionEventBus
from agentsystem.outbound.stream_models import PublicStreamEvent, RunStartedPayload
from agentsystem.redis.client import await_redis
from agentsystem.runtime.state import AgentState
from agentsystem.state.manager import StateWriteResult
from agentsystem.state.redis.manager import RedisStateManager
from redis.asyncio.cluster import RedisCluster
from redis.crc import key_slot
from tests.contract.redis.conftest import delete_cluster_prefix

_NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _context(agent_id: str = "agent-a") -> SessionContext:
    return SessionContext(
        app_id="app",
        user_id="user",
        agent_id=agent_id,
        session_id=f"session-{agent_id}",
    )


def _inbound(ctx: SessionContext, message_id: str) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        session_id=ctx.session_id,
        content=(TextBlock(text=message_id),),
        origin=MessageOrigin.USER,
        created_at=_NOW,
    )


async def test_multi_agent_ready_streams_share_slot_and_blocking_read(
    redis_cluster_client: RedisCluster,
    redis_prefix: str,
) -> None:
    """多 Agent Ready Stream 应同槽并由一次 XREADGROUP 共同读取。"""
    contexts = (_context("agent-a"), _context("agent-b"))
    queue = RedisInbox(redis_cluster_client, prefix=redis_prefix)
    keys = InboxRedisKeys(redis_prefix)
    ready_keys = [keys.ready(ctx.agent_id) for ctx in contexts]

    assert key_slot(ready_keys[0].encode()) == key_slot(ready_keys[1].encode())
    try:
        await queue.initialize(tuple(ctx.agent_id for ctx in contexts))
        for index, ctx in enumerate(contexts):
            await queue.push(ctx, _inbound(ctx, f"message-{index}"))

        ready = await queue.read_ready(
            "cluster-consumer",
            agent_ids=tuple(ctx.agent_id for ctx in contexts),
            count=2,
            block_ms=100,
        )

        assert {item.agent_id for item in ready} == {"agent-a", "agent-b"}
    finally:
        await delete_cluster_prefix(redis_cluster_client, redis_prefix)


async def test_session_adapters_keep_atomic_keys_in_one_cluster_slot(
    redis_cluster_client: RedisCluster,
    redis_prefix: str,
) -> None:
    """Inbox、Lock、Message、State 与 Stream Lua 均不得触发 CROSSSLOT。"""
    ctx = _context()
    queue = RedisInbox(redis_cluster_client, prefix=redis_prefix)
    inbox_keys = InboxRedisKeys(redis_prefix)
    lock = RedisSessionLock(
        redis_cluster_client,
        prefix=redis_prefix,
        inbox_order_key=inbox_keys.order,
    )
    messages = RedisMessageManager(redis_cluster_client, prefix=redis_prefix)
    states = RedisStateManager(redis_cluster_client, prefix=redis_prefix)
    event_bus = RedisSessionEventBus(
        redis_cluster_client,
        redis_cluster_client,
        prefix=redis_prefix,
        config=PublicStreamConfig(
            retention_seconds=30,
            terminal_grace_seconds=2,
        ),
    )
    user_message = Message(
        message_id="user-1",
        session_id=ctx.session_id,
        role=Role.USER,
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    stream_event = PublicStreamEvent(
        event_id="event-stream",
        type="run.started",
        created_at=_NOW,
        session_id=ctx.session_id,
        payload=RunStartedPayload(),
    )

    try:
        await queue.initialize((ctx.agent_id,))
        await queue.push(ctx, _inbound(ctx, "inbound-1"))
        claimed = await queue.claim(
            ctx,
            frozenset({InboundMode.QUEUE}),
            owner_token="queue-owner",
        )
        assert [item.message_id for item in claimed] == ["inbound-1"]
        await queue.complete(ctx, ("inbound-1",), owner_token="queue-owner")

        lease = await lock.acquire(ctx)
        assert lease is not None
        await await_redis(redis_cluster_client.rpush(inbox_keys.order(ctx), "pending"))
        assert (
            await lock.release_if_idle(ctx, lease.owner_token)
            is ReleaseIfIdleResult.DIRTY
        )
        await await_redis(redis_cluster_client.delete(inbox_keys.order(ctx)))
        assert (
            await lock.release_if_idle(ctx, lease.owner_token)
            is ReleaseIfIdleResult.RELEASED
        )

        await messages.commit(ctx, (user_message,))
        assert await messages.get(ctx, user_message.message_id) == user_message
        idle = AgentState(session_context=ctx)
        assert await states.create(idle) is StateWriteResult.APPLIED
        assert await states.get(ctx) == idle

        appended = await event_bus.append(ctx, stream_event)
        replay = await event_bus.read(ctx, None, count=10, block_ms=0)
        assert replay.records == (appended,)
    finally:
        await delete_cluster_prefix(redis_cluster_client, redis_prefix)
