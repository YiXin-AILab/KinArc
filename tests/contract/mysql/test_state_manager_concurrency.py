"""SqlStateManager 在真实 MySQL 上的锁与竞态合约。"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.runtime.state import AgentState, start_run
from agentsystem.state.manager import StateWriteResult
from agentsystem.state.sql.keys import state_scope_key
from agentsystem.state.sql.manager import SqlStateManager
from agentsystem.state.sql.schema import states
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

NOW = datetime(2026, 8, 13, tzinfo=UTC)


@pytest.fixture
async def concurrent_store(
    mysql_engine: AsyncEngine,
) -> AsyncIterator[tuple[SqlStateManager, SessionContext]]:
    """提供独立 scope，并在测试后定向删除数据。"""
    context = SessionContext(
        app_id="app",
        user_id="state-concurrency",
        agent_id="agent",
        session_id=f"mysql-state-race-{uuid4().hex}",
    )
    manager = SqlStateManager(mysql_engine, operation_timeout_seconds=5.0)
    try:
        yield manager, context
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(states).where(states.c.scope_key == state_scope_key(context))
            )


async def test_concurrent_identical_creates_are_exactly_idempotent(
    concurrent_store: tuple[SqlStateManager, SessionContext],
) -> None:
    """相同初始快照并发创建时只能首次应用一次。"""
    manager, context = concurrent_store
    initial = AgentState(session_context=context)

    outcomes = await asyncio.gather(
        manager.create(initial),
        manager.create(initial),
    )

    assert sorted(outcomes) == sorted(
        (StateWriteResult.APPLIED, StateWriteResult.IDEMPOTENT)
    )
    assert await manager.get(context) == initial


async def test_concurrent_divergent_saves_have_one_cas_winner(
    concurrent_store: tuple[SqlStateManager, SessionContext],
) -> None:
    """同一 previous 快照的两个不同更新只能有一个成功。"""
    manager, context = concurrent_store
    initial = AgentState(session_context=context)
    first = start_run(
        initial,
        run_id="run-a",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    second = start_run(
        initial,
        run_id="run-b",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    await manager.create(initial)

    outcomes = await asyncio.gather(
        manager.save(initial, first),
        manager.save(initial, second),
        return_exceptions=True,
    )

    assert sum(result is StateWriteResult.APPLIED for result in outcomes) == 1
    conflicts = [result for result in outcomes if isinstance(result, AdapterError)]
    assert len(conflicts) == 1
    assert conflicts[0].code == "state_conflict"
    assert await manager.get(context) in (first, second)


async def test_locked_state_times_out_without_partial_update(
    mysql_engine: AsyncEngine,
    concurrent_store: tuple[SqlStateManager, SessionContext],
) -> None:
    """等待 state 行锁超过预算时不修改原快照。"""
    manager, context = concurrent_store
    initial = AgentState(session_context=context)
    current = start_run(
        initial,
        run_id="run-timeout",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    )
    await manager.create(initial)
    async with mysql_engine.connect() as lock_connection:
        transaction = await lock_connection.begin()
        await lock_connection.execute(
            select(states)
            .where(states.c.scope_key == state_scope_key(context))
            .with_for_update()
        )
        short_manager = SqlStateManager(
            mysql_engine,
            operation_timeout_seconds=0.05,
        )
        try:
            with pytest.raises(AdapterError) as captured:
                await short_manager.save(initial, current)
            assert captured.value.code == "adapter_timeout"
        finally:
            await transaction.rollback()

    assert await manager.get(context) == initial
