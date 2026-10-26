"""存储边界故障与数据库结构，不以 Mock 证明原子性。"""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.redis.client import RedisClient
from agentsystem.todo.redis.store import RedisTodoStore
from agentsystem.todo.sql.schema import runtime_todos
from tests.unit.todo.test_store import CTX


@pytest.mark.parametrize("raw", [b"null", b"{}", b"\xff", b'[{"content":"x"}]'])
async def test_corrupt_storage_is_not_empty(raw: bytes) -> None:
    client = AsyncMock()
    client.set_retry = Mock()
    client.get.return_value = raw
    store = RedisTodoStore(cast(RedisClient, client), prefix="test", timeout_seconds=1)
    with pytest.raises(AdapterError):
        await store.read(CTX)


@pytest.mark.parametrize("failure", [TimeoutError(), asyncio.CancelledError()])
async def test_unknown_write_not_replayed(failure: BaseException) -> None:
    client = AsyncMock()
    client.set_retry = Mock()
    client.set.side_effect = failure
    store = RedisTodoStore(cast(RedisClient, client), prefix="test", timeout_seconds=1)
    expected = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else AdapterError
    )
    with pytest.raises(expected):
        await store.replace(CTX, ())
    assert client.set.await_count == 1


def test_sql_schema_only_contains_identity_content_and_times() -> None:
    assert set(runtime_todos.c.keys()) == {
        "app_id",
        "user_id",
        "agent_id",
        "session_id",
        "todos_json",
        "created_at",
        "updated_at",
    }
    assert [column.name for column in runtime_todos.primary_key] == [
        "app_id",
        "user_id",
        "agent_id",
        "session_id",
    ]
    assert not runtime_todos.foreign_keys
