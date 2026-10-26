"""RedisStateManager 的真实 Redis 合约测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.redis.client import await_redis
from agentsystem.runtime.state import AgentState
from agentsystem.state.persistence import StateCodec
from agentsystem.state.redis.keys import StateRedisKeys
from agentsystem.state.redis.manager import RedisStateManager
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix
from tests.contract.state_manager_contract import (
    assert_orphan_transition,
    assert_resume_cas,
    assert_state_round_trip,
    assert_terminal_cas,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)
CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


async def test_state_round_trip_and_unknown_schema_rejection(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """最新状态可往返读取，未知 schema_version 明确失败。"""
    manager = RedisStateManager(redis_client, prefix=redis_prefix)
    try:
        await assert_state_round_trip(manager, CONTEXT)

        keys = StateRedisKeys(redis_prefix)
        await await_redis(
            redis_client.set(
                keys.state(CONTEXT),
                b'{"payload":{},"schema_version":999}',
            )
        )
        with pytest.raises(AdapterError) as captured:
            await manager.get(CONTEXT)
        assert captured.value.code == "unsupported_schema_version"
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_terminal_state_is_idempotent_but_conflicting_terminal_fails(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """同一终态可重投，不同终态不得覆盖已定稿 run。"""
    manager = RedisStateManager(redis_client, prefix=redis_prefix)
    try:
        await assert_terminal_cas(manager, CONTEXT, now=NOW)
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_state_rejects_payload_context_mismatch(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """key 与 payload 的 session 身份不一致时报告数据损坏。"""
    manager = RedisStateManager(redis_client, prefix=redis_prefix)
    other_context = CONTEXT.model_copy(update={"session_id": "other-session"})
    encoded = StateCodec().encode(AgentState(session_context=other_context))
    try:
        await await_redis(
            redis_client.set(StateRedisKeys(redis_prefix).state(CONTEXT), encoded)
        )

        with pytest.raises(AdapterError) as captured:
            await manager.get(CONTEXT)
        assert captured.value.code == "state_context_mismatch"
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_resume_transition_consumes_pending_state_once(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """PENDING→RUNNING 首次应用，重复恢复只能得到幂等结果。"""
    manager = RedisStateManager(redis_client, prefix=redis_prefix)
    try:
        await assert_resume_cas(manager, CONTEXT, now=NOW)
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_orphan_convergence_and_new_run_can_replace_previous_terminal(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """遗留 RUNNING 收敛后允许在同一 session 开始新 run。"""
    manager = RedisStateManager(redis_client, prefix=redis_prefix)
    try:
        await assert_orphan_transition(manager, CONTEXT, now=NOW)
    finally:
        await delete_prefix(redis_client, redis_prefix)
