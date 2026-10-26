"""真实 MySQL Todo 原子覆盖与时间字段合约。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.todo.models import TodoItem
from agentsystem.todo.sql.schema import runtime_todos as table
from agentsystem.todo.sql.store import SqlTodoStore
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.todo_store_contract import (
    assert_todo_concurrency,
    assert_todo_contract,
)


async def test_todo_contract(mysql_engine: AsyncEngine) -> None:
    ctx = SessionContext(app_id=uuid4().hex, user_id="u", agent_id="g", session_id="s")
    first = SqlTodoStore(mysql_engine, timeout_seconds=5)
    second = SqlTodoStore(mysql_engine, timeout_seconds=5)
    try:
        await assert_todo_contract(first, ctx)
        for index in range(10):
            await assert_todo_concurrency(
                first, second, ctx.model_copy(update={"session_id": f"race-{index}"})
            )
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(delete(table).where(table.c.app_id == ctx.app_id))


async def test_created_time_preserved_even_for_same_content(
    mysql_engine: AsyncEngine,
) -> None:
    ctx = SessionContext(app_id=uuid4().hex, user_id="u", agent_id="g", session_id="s")
    now = datetime(2026, 9, 28, tzinfo=UTC)
    initial = now.replace(tzinfo=None)
    store = SqlTodoStore(mysql_engine, timeout_seconds=5, clock=lambda: now)
    item = (TodoItem(content="task", status="pending"),)
    try:
        for items in (item, item, ()):
            await store.replace(ctx, items)
            async with mysql_engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(table).where(table.c.app_id == ctx.app_id)
                    )
                ).one()
                assert row.created_at == initial
                assert row.updated_at == now.replace(tzinfo=None)
            assert await store.read(ctx) == items
            now += timedelta(seconds=1)
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(delete(table).where(table.c.app_id == ctx.app_id))


async def test_corrupt_data_is_an_error_and_rollback_preserves_record(
    mysql_engine: AsyncEngine,
) -> None:
    ctx = SessionContext(app_id=uuid4().hex, user_id="u", agent_id="g", session_id="s")
    store = SqlTodoStore(mysql_engine, timeout_seconds=5)
    items = (TodoItem(content="original", status="pending"),)
    condition = table.c.app_id == ctx.app_id
    try:
        await store.replace(ctx, items)
        async with mysql_engine.connect() as connection:
            before = (await connection.execute(select(table).where(condition))).one()
        async with mysql_engine.begin() as connection:
            await connection.execute(
                update(table)
                .where(condition)
                .values(todos_json="broken", updated_at=datetime(2027, 1, 1))
            )
            await connection.rollback()
        async with mysql_engine.connect() as connection:
            after = (await connection.execute(select(table).where(condition))).one()
        assert after == before
        assert await store.read(ctx) == items
        async with mysql_engine.begin() as connection:
            await connection.execute(
                update(table).where(condition).values(todos_json="broken")
            )
        with pytest.raises(AdapterError):
            await store.read(ctx)
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(delete(table).where(condition))
