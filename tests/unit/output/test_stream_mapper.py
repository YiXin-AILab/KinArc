"""内部 AgentEvent 到公开 StreamEvent 的白名单与脱敏测试。"""

from datetime import UTC, datetime

from agentsystem.common.errors import RunError
from agentsystem.common.models import FinishReason
from agentsystem.config.models import PublicStreamConfig
from agentsystem.inbox.models import InboundMode
from agentsystem.llm.models import Timing, Usage
from agentsystem.message.models import (
    Base64Source,
    BlockType,
    CompactionBlock,
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
)
from agentsystem.outbound.stream_mapper import to_public_stream_event
from agentsystem.outbound.stream_models import (
    BlockCompletedPayload,
    BlockDeltaPayload,
    ContextCompactionCompletedPayload,
    ContextCompactionStartedPayload,
    MessageStartedPayload,
    RunTerminalPayload,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventFactory,
    AgentEventPayload,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessagesCompleted,
    MessageStarted,
    RunCompleted,
    RunFailed,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from agentsystem.runtime.state import RunTiming

_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_TIMING = RunTiming(started_at=_NOW, ended_at=_NOW, elapsed_ms=0, active_ms=0)


def _event(payload: AgentEventPayload) -> AgentEventEnvelope:
    factory = AgentEventFactory(
        session_id="session",
        run_id="internal-run",
        trace_id="internal-trace",
        clock=lambda: _NOW,
        event_id_factory=lambda: "internal-event",
    )
    return factory.create(payload)


def test_stream_mapper_publishes_thinking_text_without_signature() -> None:
    """默认公开 thinking 文本，但永不公开其 opaque signature。"""
    delta = to_public_stream_event(
        _event(
            MessageBlockDelta(
                message_id="assistant",
                index=0,
                block_id="thinking",
                block_type=BlockType.THINKING,
                delta="分析中",
            )
        ),
        PublicStreamConfig(),
    )
    completed = to_public_stream_event(
        _event(
            MessageBlockCompleted(
                message_id="assistant",
                index=0,
                block_id="thinking",
                block=ThinkingBlock(thinking="分析完成", signature="secret-signature"),
            )
        ),
        PublicStreamConfig(),
    )

    assert delta is not None
    assert delta.type == "message.block.delta"
    assert isinstance(delta.payload, BlockDeltaPayload)
    assert delta.payload.delta == "分析中"
    assert completed is not None
    assert isinstance(completed.payload, BlockCompletedPayload)
    assert completed.payload.content == "分析完成"
    assert "signature" not in completed.model_dump_json()


def test_stream_mapper_exposes_safe_compaction_lifecycle() -> None:
    """压缩公开事件只携带状态和统计，不携带摘要内容。"""
    started = to_public_stream_event(
        _event(ContextCompactionStarted()),
        PublicStreamConfig(),
    )
    completed = to_public_stream_event(
        _event(
            ContextCompactionCompleted(
                action="pruned",
                compaction_message=Message(
                    message_id="compaction",
                    session_id="session",
                    role=Role.USER,
                    content=(
                        CompactionBlock(action="pruned", pruned_message_ids=("tool",)),
                    ),
                    origin=MessageOrigin.COMPACTOR,
                    run_id="run",
                    created_at=_NOW,
                    visibility=MessageVisibility.ALL,
                ),
                compacted_message_ids=(),
                pruned_message_ids=("tool",),
                before_tokens=100,
                after_tokens=40,
                usage=Usage(),
            )
        ),
        PublicStreamConfig(),
    )

    assert started is not None
    assert started.type == "context.compaction.started"
    assert isinstance(started.payload, ContextCompactionStartedPayload)
    assert started.payload.model_dump() == {}
    assert completed is not None
    assert completed.type == "context.compaction.completed"
    assert isinstance(completed.payload, ContextCompactionCompletedPayload)
    assert completed.payload.message_id == "compaction"
    assert completed.payload.before_tokens == 100
    assert completed.payload.action == "pruned"


def test_compaction_completed_payload_accepts_events_before_message_id() -> None:
    """同一 schema v1 的历史压缩事件必须可在升级后继续重放。"""
    payload = ContextCompactionCompletedPayload.model_validate(
        {
            "action": "pruned",
            "before_tokens": 100,
            "after_tokens": 40,
            "usage": {},
        }
    )

    assert payload.message_id is None


def test_stream_mapper_can_disable_all_thinking_events() -> None:
    """关闭 thinking 后 start/delta/completed 均不得泄漏内容或长度。"""
    config = PublicStreamConfig(include_thinking=False)
    event = _event(
        MessageBlockDelta(
            message_id="assistant",
            index=0,
            block_id="thinking",
            block_type=BlockType.THINKING,
            delta="secret",
        )
    )

    assert to_public_stream_event(event, config) is None


def test_stream_mapper_guards_tool_arguments_and_results() -> None:
    """工具身份始终可见，参数和结果正文只在配置开启后公开并遮蔽 Secret。"""
    started = _event(
        ToolExecutionStarted(
            tool_call_id="call-1",
            tool_name="lookup",
            arguments={"query": "weather", "api_key": "must-hide"},
            raw_arguments='{"query":"weather","api_key":"must-hide"}',
        )
    )
    result = ToolResultBlock(
        tool_call_id="call-1",
        tool_name="lookup",
        content="raw-result",
    )
    tool_message = Message(
        message_id="tool-message",
        session_id="session",
        role=Role.TOOL,
        content=(result,),
        origin=MessageOrigin.TOOL,
        run_id="internal-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    completed = _event(
        ToolExecutionCompleted(
            tool_call_id="call-1",
            tool_name="lookup",
            result=result,
            message=tool_message,
            timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        )
    )
    defaults = PublicStreamConfig()
    enabled = PublicStreamConfig(
        include_tool_arguments=True,
        include_tool_results=True,
    )

    hidden_start = to_public_stream_event(started, defaults)
    hidden_result = to_public_stream_event(completed, defaults)
    visible_start = to_public_stream_event(started, enabled)
    visible_result = to_public_stream_event(completed, enabled)

    assert hidden_start is not None
    assert isinstance(hidden_start.payload, ToolStartedPayload)
    assert hidden_result is not None
    assert isinstance(hidden_result.payload, ToolCompletedPayload)
    assert visible_start is not None
    assert isinstance(visible_start.payload, ToolStartedPayload)
    assert visible_result is not None
    assert isinstance(visible_result.payload, ToolCompletedPayload)
    assert hidden_start.payload.arguments is None
    assert hidden_result.payload.content is None
    assert visible_start.payload.arguments is not None
    assert visible_start.payload.arguments["api_key"] == "[REDACTED]"
    assert "must-hide" not in visible_start.model_dump_json()
    assert visible_result.payload.content == "raw-result"


def test_stream_mapper_skips_internal_messages_and_maps_terminal_metrics() -> None:
    """MessagesCompleted 永不公开，终态公开安全错误与权威统计。"""
    message = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="answer"),),
        origin=MessageOrigin.MODEL,
        run_id="internal-run",
        created_at=_NOW,
        stop_reason=FinishReason.STOP,
        visibility=MessageVisibility.ALL,
    )
    config = PublicStreamConfig()

    assert (
        to_public_stream_event(_event(MessagesCompleted(messages=(message,))), config)
        is None
    )
    completed = to_public_stream_event(
        _event(
            RunCompleted(
                final_message=message, usage=Usage(total_tokens=3), timing=_TIMING
            )
        ),
        config,
    )
    failed = to_public_stream_event(
        _event(
            RunFailed(
                error=RunError(
                    code="failed",
                    message="safe",
                    error_id="opaque-error-id",
                    exception_type="agentsystem.common.errors.RunFailure",
                    stacktrace="Traceback ... private provider detail\n",
                ),
                usage=Usage(total_tokens=2),
                timing=_TIMING,
            )
        ),
        config,
    )

    assert completed is not None
    assert isinstance(completed.payload, RunTerminalPayload)
    assert failed is not None
    assert isinstance(failed.payload, RunTerminalPayload)
    assert completed.payload.status == "succeeded"
    assert completed.payload.usage.total_tokens == 3
    assert completed.payload.timing.active_ms == 0
    assert completed.payload.final_message_id == "assistant"
    assert failed.payload.error is not None
    assert failed.payload.error.code == "failed"
    assert failed.payload.error.error_id == "opaque-error-id"
    assert failed.payload.error.exception_type == (
        "agentsystem.common.errors.RunFailure"
    )
    assert failed.payload.error.stacktrace == (
        "Traceback ... private provider detail\n"
    )
    assert "private provider detail" in failed.model_dump_json()
    assert "internal-run" not in failed.model_dump_json()
    assert "internal-trace" not in failed.model_dump_json()


