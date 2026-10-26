"""本节点活动 Run Task 注册与取消归属测试。"""

import asyncio
from datetime import UTC, datetime

import pytest
from agentsystem.cancellation.models import CancellationCommand
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.models import SessionContext
from pydantic import ValidationError

_CTX = SessionContext(
    app_id="app",
    agent_id="agent",
    user_id="user",
    session_id="session",
)


async def _waiting_task() -> None:
    """保持活动，直到测试显式取消。"""
    await asyncio.Event().wait()


def test_cancellation_command_rejects_unknown_and_empty_fields() -> None:
    """内部广播命令必须是严格且完整的无正文模型。"""
    command = CancellationCommand(
        app_id="app",
        agent_id="agent",
        user_id="user",
        session_id="session",
        run_id="run",
        requested_at=datetime(2026, 8, 13, tzinfo=UTC),
    )

    assert command.run_id == "run"
    with pytest.raises(ValidationError):
        CancellationCommand.model_validate(
            {**command.model_dump(), "run_id": "", "reason": "secret"}
        )


async def test_request_only_cancels_exact_active_run_once() -> None:
    """错误 scope 不得命中，重复广播不得重复增加 Task 取消计数。"""
    registry = RunTaskRegistry()
    task = asyncio.create_task(_waiting_task())
    registry.register(_CTX, "run", task)
    other = _CTX.model_copy(update={"session_id": "other"})

    assert registry.request_if_active(other, "run") is False
    assert registry.request_if_active(_CTX, "run") is True
    assert registry.request_if_active(_CTX, "run") is True
    assert task.cancelling() == 1
    assert registry.is_user_requested(_CTX, "run") is True
    assert registry.consume_user_cancellation(_CTX, "run") is True
    assert registry.consume_user_cancellation(_CTX, "run") is False

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_inactive_request_does_not_create_ghost_entry() -> None:
    """先到达的 Pub/Sub 命令不能取消未来复用同一 run_id 的 Task。"""
    registry = RunTaskRegistry()

    assert registry.request_if_active(_CTX, "future") is False
    task = asyncio.create_task(_waiting_task())
    registry.register(_CTX, "future", task)

    assert registry.is_user_requested(_CTX, "future") is False
    assert task.cancelling() == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_register_rejects_replacing_an_active_task() -> None:
    """同一个完整 run scope 在本节点最多只能有一个活动 Task。"""
    registry = RunTaskRegistry()
    first = asyncio.create_task(_waiting_task())
    second = asyncio.create_task(_waiting_task())
    registry.register(_CTX, "run", first)

    with pytest.raises(RuntimeError, match="已经注册"):
        registry.register(_CTX, "run", second)

    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)


async def test_clear_does_not_affect_new_run_in_same_session() -> None:
    """旧 run 清理不能删除同 session 新 run 的注册项。"""
    registry = RunTaskRegistry()
    old = asyncio.create_task(_waiting_task())
    new = asyncio.create_task(_waiting_task())
    registry.register(_CTX, "old", old)
    registry.register(_CTX, "new", new)

    registry.clear(_CTX, "old")

    assert registry.request_if_active(_CTX, "old") is False
    assert registry.request_if_active(_CTX, "new") is True
    old.cancel()
    await asyncio.gather(old, new, return_exceptions=True)
