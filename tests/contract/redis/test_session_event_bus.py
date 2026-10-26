"""RedisSessionEventBus 的真实 Redis Stream 合约测试。"""

import asyncio
from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import ServiceError
from agentsystem.common.models import SessionContext
from agentsystem.config.models import PublicStreamConfig
from agentsystem.llm.models import Usage
from agentsystem.outbound.redis_event_bus import RedisSessionEventBus
from agentsystem.outbound.redis_event_keys import PublicEventRedisKeys
from agentsystem.outbound.stream_models import (
    PublicStreamEvent,
    PublicStreamLifecycle,
    RunResumedPayload,
    RunStartedPayload,
    RunSuspendedPayload,
    RunTerminalPayload,
)
from agentsystem.runtime.state import RunTiming
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix

_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def _event(sequence: int, *, terminal: bool = False) -> PublicStreamEvent:
    payload = (
        RunTerminalPayload(
            status="succeeded",
            usage=Usage(),
            timing=RunTiming(
                started_at=_NOW,
                ended_at=_NOW,
                elapsed_ms=0,
                active_ms=0,
            ),
        )
        if terminal
        else RunStartedPayload()
        if sequence == 1
        else RunResumedPayload()
    )
    return PublicStreamEvent(
        event_id=f"public-{sequence}",
        type="run.completed"
        if terminal
        else "run.started"
        if sequence == 1
        else "run.resumed",
        created_at=_NOW,
        session_id="session",
        payload=payload,
    )


