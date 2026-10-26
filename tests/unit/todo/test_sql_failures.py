"""SQL 提交未知、取消和确定的唯一键竞争边界。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.todo.sql.store import SqlTodoStore
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.unit.todo.test_store import CTX


@pytest.mark.parametrize("failure", [TimeoutError(), asyncio.CancelledError()])
async def test_unknown_commit_not_retried(failure: BaseException) -> None:
    connection = AsyncMock()
    result = Mock()
    result.first.return_value = None
    connection.execute.return_value = result

    class Engine:
        calls = 0

        @asynccontextmanager
        async def begin(self) -> AsyncIterator[AsyncConnection]:
            self.calls += 1
            yield cast(AsyncConnection, connection)
            raise failure

    engine = Engine()
    store = SqlTodoStore(cast(AsyncEngine, engine), timeout_seconds=1)
    expected = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else AdapterError
    )
    with pytest.raises(expected):
        await store.replace(CTX, ())
    assert engine.calls == 1
    assert connection.execute.await_count == 2


@pytest.mark.parametrize("code,expected_calls", [(1062, 2), (1213, 1)])
async def test_only_confirmed_duplicate_primary_key_recovered(
    code: int, expected_calls: int
) -> None:
    connection = AsyncMock()
    result = Mock()
    result.first.return_value = None
    error = IntegrityError(
        "INSERT", {}, Exception(code, "Duplicate entry for key PRIMARY")
    )
    connection.execute.side_effect = [result, error, result]

    class Engine:
        calls = 0

        @asynccontextmanager
        async def begin(self) -> AsyncIterator[AsyncConnection]:
            self.calls += 1
            yield cast(AsyncConnection, connection)

    engine = Engine()
    store = SqlTodoStore(cast(AsyncEngine, engine), timeout_seconds=1)
    if code == 1062:
        assert await store.replace(CTX, ()) == ()
        connection.rollback.assert_awaited_once()
    else:
        with pytest.raises(AdapterError):
            await store.replace(CTX, ())
    assert engine.calls == expected_calls


@pytest.mark.parametrize("duplicate", [False, True])
async def test_write_timestamp_excludes_connection_wait(duplicate: bool) -> None:
    """连接等待及冲突恢复期间的旧时间不能写成最后更新时间。"""
    now = datetime(2026, 9, 28, tzinfo=UTC)
    connection = AsyncMock()
    result = Mock()
    result.first.return_value = None if duplicate else ("app",)
    connection.execute.side_effect = (
        [result, IntegrityError("INSERT", {}, Exception(1062, "PRIMARY")), result]
        if duplicate
        else [result, result]
    )

    class Engine:
        @asynccontextmanager
        async def begin(self) -> AsyncIterator[AsyncConnection]:
            nonlocal now
            # 模拟连接池等待：实际取得连接时，时钟已前进。
            now += timedelta(seconds=10)
            yield cast(AsyncConnection, connection)

    store = SqlTodoStore(
        cast(AsyncEngine, Engine()), timeout_seconds=1, clock=lambda: now
    )
    await store.replace(CTX, ())
    statement = connection.execute.call_args_list[-1].args[0]
    assert statement.compile().params["updated_at"] == now.replace(tzinfo=None)
