"""SqlStateManager 的真实 MySQL 基础合约与损坏检测。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.runtime.state import AgentState
from agentsystem.state.persistence import StateCodec
from agentsystem.state.sql.keys import payload_hash, state_scope_key
from agentsystem.state.sql.manager import SqlStateManager
from agentsystem.state.sql.schema import states
from sqlalchemy import delete, insert, update
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.state_manager_contract import (
    assert_orphan_transition,
    assert_resume_cas,
    assert_state_round_trip,
    assert_terminal_cas,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _context() -> SessionContext:
    return SessionContext(
        app_id=f"sql-state-contract-{uuid4().hex}",
        user_id="user",
        agent_id="agent",
        session_id="session",
    )


async def _cleanup(engine: AsyncEngine, ctx: SessionContext) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            delete(states).where(states.c.scope_key == state_scope_key(ctx))
        )


async def test_sql_state_round_trip(mysql_engine: AsyncEngine) -> None:
    """SQL adapter 必须满足共享创建与读取合约。"""
    ctx = _context()
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        await assert_state_round_trip(manager, ctx)
    finally:
        await _cleanup(mysql_engine, ctx)


async def test_sql_state_terminal_cas(mysql_engine: AsyncEngine) -> None:
    """SQL adapter 必须满足共享终态 CAS 合约。"""
    ctx = _context()
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        await assert_terminal_cas(manager, ctx, now=NOW)
    finally:
        await _cleanup(mysql_engine, ctx)


async def test_sql_state_resume_cas(mysql_engine: AsyncEngine) -> None:
    """SQL adapter 必须满足共享 Resume 单次消费合约。"""
    ctx = _context()
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        await assert_resume_cas(manager, ctx, now=NOW)
    finally:
        await _cleanup(mysql_engine, ctx)


async def test_sql_state_orphan_transition(mysql_engine: AsyncEngine) -> None:
    """SQL adapter 必须满足共享 orphan 收敛合约。"""
    ctx = _context()
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        await assert_orphan_transition(manager, ctx, now=NOW)
    finally:
        await _cleanup(mysql_engine, ctx)


async def test_sql_state_detects_payload_hash_corruption(
    mysql_engine: AsyncEngine,
) -> None:
    """已存 payload 被篡改后不得作为合法执行状态返回。"""
    ctx = _context()
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        await manager.create(AgentState(session_context=ctx))
        async with mysql_engine.begin() as connection:
            await connection.execute(
                update(states)
                .where(states.c.scope_key == state_scope_key(ctx))
                .values(payload_sha256=b"x" * 32)
            )

        with pytest.raises(AdapterError) as captured:
            await manager.get(ctx)
        assert captured.value.code == "state_payload_invalid"
    finally:
        await _cleanup(mysql_engine, ctx)


async def test_sql_state_rejects_scope_digest_identity_collision(
    mysql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要键命中但原始身份不同时不得读取其他 session。"""
    stored_ctx = _context()
    requested_ctx = _context()
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        await manager.create(AgentState(session_context=stored_ctx))
        monkeypatch.setattr(
            "agentsystem.state.sql.manager.state_scope_key",
            lambda _ctx: state_scope_key(stored_ctx),
        )

        with pytest.raises(AdapterError) as captured:
            await manager.get(requested_ctx)
        assert captured.value.code == "state_payload_invalid"
    finally:
        await _cleanup(mysql_engine, stored_ctx)


async def test_sql_state_rejects_payload_context_mismatch(
    mysql_engine: AsyncEngine,
) -> None:
    """payload 内身份与索引身份不一致时不得返回状态。"""
    indexed_ctx = _context()
    payload_ctx = _context()
    encoded = StateCodec().encode(AgentState(session_context=payload_ctx))
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5)
    try:
        async with mysql_engine.begin() as connection:
            await connection.execute(
                insert(states).values(
                    scope_key=state_scope_key(indexed_ctx),
                    app_id=indexed_ctx.app_id,
                    user_id=indexed_ctx.user_id,
                    agent_id=indexed_ctx.agent_id,
                    session_id=indexed_ctx.session_id,
                    payload=encoded,
                    payload_sha256=payload_hash(encoded),
                )
            )

        with pytest.raises(AdapterError) as captured:
            await manager.get(indexed_ctx)
        assert captured.value.code == "state_payload_invalid"
    finally:
        await _cleanup(mysql_engine, indexed_ctx)
