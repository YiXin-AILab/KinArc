"""SQL 提交结果未知与取消不重放。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.memory.sql.store import SqlMemoryStore
from agentsystem.memory.store import MemoryEditResult, MemoryScope
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@pytest.mark.parametrize("failure", [TimeoutError(), asyncio.CancelledError()])
async def test_unknown_commit_is_not_retried(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Engine:
        calls = 0

        @asynccontextmanager
        async def begin(self) -> AsyncIterator[AsyncConnection]:
            self.calls += 1
            yield cast(AsyncConnection, object())
            raise failure

    engine = Engine()
    store = SqlMemoryStore(cast(AsyncEngine, engine), timeout_seconds=1)
    attempt = AsyncMock(return_value=MemoryEditResult("changed", "new"))
    monkeypatch.setattr(store, "_attempt", attempt)
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
    assert engine.calls == 1 and attempt.await_count == 1
