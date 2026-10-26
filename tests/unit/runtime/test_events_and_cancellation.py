"""AgentEvent 与进程内取消令牌测试。"""

from dataclasses import FrozenInstanceError, is_dataclass
from datetime import UTC, datetime

import pytest
from agentsystem.common.models import FinishReason, OutputMode
from agentsystem.llm.models import (
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    Timing,
    Usage,
)
from agentsystem.message.models import (
    CompactionBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.runtime.events import (
    AgentEventFactory,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    MessagesCompleted,
    RunStarted,
    TurnStarted,
)

_NOW = datetime(2026, 8, 3, tzinfo=UTC)


def test_event_factory_preserves_envelope_identity_and_unique_facts() -> None:
    """同一执行段的 envelope 身份稳定，不同事实使用唯一 event_id。"""
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )

    user_message = Message(
        message_id="message",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    started = factory.create(RunStarted(initial_messages=(user_message,)))
    turned = factory.create(TurnStarted(turn_index=1), turn_index=1)

    assert started.session_id == turned.session_id == "session"
    assert started.run_id == turned.run_id == "run"
    assert started.trace_id == turned.trace_id == "trace"
    assert started.created_at == turned.created_at == _NOW
    assert started.event_id != turned.event_id
    assert isinstance(started.payload, RunStarted)
    assert started.payload.initial_messages == (user_message,)
    assert turned.turn_index == 1
    assert started.payload.type == "run.started"
    assert is_dataclass(started)
    assert is_dataclass(started.payload)
    with pytest.raises(FrozenInstanceError):
        started.__setattr__("session_id", "other")


def test_completed_messages_use_completed_discriminator() -> None:
    """完整消息保存屏障使用 completed 术语。"""
    message = Message(
        message_id="message",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )

    completed = MessagesCompleted(messages=(message,))

    assert completed.type == "messages.completed"


def test_pruned_event_accepts_empty_summary_attempts() -> None:
    """纯剪枝事件不伪造摘要 LLM 元数据。"""
    compaction = Message(
        message_id="compaction",
        session_id="session",
        role=Role.USER,
        content=(CompactionBlock(action="pruned", pruned_message_ids=("tool",)),),
        origin=MessageOrigin.COMPACTOR,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )

    event = ContextCompactionCompleted(
        action="pruned",
        compaction_message=compaction,
        compacted_message_ids=(),
        pruned_message_ids=("tool",),
        before_tokens=100,
        after_tokens=40,
        usage=Usage(),
    )

    assert event.summary_calls == ()
    assert event.selected_summary_index is None


def test_compaction_started_is_an_empty_fact() -> None:
    """压缩开始事件不重复携带结束事件才有的统计数据。"""
    event = ContextCompactionStarted()

    assert event.type == "context.compaction.started"


def test_summarized_event_requires_a_selected_summary_call() -> None:
    """摘要事件必须选择一组完整的请求响应对。"""
    compaction = Message(
        message_id="compaction",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=("old",),
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        model="summary-model",
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    with pytest.raises(ValueError, match="完整摘要调用"):
        ContextCompactionCompleted(
            action="summarized",
            compaction_message=compaction,
            compacted_message_ids=("old",),
            pruned_message_ids=(),
            before_tokens=100,
            after_tokens=40,
            usage=Usage(),
            summary_provider_name="provider",
            summary_calls=(),
            selected_summary_index=0,
        )


def test_summarized_event_rejects_multiple_summary_calls() -> None:
    """无重试压缩协议只允许保存唯一一次摘要调用。"""
    compaction = Message(
        message_id="compaction",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=("old",),
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        model="summary-model",
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    request = LLMRequest(
        request_id="summary-request",
        messages=(
            Message(
                message_id="summary-input",
                session_id="session",
                role=Role.USER,
                content=(TextBlock(text="history"),),
                origin=MessageOrigin.SERVICE,
                run_id="run",
                created_at=_NOW,
                visibility=MessageVisibility.ALL,
            ),
        ),
        config=LLMGenerationConfig(model="summary-model"),
        output_mode=OutputMode.COMPLETE,
    )
    response = LLMResponse(
        blocks=(TextBlock(text="summary"),),
        usage=Usage(),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="summary-model",
        finish_reason=FinishReason.STOP,
    )

    with pytest.raises(ValueError, match="唯一摘要调用"):
        ContextCompactionCompleted(
            action="summarized",
            compaction_message=compaction,
            compacted_message_ids=("old",),
            pruned_message_ids=(),
            before_tokens=100,
            after_tokens=40,
            usage=Usage(),
            summary_provider_name="provider",
            summary_calls=((request, response), (request, response)),
            selected_summary_index=0,
        )
