"""真实 MySQL 条件更新与元数据合约。"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import pytest
from agentsystem.memory.sql.keys import MemoryIdentity
from agentsystem.memory.sql.schema import runtime_memories
from agentsystem.memory.sql.store import SqlMemoryStore
from agentsystem.memory.store import MemoryScope
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.contract.memory_store_contract import (
    assert_concurrent_edits,
    assert_memory_store_contract,
)


class _Identity(TypedDict):
    """测试调用的显式身份参数。"""

    app_id: str
    scope: MemoryScope
    user_id: str
    agent_id: str | None


async def test_memory_contract(mysql_engine: AsyncEngine) -> None:
    app_id = uuid4().hex
    first = SqlMemoryStore(mysql_engine, timeout_seconds=5)
    second = SqlMemoryStore(mysql_engine, timeout_seconds=5)
    try:
        await assert_memory_store_contract(first, app_id=app_id)
        await assert_concurrent_edits(first, second, app_id=app_id)
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(runtime_memories).where(runtime_memories.c.app_id == app_id)
            )


async def test_sql_metadata_exact_comparison_and_concurrent_recompute(
    mysql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = uuid4().hex
    now = datetime(2026, 9, 24, tzinfo=UTC)
    first = SqlMemoryStore(mysql_engine, timeout_seconds=5, clock=lambda: now)
    second = SqlMemoryStore(mysql_engine, timeout_seconds=5, clock=lambda: now)
    scope = MemoryScope.USER_GLOBAL
    identity = _Identity(app_id=app_id, scope=scope, user_id="u", agent_id=None)
    condition = runtime_memories.c.app_id == app_id
    try:
        await first.add(**identity, new_content="alpha beta", max_chars=1_000_000)
        async with mysql_engine.connect() as connection:
            initial = (
                await connection.execute(select(runtime_memories).where(condition))
            ).one()
        now += timedelta(seconds=1)
        await first.replace(
            **identity, old_content="alpha", new_content="alpha", max_chars=1_000_000
        )
        async with mysql_engine.connect() as connection:
            unchanged = (
                await connection.execute(select(runtime_memories).where(condition))
            ).one()
        assert unchanged.updated_at == initial.updated_at

        # Force both writers to calculate from the same row before either submits.
        original = first._read
        reads = 0
        ready = asyncio.Event()

        async def synchronized_read(
            connection: AsyncConnection, fields: MemoryIdentity
        ) -> tuple[int, str] | None:
            nonlocal reads
            row = await original(connection, fields)
            reads += 1
            if reads == 2:
                ready.set()
            if reads <= 2:
                await ready.wait()
            return row

        monkeypatch.setattr(first, "_read", synchronized_read)
        monkeypatch.setattr(second, "_read", synchronized_read)
        await asyncio.gather(
            first.replace(
                **identity, old_content="alpha", new_content="A", max_chars=1_000_000
            ),
            second.replace(
                **identity, old_content="beta", new_content="B", max_chars=1_000_000
            ),
        )
        assert await first.read(**identity) == "A B"
        assert reads >= 3
        async with mysql_engine.begin() as connection:
            changed = (
                await connection.execute(select(runtime_memories).where(condition))
            ).one()
            assert changed.id == initial.id
            assert changed.created_at == initial.created_at
            assert changed.updated_at > initial.updated_at
            for previous in ("a B", "Á B", "A B "):
                result = await connection.execute(
                    update(runtime_memories)
                    .where(
                        condition,
                        runtime_memories.c.content == previous,
                        func.char_length(runtime_memories.c.content) == len(previous),
                    )
                    .values(content="wrong")
                )
                assert result.rowcount == 0
        oversized = await first.add(
            **identity, new_content="x" * 1_000_001, max_chars=1_000_000
        )
        assert oversized.status == "capacity_exceeded"
        assert await first.read(**identity) == "A B"
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(delete(runtime_memories).where(condition))


async def test_memory_recomputes_after_concurrent_trailing_space_edit(
    mysql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅尾部空格变化也必须使旧正文条件更新失败并重算。"""
    app_id = uuid4().hex
    store = SqlMemoryStore(mysql_engine, timeout_seconds=5)
    other = SqlMemoryStore(mysql_engine, timeout_seconds=5)
    identity = _Identity(
        app_id=app_id, scope=MemoryScope.USER_GLOBAL, user_id="u", agent_id=None
    )
    original_read = store._read
    injected = False

    async def read_before_concurrent_edit(
        connection: AsyncConnection, fields: MemoryIdentity
    ) -> tuple[int, str] | None:
        nonlocal injected
        row = await original_read(connection, fields)
        if not injected:
            injected = True
            await other.replace(
                **identity, old_content="正文", new_content="正文 ", max_chars=100
            )
        return row

    try:
        await store.add(**identity, new_content="正文", max_chars=100)
        monkeypatch.setattr(store, "_read", read_before_concurrent_edit)
        result = await store.add(**identity, new_content="追加", max_chars=100)
        assert result.content == "正文 \n追加"
        assert await other.read(**identity) == "正文 \n追加"
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(runtime_memories).where(runtime_memories.c.app_id == app_id)
            )
