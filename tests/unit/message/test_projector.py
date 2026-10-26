"""MessageProjector 的消息与压缩提交测试。"""

from datetime import UTC, datetime

from agentsystem.common.models import SessionContext
from agentsystem.llm.models import Usage
from agentsystem.message.models import (
    CompactionBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.projector import MessageProjector
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventFactory,
    ContextCompactionCompleted,
    MessagesCompleted,
    RunStarted,
)
from tests.unit.runtime.port_stubs import MessageManagerStub

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


def _event(
    payload: RunStarted | MessagesCompleted | ContextCompactionCompleted,
) -> AgentEventEnvelope:
    return AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    ).create(payload)


def _user_message() -> Message:
    return Message(
        message_id="user",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )


async def test_projector_commits_only_completed_messages() -> None:
    committed: list[tuple[Message, ...]] = []

    class Manager(MessageManagerStub):
        async def commit(
            self,
            ctx: SessionContext,
            messages: tuple[Message, ...],
        ) -> None:
            assert ctx == _CONTEXT
            committed.append(messages)

    user = _user_message()
    projector = MessageProjector(Manager())

    await projector.project(_CONTEXT, _event(RunStarted(initial_messages=(user,))))
    await projector.project(
        _CONTEXT,
        _event(MessagesCompleted(messages=(user,))),
    )

    assert committed == [(user,)]


async def test_projector_commits_compaction_with_expected_latest() -> None:
    calls: list[tuple[Message, str | None]] = []

    class Manager(MessageManagerStub):
        async def commit_compaction(
            self,
            ctx: SessionContext,
            message: Message,
            *,
            expected_latest_compaction_id: str | None,
        ) -> None:
            assert ctx == _CONTEXT
            calls.append((message, expected_latest_compaction_id))

    compaction = Message(
        message_id="compaction-2",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="pruned",
                summary="summary",
                compacted_message_ids=("user-old",),
                next_retained_message_id="user-live",
                pruned_message_ids=("tool",),
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        model="summary-model",
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    payload = ContextCompactionCompleted(
        action="pruned",
        compaction_message=compaction,
        expected_latest_compaction_id="compaction-1",
        compacted_message_ids=("user-old",),
        pruned_message_ids=("tool",),
        before_tokens=90,
        after_tokens=30,
        usage=Usage(),
    )

    await MessageProjector(Manager()).project(_CONTEXT, _event(payload))

    assert calls == [(compaction, "compaction-1")]
