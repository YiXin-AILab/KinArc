"""SqlMessageManager 在真实 MySQL 上的锁、竞态与快照合约。"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.models import (
    CompactionBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.sql.keys import session_scope_key
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.message.sql.schema import message_sessions, messages
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine

NOW = datetime(2026, 8, 10, tzinfo=UTC)


@pytest.fixture
async def concurrent_store(
    mysql_engine: AsyncEngine,
) -> AsyncIterator[tuple[SqlMessageManager, SessionContext]]:
    """提供独立 scope，并在测试后定向删除数据。"""
    context = SessionContext(
        app_id="app",
        user_id="concurrency",
        agent_id="agent",
        session_id=f"mysql-race-{uuid4().hex}",
    )
    manager = SqlMessageManager(mysql_engine, operation_timeout_seconds=5.0)
    try:
        yield manager, context
    finally:
        scope_key = session_scope_key(context)
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(messages).where(messages.c.session_scope_key == scope_key)
            )
            await connection.execute(
                delete(message_sessions).where(
                    message_sessions.c.scope_key == scope_key
                )
            )


def _user(ctx: SessionContext, message_id: str, text: str | None = None) -> Message:
    return Message(
        message_id=message_id,
        session_id=ctx.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text=text or message_id),),
        visibility=MessageVisibility.ALL,
    )


def _system(ctx: SessionContext) -> Message:
    return Message(
        message_id="system",
        session_id=ctx.session_id,
        role=Role.SYSTEM,
        origin=MessageOrigin.SYSTEM,
        created_at=NOW,
        content=(TextBlock(text="system"),),
        visibility=MessageVisibility.MODEL,
    )


def _compaction(ctx: SessionContext, message_id: str) -> Message:
    return Message(
        message_id=message_id,
        session_id=ctx.session_id,
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        run_id="run-1",
        created_at=NOW,
        content=(
            CompactionBlock(
                action="summarized",
                summary=message_id,
                compacted_message_ids=("old",),
            ),
        ),
        visibility=MessageVisibility.ALL,
    )


async def test_concurrent_new_session_writers_allocate_contiguous_positions(
    mysql_engine: AsyncEngine,
    concurrent_store: tuple[SqlMessageManager, SessionContext],
) -> None:
    """并发建 head 与写入由行锁串行化，position 不重不漏。"""
    manager, context = concurrent_store

    results = await asyncio.gather(
        *(
            manager.commit(context, (_user(context, f"m-{index}"),))
            for index in range(20)
        )
    )

    assert results == [None] * 20
    stored = await manager.list_messages(context)
    assert {item.message_id for item in stored} == {
        *(f"m-{index}" for index in range(20))
    }
    async with mysql_engine.connect() as connection:
        positions = (
            (
                await connection.execute(
                    select(messages.c.position)
                    .where(messages.c.session_scope_key == session_scope_key(context))
                    .order_by(messages.c.position)
                )
            )
            .scalars()
            .all()
        )
    assert positions == list(range(1, 21))


async def test_overlapping_batches_and_same_id_writers_are_serializable(
    concurrent_store: tuple[SqlMessageManager, SessionContext],
) -> None:
    """重叠同内容幂等，而同 ID 不同内容只能报告冲突。"""
    manager, context = concurrent_store
    shared = _user(context, "shared")
    await asyncio.gather(
        manager.commit(context, (shared, _user(context, "left"))),
        manager.commit(context, (shared, _user(context, "right"))),
    )
    outcomes = await asyncio.gather(
        manager.commit(context, (shared,)),
        manager.commit(context, (_user(context, "shared", "changed"),)),
        return_exceptions=True,
    )

    assert outcomes[0] is None
    assert isinstance(outcomes[1], AdapterError)
    assert outcomes[1].code == "message_conflict"
    assert {item.message_id for item in await manager.list_messages(context)} == {
        "shared",
        "left",
        "right",
    }


async def test_compaction_compare_and_set_race_has_one_winner(
    concurrent_store: tuple[SqlMessageManager, SessionContext],
) -> None:
    """同一 expected head 的两个摘要只能有一个推进指针。"""
    manager, context = concurrent_store
    outcomes = await asyncio.gather(
        manager.commit_compaction(
            context,
            _compaction(context, "compaction-a"),
            expected_latest_compaction_id=None,
        ),
        manager.commit_compaction(
            context,
            _compaction(context, "compaction-b"),
            expected_latest_compaction_id=None,
        ),
        return_exceptions=True,
    )

    assert sum(result is None for result in outcomes) == 1
    conflicts = [result for result in outcomes if isinstance(result, AdapterError)]
    assert len(conflicts) == 1
    assert conflicts[0].code == "compaction_conflict"


async def test_context_source_reads_message_table_once_without_head_select(
    mysql_engine: AsyncEngine,
    concurrent_store: tuple[SqlMessageManager, SessionContext],
) -> None:
    """上下文读取不得用 head 查询制造跨语句快照。"""
    manager, context = concurrent_store
    await manager.commit(context, (_system(context), _user(context, "user")))
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(mysql_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        source = await manager.read_context_source(context)
    finally:
        event.remove(
            mysql_engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )

    assert source.retained_messages == (_user(context, "user"),)
    message_selects = [
        statement for statement in statements if "runtime_messages" in statement
    ]
    assert len(message_selects) == 1
    assert "runtime_message_sessions" not in message_selects[0]


async def test_locked_session_times_out_without_partial_insert(
    mysql_engine: AsyncEngine,
    concurrent_store: tuple[SqlMessageManager, SessionContext],
) -> None:
    """等待 session 行锁超过预算时回滚整个新批次。"""
    manager, context = concurrent_store
    await manager.commit(context, (_user(context, "base"),))
    scope_key = session_scope_key(context)
    async with mysql_engine.connect() as lock_connection:
        transaction = await lock_connection.begin()
        await lock_connection.execute(
            select(message_sessions)
            .where(message_sessions.c.scope_key == scope_key)
            .with_for_update()
        )
        short_manager = SqlMessageManager(
            mysql_engine,
            operation_timeout_seconds=0.05,
        )
        try:
            with pytest.raises(AdapterError) as captured:
                await short_manager.commit(context, (_user(context, "timed-out"),))
            assert captured.value.code == "adapter_timeout"
        finally:
            await transaction.rollback()

    assert await manager.get(context, "timed-out") is None