def _config(**overrides: int) -> PublicStreamConfig:
    return PublicStreamConfig.model_validate(
        {
            "retention_seconds": 30,
            "terminal_grace_seconds": 2,
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_append_replay_and_two_readers_are_independent(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """无 cursor 从头重放，两个客户端读取同一进度且互不消费。"""
    bus = RedisSessionEventBus(
        redis_client,
        redis_client,
        prefix=redis_prefix,
        config=_config(),
    )
    try:
        first = await bus.append(_CONTEXT, _event(1))
        second = await bus.append(_CONTEXT, _event(2))

        reader_one = await bus.read(_CONTEXT, None, count=10, block_ms=0)
        reader_two = await bus.read(_CONTEXT, None, count=10, block_ms=0)
        after_first = await bus.read(
            _CONTEXT,
            first.cursor,
            count=10,
            block_ms=0,
        )

        assert [item.cursor for item in reader_one.records] == [
            first.cursor,
            second.cursor,
        ]
        assert reader_two.records == reader_one.records
        assert after_first.records == (second,)
        assert after_first.next_cursor == second.cursor
        assert after_first.lifecycle is PublicStreamLifecycle.OPEN
    finally:
        await delete_prefix(redis_client, redis_prefix)


@pytest.mark.asyncio
async def test_terminal_ttl_and_run_started_reopens_stream(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """终态进入短宽限 TTL；新 run.started 可在宽限期重开并恢复保留期。"""
    bus = RedisSessionEventBus(
        redis_client,
        redis_client,
        prefix=redis_prefix,
        config=_config(),
    )
    keys = PublicEventRedisKeys(redis_prefix)
    try:
        await bus.append(_CONTEXT, _event(1))
        await bus.append(_CONTEXT, _event(2, terminal=True))

        terminal = await bus.inspect(_CONTEXT, None)
        terminal_ttl = await redis_client.ttl(keys.stream(_CONTEXT))
        assert terminal.lifecycle is PublicStreamLifecycle.TERMINAL
        assert 0 < terminal_ttl <= 2

        await bus.append(_CONTEXT, _event(1))
        reopened = await bus.inspect(_CONTEXT, None)
        reopened_ttl = await redis_client.ttl(keys.stream(_CONTEXT))
        assert reopened.lifecycle is PublicStreamLifecycle.OPEN
        assert 20 <= reopened_ttl <= 30
    finally:
        await delete_prefix(redis_client, redis_prefix)


@pytest.mark.asyncio
async def test_new_epoch_rejects_old_cursor_and_ahead_cursor(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """精确删除后的新 Stream 使用新 epoch，并区分超前 cursor。"""
    bus = RedisSessionEventBus(
        redis_client,
        redis_client,
        prefix=redis_prefix,
        config=_config(),
    )
    keys = PublicEventRedisKeys(redis_prefix)
    try:
        old = await bus.append(_CONTEXT, _event(1))
        await redis_client.delete(keys.stream(_CONTEXT), keys.metadata(_CONTEXT))
        current = await bus.append(_CONTEXT, _event(1))

        assert current.cursor.epoch != old.cursor.epoch
        with pytest.raises(ServiceError) as expired:
            await bus.inspect(_CONTEXT, old.cursor)
        assert expired.value.code == "cursor_expired"
        with pytest.raises(ServiceError) as ahead:
            await bus.inspect(
                _CONTEXT,
                current.cursor.model_copy(update={"stream_id": "9999999999999-0"}),
            )
        assert ahead.value.code == "cursor_ahead"
    finally:
        await delete_prefix(redis_client, redis_prefix)


@pytest.mark.asyncio
async def test_blocking_read_observes_later_append_and_pending_stays_open(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """有限 XREAD 可接收稍后追加；PENDING 不是清理 Stream 的终态。"""
    bus = RedisSessionEventBus(
        redis_client,
        redis_client,
        prefix=redis_prefix,
        config=_config(),
    )
    try:
        first = await bus.append(_CONTEXT, _event(1))
        read_task = asyncio.create_task(
            bus.read(_CONTEXT, first.cursor, count=10, block_ms=1_000)
        )
        await asyncio.sleep(0.02)
        suspended = PublicStreamEvent(
            event_id="public-suspended",
            type="run.suspended",
            created_at=_NOW,
            session_id="session",
            payload=RunSuspendedPayload(resume_token="resume-token"),
        )
        appended = await bus.append(_CONTEXT, suspended)

        batch = await read_task
        assert batch.records == (appended,)
        assert batch.lifecycle is PublicStreamLifecycle.OPEN
    finally:
        await delete_prefix(redis_client, redis_prefix)


@pytest.mark.asyncio
async def test_maxlen_trim_expires_old_cursor_and_diagnostics_are_payload_free(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """长度裁剪使旧位置失效，诊断不记录 session 或正文。"""
    diagnostics: list[object] = []
    bus = RedisSessionEventBus(
        redis_client,
        redis_client,
        prefix=redis_prefix,
        config=_config(max_events_per_session=10),
        diagnostic_sink=diagnostics.append,
    )
    try:
        old = await bus.append(_CONTEXT, _event(1))
        for sequence in range(2, 302):
            await bus.append(_CONTEXT, _event(sequence))

        with pytest.raises(ServiceError) as expired:
            await bus.inspect(_CONTEXT, old.cursor)
        assert expired.value.code == "cursor_expired"
        assert diagnostics
        serialized = "".join(str(item) for item in diagnostics)
        assert "session" not in serialized
        assert "payload" not in serialized
    finally:
        await delete_prefix(redis_client, redis_prefix)


@pytest.mark.asyncio
async def test_empty_stream_read_is_finite_and_session_scopes_are_isolated(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """不存在的 Stream 有限返回，其他 session 的事件不会串流。"""
    bus = RedisSessionEventBus(
        redis_client,
        redis_client,
        prefix=redis_prefix,
        config=_config(),
    )
    other_context = _CONTEXT.model_copy(update={"session_id": "other-session"})
    try:
        empty = await bus.read(_CONTEXT, None, count=10, block_ms=20)
        await bus.append(
            other_context, _event(1).model_copy(update={"session_id": "other-session"})
        )
        original = await bus.inspect(_CONTEXT, None)
        other = await bus.read(other_context, None, count=10, block_ms=0)

        assert empty.records == ()
        assert empty.next_cursor is None
        assert original.exists is False
        assert len(other.records) == 1
        assert other.records[0].event.session_id == "other-session"
    finally:
        await delete_prefix(redis_client, redis_prefix)
