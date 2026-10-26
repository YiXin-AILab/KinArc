"""Todo 领域校验与 Local 合约。"""

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.todo.local.store import LocalTodoStore
from agentsystem.todo.models import TodoItem
from tests.contract.todo_store_contract import (
    assert_todo_concurrency,
    assert_todo_contract,
)

CTX = SessionContext(app_id="a", user_id="u", agent_id="g", session_id="s")


async def test_local_contract() -> None:
    store = LocalTodoStore()
    await assert_todo_contract(store, CTX)
    await assert_todo_concurrency(store, store, CTX)
    assert await LocalTodoStore().read(CTX) == ()


@pytest.mark.parametrize("content", ["", " \n", "x" * 501, "\ud800", 123])
def test_invalid_content(content: object) -> None:
    with pytest.raises(ValueError):
        TodoItem.model_validate({"content": content, "status": "pending"})


@pytest.mark.parametrize("extra", [{"status": "done"}, {"priority": "high"}])
def test_strict_item(extra: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        TodoItem.model_validate({"content": "x", "status": "pending", **extra})


async def test_identity_boundaries_and_immutable_results() -> None:
    store = LocalTodoStore()
    item = TodoItem(content=" 中文 " + "x" * 496, status="completed")
    assert len(item.content) == 500
    await store.replace(CTX, (item,) * 50)
    with pytest.raises(ValueError):
        item.__setattr__("content", "bad")
    assert (await store.read(CTX))[0].content.startswith(" 中文 ")
    for value in ("", "x" * 192, "\ud800", "s "):
        with pytest.raises(ValueError):
            await store.read(CTX.model_copy(update={"session_id": value}))
