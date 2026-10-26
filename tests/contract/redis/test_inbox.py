"""RedisInbox 的真实 Redis 合约测试。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    InboundRole,
    NormalReady,
    ResumeReady,
    ResumeRequest,
)
from agentsystem.inbox.redis_inbox import RedisInbox
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.message.models import (
    AssetSource,
    Base64Source,
    ImageBlock,
    MessageOrigin,
    TextBlock,
    UrlSource,
)
from agentsystem.redis.client import await_redis
from agentsystem.redis.codec import RedisJsonCodec
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix

NOW = datetime(2026, 8, 3, tzinfo=UTC)
CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def inbound(
    message_id: str,
    text: str,
    mode: InboundMode = InboundMode.QUEUE,
) -> InboundMessage:
    """构造固定时间的入站消息。"""
    return InboundMessage(
        message_id=message_id,
        session_id=CONTEXT.session_id,
        content=(TextBlock(text=text),),
        origin=MessageOrigin.USER,
        mode=mode,
        created_at=NOW,
    )


async def test_push_is_idempotent_and_rejects_conflicts(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """相同 ID+payload 幂等，不同 payload 冲突。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    try:
        await inbox.push(CONTEXT, inbound("m-1", "first"))
        await inbox.push(
            CONTEXT,
            inbound("m-1", "first").model_copy(
                update={"created_at": NOW + timedelta(seconds=1)}
            ),
        )
        with pytest.raises(AdapterError) as captured:
            await inbox.push(CONTEXT, inbound("m-1", "changed"))
        assert captured.value.code == "inbox_message_conflict"

        messages = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="owner",
        )
        assert [message.message_id for message in messages] == ["m-1"]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_system_role_and_custom_origin_survive_inbox_lifecycle(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """SYSTEM role 与任意 origin 必须经 pending/claim/complete 原样往返。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    system = InboundMessage(
        message_id="system-1",
        session_id=CONTEXT.session_id,
        role=InboundRole.SYSTEM,
        origin="订单系统 / refund",
        content=(TextBlock(text="refunded"),),
        mode=InboundMode.STEER,
        created_at=NOW,
    )
    try:
        await inbox.push(CONTEXT, system)

        assert await inbox.list_pending(CONTEXT) == (system,)
        assert await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.STEER}),
            owner_token="owner",
        ) == (system,)

        with pytest.raises(AdapterError) as captured:
            await inbox.push(
                CONTEXT,
                system.model_copy(update={"origin": "changed"}),
            )
        assert captured.value.code == "inbox_message_conflict"

        await inbox.complete(CONTEXT, (system.message_id,), owner_token="owner")
        assert await inbox.list_pending(CONTEXT) == ()
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_old_inbox_payload_schema_is_rejected(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Inbox adapter 不兼容缺少模块本地版本信封的旧 payload。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    keys = InboxRedisKeys(redis_prefix)
    legacy = inbound("legacy", "old payload")
    encoded = RedisJsonCodec().encode(legacy.model_dump(mode="json"))
    try:
        await await_redis(redis_client.rpush(keys.order(CONTEXT), legacy.message_id))
        await await_redis(
            redis_client.hset(
                keys.data(CONTEXT),
                legacy.message_id,
                encoded.decode("utf-8"),
            )
        )

        with pytest.raises(AdapterError) as captured:
            await inbox.list_pending(CONTEXT)
        assert captured.value.code == "unsupported_inbox_schema_version"
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_concurrent_multimodal_duplicate_is_idempotent(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """并发重复媒体消息只保留一次，且 Source JSON 往返不变。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    media = InboundMessage(
        message_id="media-duplicate",
        session_id=CONTEXT.session_id,
        content=(
            TextBlock(text="inspect"),
            ImageBlock(
                source=UrlSource(
                    url="https://example.test/a.png",
                    media_type="image/png",
                )
            ),
            ImageBlock(
                source=Base64Source(data="iVBORw0KGgo=", media_type="image/png")
            ),
            ImageBlock(
                source=AssetSource(
                    asset_id="sha256-" + "c" * 64,
                    media_type="image/png",
                )
            ),
        ),
        origin=MessageOrigin.USER,
        created_at=NOW,
    )
    try:
        await asyncio.gather(*(inbox.push(CONTEXT, media) for _ in range(8)))

        pending = await inbox.list_pending(CONTEXT)
        assert pending == (media,)
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_push_rejects_session_mismatch_and_capacity_overflow(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Inbox 边界拒绝错误 session，且超限不丢弃旧消息。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix, max_messages=1)
    await inbox.initialize((CONTEXT.agent_id,))
    mismatched = inbound("m-0", "wrong").model_copy(
        update={"session_id": "another-session"}
    )
    try:
        with pytest.raises(ValueError, match="session_id"):
            await inbox.push(CONTEXT, mismatched)
        await inbox.push(CONTEXT, inbound("m-1", "first"))
        with pytest.raises(AdapterError) as captured:
            await inbox.push(CONTEXT, inbound("m-2", "second"))
        assert captured.value.code == "inbox_capacity_exceeded"

        messages = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="owner",
        )
        assert [message.message_id for message in messages] == ["m-1"]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_claim_filters_modes_and_preserves_global_order(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """筛选删除后，未匹配消息保持原始相对顺序。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    try:
        await inbox.push(CONTEXT, inbound("q-1", "one"))
        await inbox.push(CONTEXT, inbound("s-1", "two", InboundMode.STEER))
        await inbox.push(CONTEXT, inbound("q-2", "three"))

        queued = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="inbox-owner",
        )
        await inbox.complete(
            CONTEXT,
            tuple(message.message_id for message in queued),
            owner_token="inbox-owner",
        )
        steered = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.STEER}),
            owner_token="steer-owner",
        )

        assert [message.message_id for message in queued] == ["q-1", "q-2"]
        assert [message.message_id for message in steered] == ["s-1"]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_ready_stream_separates_normal_and_resume_control(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """ResumeReady 进入 Stream 但不进入普通 Inbox。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    try:
        await inbox.push(CONTEXT, inbound("m-1", "hello"))
        request = ResumeRequest(
            ctx=CONTEXT,
            resume_id="resume-1",
            run_id="run-1",
            resume_token="token-1",
            input={"approved": True},
        )
        await inbox.push_resume(request)
        ready = await inbox.read_ready(
            "consumer-1",
            agent_ids=(CONTEXT.agent_id,),
            count=2,
            block_ms=100,
        )

        assert isinstance(ready[0], NormalReady)
        assert isinstance(ready[1], ResumeReady)
        assert ready[1].input == {"approved": True}

        messages = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="owner",
        )
        assert [message.message_id for message in messages] == ["m-1"]

        await inbox.requeue_resume(ready[1])
        requeued = await inbox.read_ready(
            "consumer-2",
            agent_ids=(CONTEXT.agent_id,),
            count=1,
            block_ms=100,
        )
        assert requeued == (ready[1],)
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_read_ready_discards_only_malformed_entry(
    redis_client: Redis,
    redis_prefix: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一条非法 NOACK entry 不得连带丢弃同次读取中的合法 Ready。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    keys = InboxRedisKeys(redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    try:
        await redis_client.xadd(
            keys.ready(CONTEXT.agent_id),
            {"kind": "normal", "app_id": CONTEXT.app_id},
        )
        await inbox.push(CONTEXT, inbound("valid", "hello"))

        ready = await inbox.read_ready(
            "consumer-1",
            agent_ids=(CONTEXT.agent_id,),
            count=2,
            block_ms=100,
        )

        assert ready == (NormalReady(**CONTEXT.model_dump()),)
        failure = next(
            record
            for record in caplog.records
            if "ready_decode_failed" in record.message
        )
        assert failure.exc_info is not None
        assert "error_id=" in failure.message
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_claim_keeps_pending_until_owner_completes(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """领取只加内部租约，消息仍是 pending，且只有 owner 能完成。"""
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id,))
    try:
        await inbox.push(CONTEXT, inbound("m-1", "first"))

        claimed = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="owner-a",
        )

        assert [message.message_id for message in claimed] == ["m-1"]
        assert [
            message.message_id for message in await inbox.list_pending(CONTEXT)
        ] == ["m-1"]
        assert (
            await inbox.claim(
                CONTEXT,
                frozenset({InboundMode.QUEUE}),
                owner_token="owner-b",
            )
            == ()
        )
        with pytest.raises(AdapterError) as captured:
            await inbox.complete(
                CONTEXT,
                ("m-1",),
                owner_token="owner-b",
            )
        assert captured.value.code == "inbox_claim_ownership_mismatch"

        await inbox.complete(CONTEXT, ("m-1",), owner_token="owner-a")
        await inbox.complete(CONTEXT, ("m-1",), owner_token="owner-a")
        assert await inbox.list_pending(CONTEXT) == ()
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_expired_claim_can_be_reclaimed(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """崩溃 owner 的短租约到期后，另一 owner 可以重新领取。"""
    inbox = RedisInbox(
        redis_client,
        prefix=redis_prefix,
        claim_ttl_seconds=1,
    )
    await inbox.initialize((CONTEXT.agent_id,))
    try:
        await inbox.push(CONTEXT, inbound("m-1", "first"))
        await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="owner-a",
        )

        await asyncio.sleep(1.05)

        reclaimed = await inbox.claim(
            CONTEXT,
            frozenset({InboundMode.QUEUE}),
            owner_token="owner-b",
        )
        assert [message.message_id for message in reclaimed] == ["m-1"]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_ready_streams_are_isolated_by_agent(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """节点只消费其已注册 Agent 的 Stream，不会抢走其他 Agent 任务。"""
    other = CONTEXT.model_copy(update={"agent_id": "agent / 用户"})
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id, other.agent_id))
    try:
        await inbox.push(CONTEXT, inbound("m-agent", "first"))
        await inbox.push(other, inbound("m-other", "second"))

        first = await inbox.read_ready(
            "consumer-a",
            agent_ids=(CONTEXT.agent_id,),
            count=2,
            block_ms=100,
        )
        second = await inbox.read_ready(
            "consumer-b",
            agent_ids=(other.agent_id,),
            count=2,
            block_ms=100,
        )

        assert [item.agent_id for item in first] == [CONTEXT.agent_id]
        assert [item.agent_id for item in second] == [other.agent_id]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_read_ready_count_applies_to_each_agent_stream(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """多 Stream 的 COUNT 1 必须允许每个 Agent Stream 各返回一条。"""
    other = CONTEXT.model_copy(update={"agent_id": "other-agent"})
    inbox = RedisInbox(redis_client, prefix=redis_prefix)
    await inbox.initialize((CONTEXT.agent_id, other.agent_id))
    try:
        await inbox.push(CONTEXT, inbound("first", "first"))
        await inbox.push(other, inbound("second", "second"))

        ready = await inbox.read_ready(
            "consumer",
            agent_ids=(CONTEXT.agent_id, other.agent_id),
            count=1,
            block_ms=100,
        )

        assert {item.agent_id for item in ready} == {
            CONTEXT.agent_id,
            other.agent_id,
        }
        assert len(ready) == 2
    finally:
        await delete_prefix(redis_client, redis_prefix)
