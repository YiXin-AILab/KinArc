"""故障合约不将 Mock 当作原子性证明。"""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.memory.redis.store import RedisMemoryStore
from agentsystem.memory.store import MemoryScope
from agentsystem.redis.client import RedisClient
from redis.asyncio import Redis


@pytest.mark.parametrize("failure", [TimeoutError(), asyncio.CancelledError()])
async def test_uncertain_write_never_replayed(failure: BaseException) -> None:
    client = AsyncMock()
    client.set_retry = Mock()
    client.get.return_value = b"old"
    client.eval.side_effect = failure
    store = RedisMemoryStore(
        cast(RedisClient, client), prefix="test", timeout_seconds=1
    )
    expected = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else AdapterError
    )
    with pytest.raises(expected):
        await store.add(
            app_id="a",
            scope=MemoryScope.USER_GLOBAL,
            user_id="u",
            agent_id=None,
            new_content="new",
            max_chars=1_000_000,
        )
    assert client.eval.await_count == 1


async def test_confirmed_conflict_recomputes_latest_text() -> None:
    client = AsyncMock()
    client.set_retry = Mock()
    client.get.side_effect = [b"A B", b"A C"]
    client.eval.side_effect = [0, 1]
    store = RedisMemoryStore(
        cast(RedisClient, client), prefix="test", timeout_seconds=1
    )
    result = await store.replace(
        app_id="a",
        scope=MemoryScope.USER_GLOBAL,
        user_id="u",
        agent_id=None,
        old_content="A",
        new_content="D",
        max_chars=1_000_000,
    )
    assert result.content == "D C"


async def test_corrupt_utf8_is_not_empty_memory() -> None:
    client = AsyncMock()
    client.set_retry = Mock()
    client.get.return_value = b"\xff"
    store = RedisMemoryStore(
        cast(RedisClient, client), prefix="test", timeout_seconds=1
    )
    with pytest.raises(AdapterError):
        await store.read(
            app_id="a", scope=MemoryScope.USER_GLOBAL, user_id="u", agent_id=None
        )


def test_store_disables_driver_write_retries() -> None:
    client = Redis()
    RedisMemoryStore(client, prefix="test", timeout_seconds=1)
    retry = client.get_retry()
    assert retry is not None and retry.get_retries() == 0