def test_stream_mapper_maps_explicit_message_started() -> None:
    """显式 MessageStarted 映射为单条公开 message.started。"""
    first = Message(
        message_id="user-1",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="first"),),
        origin=MessageOrigin.USER,
        run_id="internal-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    second = first.model_copy(
        update={
            "message_id": "user-2",
            "content": (TextBlock(text="second"),),
        }
    )

    mapped = tuple(
        to_public_stream_event(
            _event(MessageStarted(message=message, mode=mode)),
            PublicStreamConfig(),
        )
        for message, mode in (
            (first, InboundMode.QUEUE),
            (second, InboundMode.STEER),
        )
    )
    assert all(event is not None for event in mapped)
    events = tuple(event for event in mapped if event is not None)

    assert [event.type for event in events] == [
        "message.started",
        "message.started",
    ]
    assert all(isinstance(event.payload, MessageStartedPayload) for event in events)
    payloads = [
        event.payload
        for event in events
        if isinstance(event.payload, MessageStartedPayload)
    ]
    assert [payload.message.message_id for payload in payloads] == [
        "user-1",
        "user-2",
    ]
    assert [payload.mode for payload in payloads] == ["queue", "steer"]
    assert "internal-run" not in "".join(event.model_dump_json() for event in events)


def test_stream_mapper_preserves_system_role_and_origin() -> None:
    """动态 SYSTEM started 公开真实 role/origin。"""
    system = Message(
        message_id="system-1",
        session_id="session",
        role=Role.SYSTEM,
        content=(TextBlock(text="notice"),),
        origin="workflow",
        run_id="internal-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )

    event = to_public_stream_event(
        _event(MessageStarted(message=system, mode=InboundMode.STEER)),
        PublicStreamConfig(),
    )

    assert event is not None
    assert isinstance(event.payload, MessageStartedPayload)
    assert event.payload.message.role == "system"
    assert event.payload.message.origin == "workflow"


def test_stream_mapper_keeps_media_only_system_without_payload() -> None:
    """纯媒体 SYSTEM started 不被丢弃，且只公开安全媒体元数据。"""
    system = Message(
        message_id="system-media",
        session_id="session",
        role=Role.SYSTEM,
        content=(
            ImageBlock(source=Base64Source(data="c2VjcmV0", media_type="image/png")),
        ),
        origin="workflow.media",
        run_id="internal-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )

    event = to_public_stream_event(
        _event(MessageStarted(message=system, mode=InboundMode.QUEUE)),
        PublicStreamConfig(),
    )

    assert event is not None
    assert isinstance(event.payload, MessageStartedPayload)
    assert event.payload.message.content == ""
    assert event.payload.message.blocks[0].model_dump() == {
        "type": "image",
        "source_type": "base64",
        "media_type": "image/png",
        "filename": None,
        "asset_id": None,
    }
    assert "c2VjcmV0" not in event.model_dump_json()
