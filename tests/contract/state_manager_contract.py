"""Redis 与 SQL StateManager 共享的可观察行为合约。"""

from datetime import datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError, RunError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.runtime.state import (
    AgentState,
    SessionStatus,
    complete_run,
    converge_orphaned_run,
    fail_run,
    resume_run,
    start_run,
    suspend_run,
)
from agentsystem.state.manager import StateManager, StateWriteResult


async def assert_state_round_trip(
    manager: StateManager,
    ctx: SessionContext,
) -> None:
    """首次创建、重复创建和读取必须保持完整快照。"""
    idle = AgentState(session_context=ctx)

    assert await manager.create(idle) is StateWriteResult.APPLIED
    assert await manager.create(idle) is StateWriteResult.IDEMPOTENT
    assert await manager.get(ctx) == idle


async def assert_terminal_cas(
    manager: StateManager,
    ctx: SessionContext,
    *,
    now: datetime,
) -> None:
    """同一终态可重投，不同终态不得覆盖已定稿 run。"""
    later = now + timedelta(seconds=10)
    idle = AgentState(session_context=ctx)
    running = start_run(
        idle,
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=now,
    )
    done = complete_run(running, final_message_id="assistant-1", now=later)
    failed = fail_run(
        running,
        error=RunError(code="failed", message="failed"),
        now=later,
    )

    await manager.create(idle)
    assert await manager.save(idle, running) is StateWriteResult.APPLIED
    assert await manager.save(running, done) is StateWriteResult.APPLIED
    assert await manager.save(running, done) is StateWriteResult.IDEMPOTENT
    with pytest.raises(AdapterError) as captured:
        await manager.save(running, failed)
    assert captured.value.code == "state_conflict"
    assert await manager.get(ctx) == done


async def assert_resume_cas(
    manager: StateManager,
    ctx: SessionContext,
    *,
    now: datetime,
) -> None:
    """PENDING→RUNNING 首次应用，重复恢复只能得到幂等结果。"""
    later = now + timedelta(seconds=10)
    idle = AgentState(session_context=ctx)
    running = start_run(
        idle,
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=now,
    )
    pending = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="single-use-token",
        now=now,
    )
    resumed = resume_run(pending, resume_token="single-use-token", now=later)

    await manager.create(idle)
    await manager.save(idle, running)
    await manager.save(running, pending)
    assert await manager.save(pending, resumed) is StateWriteResult.APPLIED
    assert await manager.save(pending, resumed) is StateWriteResult.IDEMPOTENT
    stored = await manager.get(ctx)
    assert stored is not None
    assert stored.resume_token is None


async def assert_orphan_transition(
    manager: StateManager,
    ctx: SessionContext,
    *,
    now: datetime,
) -> None:
    """遗留 RUNNING 收敛后允许在同一 session 开始新 run。"""
    later = now + timedelta(seconds=10)
    idle = AgentState(session_context=ctx)
    running = start_run(
        idle,
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=now,
    )
    orphaned = converge_orphaned_run(running, now=later)
    restarted = start_run(
        orphaned,
        run_id="run-2",
        output_mode=OutputMode.COMPLETE,
        now=later,
    )

    await manager.create(idle)
    await manager.save(idle, running)
    await manager.save(running, orphaned)
    await manager.save(orphaned, restarted)
    current = await manager.get(ctx)
    assert current is not None
    assert current.status is SessionStatus.RUNNING
    assert current.run_id == "run-2"
