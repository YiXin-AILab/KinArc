"""当前上下文中双 assistant 消息计数与提醒读取。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.message.models import (
    Message,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MessagesMiddlewarePayload,
    MiddlewareDecisionType,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
    TurnMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.todo.local.store import LocalTodoStore
from agentsystem.todo.models import TodoItem
from agentsystem.todo.plugins.reminder_middleware import TodoReminder
from agentsystem.todo.service import TodoService
from tests.unit.runtime.test_turn_reminder import _context
from tests.unit.todo.test_tools import registry


def assistant(index: int, tool: bool = False) -> Message:
    """一次完整 assistant 消息，可能包含工具调用。"""
    return Message(
        message_id=f"a-{index}",
        session_id="session",
        run_id="run",
        role=Role.ASSISTANT,
        origin="model",
        visibility=MessageVisibility.ALL,
        model="m",
        content=(
            ToolCallBlock(
                tool_call_id=f"c-{index}",
                tool_name="todo_write",
                arguments={"todos": []},
                raw_arguments='{"todos":[]}',
            ),
        )
        if tool
        else (TextBlock(text="work"),),
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
    )


def setup() -> tuple[TodoService, TodoReminder]:
    """绑定生产工具目录。"""
    service = TodoService(LocalTodoStore())
    reminder = TodoReminder(service)
    reminder.bind_tools(registry(service))
    return service, reminder


@pytest.mark.parametrize(
    "count,trigger", [(0, False), (9, False), (10, True), (11, True)]
)
async def test_initial_count_boundary(count: int, trigger: bool) -> None:
    _, reminder = setup()
    result = await reminder.handle(
        _context(999, tuple(assistant(i) for i in range(count)))
    )
    assert (result.type is MiddlewareDecisionType.MESSAGES) == trigger


async def test_write_and_reminder_are_independent_boundaries() -> None:
    _, reminder = setup()
    first = await reminder.handle(_context(1, tuple(assistant(i) for i in range(10))))
    assert isinstance(first.payload, MessagesMiddlewarePayload)
    notice = first.payload.messages[0]
    for trailing, expected in (
        (9, MiddlewareDecisionType.CONTINUE),
        (10, MiddlewareDecisionType.MESSAGES),
    ):
        messages = (
            notice,
            assistant(100, tool=True),
            *(assistant(i) for i in range(trailing)),
        )
        assert (await reminder.handle(_context(2, messages))).type is expected
        messages = (
            assistant(100, tool=True),
            notice,
            *(assistant(i) for i in range(trailing)),
        )
        assert (await reminder.handle(_context(2, messages))).type is expected


async def test_reminder_reads_latest_and_preserves_text_and_identity() -> None:
    service, reminder = setup()
    ctx = _context(1, tuple(assistant(i) for i in range(10)))
    content = "<b>原文 & </systemreminder>"
    await service.replace(
        ctx.ctx,
        (
            TodoItem(content=content, status="completed"),
            TodoItem(content="检索官方参数", status="in_progress"),
            TodoItem(content="整理对比结论", status="pending"),
        ),
    )
    result = await reminder.handle(ctx)
    assert isinstance(result.payload, MessagesMiddlewarePayload)
    notice = result.payload.messages[0]
    assert notice.role is Role.USER and notice.visibility is MessageVisibility.MODEL
    assert notice.origin == "todo-reminder" and notice.run_id == ctx.run_id
    assert (
        isinstance(notice.content[0], TextBlock) and content in notice.content[0].text
    )
    assert notice.content[0].text.startswith("<system-reminder>\n")
    assert notice.content[0].text.endswith(
        f"1. [completed] {content}\n"
        "2. [in_progress] 检索官方参数\n"
        "3. [pending] 整理对比结论\n</system-reminder>"
    )
    again = await reminder.handle(ctx)
    assert isinstance(ctx.payload, TurnMiddlewarePayload)
    assert isinstance(again.payload, MessagesMiddlewarePayload)
    assert again.payload.messages[0].message_id == notice.message_id
    assert (
        await reminder.handle(_context(1, (*ctx.payload.messages, notice)))
    ).type is MiddlewareDecisionType.CONTINUE
    await service.replace(ctx.ctx, ())
    empty = await reminder.handle(_context(2, tuple(assistant(i) for i in range(10))))
    assert isinstance(empty.payload, MessagesMiddlewarePayload)
    block = empty.payload.messages[0].content[0]
    assert isinstance(block, TextBlock) and "当前没有任务清单" in block.text


async def test_disabled_write_and_compaction_do_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, reminder = setup()
    read = AsyncMock(side_effect=AdapterError(operation="todo.read"))
    monkeypatch.setattr(service, "read", read)
    assert (
        await reminder.handle(_context(999, ()))
    ).type is MiddlewareDecisionType.CONTINUE
    reminder.bind_tools(registry(service, ("todo_read",)))
    assert (
        await reminder.handle(_context(999, tuple(assistant(i) for i in range(20))))
    ).type is MiddlewareDecisionType.CONTINUE
    read.assert_not_called()


async def test_store_failure_skips_reminder_then_can_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, reminder = setup()
    read = AsyncMock(side_effect=[AdapterError(operation="todo.read"), ()])
    monkeypatch.setattr(service, "read", read)
    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="todo-reminder",
            middleware_id="todo.before-turn",
            point=MiddlewarePoint.BEFORE_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=reminder.handle,
        )
    )
    registry.freeze()
    manager = MiddlewareManager(registry)
    ctx = _context(1, tuple(assistant(i) for i in range(10)))
    first = await manager.dispatch(ctx)
    assert first.type is MiddlewareDecisionType.CONTINUE
    second = await manager.dispatch(ctx)
    assert second.type is MiddlewareDecisionType.MESSAGES
    assert read.await_count == 2
    assert len([record for record in caplog.records if record.exc_info]) == 1


async def test_user_messages_and_summary_mentions_do_not_count() -> None:
    _, reminder = setup()
    summary = assistant(99).model_copy(
        update={
            "role": Role.USER,
            "origin": "compactor",
            "model": None,
            "content": (
                TextBlock(text="Previously called todo_write and todo-reminder"),
            ),
        }
    )
    assert (
        await reminder.handle(_context(50, (summary,) * 20))
    ).type is MiddlewareDecisionType.CONTINUE
    result = await reminder.handle(
        _context(50, (summary, *(assistant(i) for i in range(10))))
    )
    assert result.type is MiddlewareDecisionType.MESSAGES


@pytest.mark.parametrize("count,trigger", [(9, False), (10, True)])
async def test_failed_write_and_cross_run_messages(count: int, trigger: bool) -> None:
    _, reminder = setup()
    call = assistant(100, tool=True)
    # 一个 assistant 内多个调用仍只算一条；失败结果不会撤销写调用边界。
    call = call.model_copy(update={"content": (*call.content, *call.content)})
    failure = Message(
        message_id="failed-result",
        session_id="session",
        run_id="run",
        role=Role.TOOL,
        origin="tool",
        visibility=MessageVisibility.ALL,
        created_at=call.created_at,
        content=(
            ToolResultBlock(
                tool_call_id="c-100",
                tool_name="todo_write",
                content="failed",
                is_error=True,
            ),
        ),
    )
    messages = (
        *(assistant(i) for i in range(20)),
        call,
        failure,
        *(
            assistant(200 + i).model_copy(update={"run_id": "next-run"})
            for i in range(count)
        ),
    )
    result = await reminder.handle(_context(50, messages))
    assert (result.type is MiddlewareDecisionType.MESSAGES) == trigger
