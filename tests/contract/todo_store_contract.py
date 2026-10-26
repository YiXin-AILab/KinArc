"""Todo 后端共享行为合约。"""

import asyncio

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.todo.models import TodoItem
from agentsystem.todo.store import TodoStore


async def assert_todo_contract(store: TodoStore, ctx: SessionContext) -> None:
    """验证空读、完整覆盖、清空、隔离和无部分提交。"""
    assert await store.read(ctx) == ()
    todos = (
        TodoItem(content="same", status="in_progress"),
        TodoItem(content="same", status="in_progress"),
    )
    assert await store.replace(ctx, todos) == todos
    assert await store.read(ctx) == todos
    for field in ("app_id", "user_id", "agent_id", "session_id"):
        other = ctx.model_copy(update={field: "other"})
        assert await store.read(other) == ()
    with pytest.raises(ValueError):
        await store.replace(ctx, todos * 26)
    assert await store.read(ctx) == todos
    reopened = (TodoItem(content="changed", status="pending"),)
    assert await store.replace(ctx, reopened) == reopened
    assert await store.replace(ctx, ()) == ()
    assert await store.read(ctx) == ()


async def assert_todo_concurrency(
    first: TodoStore, second: TodoStore, ctx: SessionContext
) -> None:
    """并发创建/覆盖只能读到完整候选清单。"""
    candidates = [
        (TodoItem(content=str(index), status="pending"),) * 10 for index in range(12)
    ]
    results = await asyncio.gather(
        *(
            store.replace(ctx, items)
            for store, items in zip([first, second] * 6, candidates, strict=True)
        )
    )
    assert results == candidates
    assert await first.read(ctx) in candidates
    assert await second.read(ctx) == await first.read(ctx)
