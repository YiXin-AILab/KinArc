"""TraceProjector 的标准 OTel/GenAI 语义测试。"""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import LLMError, RunError
from agentsystem.common.models import FinishReason, OutputMode, SessionContext
from agentsystem.llm.models import (
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    Timing,
    Usage,
    UsageAccounting,
)
from agentsystem.message.models import (
    AssetSource,
    Base64Source,
    BlockType,
    CompactionBlock,
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolResultBlock,
)
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventFactory,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    LLMCallCompleted,
    LLMCallStarted,
    MessageBlockDelta,
    MessageBlockStarted,
    MessagesCompleted,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    TurnStarted,
)
from agentsystem.runtime.state import RunTiming
from agentsystem.trace.otel import OTelTraceRecorder
from agentsystem.trace.projector import TraceProjector
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)
_CONTENT_KEYS = {
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
}


def _json_attribute(attributes: Mapping[str, object], key: str) -> object:
    value = attributes[key]
    assert isinstance(value, str)
    return json.loads(value)


def _message(role: Role, text: str, *, message_id: str) -> Message:
    return Message(
        message_id=message_id,
        session_id="session",
        role=role,
        content=(TextBlock(text=text),),
        origin=MessageOrigin.USER if role is Role.USER else MessageOrigin.MODEL,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )


def _projector(
    *,
    capture_content: bool = False,
) -> tuple[TraceProjector, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        TraceProjector(
            OTelTraceRecorder(provider),
            capture_content=capture_content,
        ),
        exporter,
    )


def _successful_events(
    *,
    user_text: str = "查询天气",
    user_message: Message | None = None,
    tool_error: bool = False,
) -> tuple[AgentEventEnvelope, ...]:
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )
    user = user_message or _message(Role.USER, user_text, message_id="user")
    assistant = _message(Role.ASSISTANT, "上海晴", message_id="assistant")
    request = LLMRequest(
        request_id="request",
        messages=(user,),
        config=LLMGenerationConfig(model="test-model"),
        output_mode=OutputMode.COMPLETE,
    )
    response = LLMResponse(
        blocks=(TextBlock(text="上海晴"),),
        usage=Usage(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            accounting=UsageAccounting.EXACT,
            cache_read_tokens=30,
            cache_write_tokens=10,
            reasoning_tokens=5,
        ),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="test-model",
        finish_reason=FinishReason.STOP,
        response_id="response",
    )
    result = ToolResultBlock(
        tool_call_id="call-1",
        tool_name="search",
        content="晴",
        is_error=tool_error,
    )
    tool_message = Message(
        message_id="tool",
        session_id="session",
        role=Role.TOOL,
        content=(result,),
        origin=MessageOrigin.TOOL,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    return (
        factory.create(RunStarted(initial_messages=(user,))),
        factory.create(TurnStarted(turn_index=1), turn_index=1),
        factory.create(
            LLMCallStarted(
                request_id="request",
                provider_name="openai",
                model="test-model",
                output_mode=OutputMode.COMPLETE,
                request=request,
            ),
            turn_index=1,
        ),
        factory.create(
            LLMCallCompleted(request_id="request", response=response),
            turn_index=1,
        ),
        factory.create(
            ToolExecutionStarted(
                tool_call_id="call-1",
                tool_name="search",
                arguments={"city": "上海"},
                raw_arguments='{"city":"上海"}',
            ),
            turn_index=1,
        ),
        factory.create(
            ToolExecutionCompleted(
                tool_call_id="call-1",
                tool_name="search",
                result=result,
                message=tool_message,
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
            ),
            turn_index=1,
        ),
        factory.create(
            RunCompleted(
                final_message=assistant,
                usage=Usage(),
                timing=RunTiming(
                    started_at=_NOW,
                    ended_at=_NOW,
                    elapsed_ms=0,
                    active_ms=0,
                ),
            ),
            turn_index=1,
        ),
    )


async def _project_all(
    projector: TraceProjector,
    events: tuple[AgentEventEnvelope, ...],
) -> None:
    for event in events:
        await projector.project(_CONTEXT, event)


@pytest.mark.parametrize("failed", [False, True])
async def test_llm_span_exports_request_id_without_content_capture(
    failed: bool,
) -> None:
    """成功或失败的模型调用均导出请求 ID，且不依赖正文采集。"""
    projector, exporter = _projector()
    events = _successful_events()
    completed = events[3]
    assert isinstance(completed.payload, LLMCallCompleted)
    if failed:
        response = completed.payload.response.model_copy(
            update={
                "finish_reason": FinishReason.ERROR,
                "error": LLMError(code="timeout", message="timeout"),
                "response_id": None,
            }
        )
        completed = replace(
            completed,
            payload=LLMCallCompleted(request_id="request", response=response),
        )

    await _project_all(projector, (*events[:3], completed))

    span = exporter.get_finished_spans()[0]
    attributes = span.attributes or {}
    assert attributes["llm.request_id"] == "request"
    assert _CONTENT_KEYS.isdisjoint(attributes)
    if failed:
        assert span.status.status_code is StatusCode.ERROR
        assert "gen_ai.response.id" not in attributes


async def test_metadata_hierarchy_uses_standard_and_correlation_attributes() -> None:
    """Agent、turn、LLM 和 tool 保持层级且不写 vendor 私有属性。"""
    projector, exporter = _projector()

    await _project_all(projector, _successful_events())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["invoke_agent"]
    turn = spans["agent turn"]
    llm = spans["chat test-model"]
    tool = spans["execute_tool search"]
    root_attributes = root.attributes or {}
    assert root_attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root_attributes["gen_ai.agent.id"] == "agent"
    assert root_attributes["gen_ai.conversation.id"] == "session"
    llm_attributes = llm.attributes or {}
    assert llm_attributes["gen_ai.operation.name"] == "chat"
    assert llm_attributes["gen_ai.provider.name"] == "openai"
    assert llm_attributes["gen_ai.request.model"] == "test-model"
    tool_attributes = tool.attributes or {}
    assert tool_attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool_attributes["gen_ai.tool.call.id"] == "call-1"
    assert tool_attributes["gen_ai.tool.name"] == "search"
    assert turn.parent is not None and turn.parent.span_id == root.context.span_id
    assert llm.parent is not None and llm.parent.span_id == turn.context.span_id
    assert tool.parent is not None and tool.parent.span_id == turn.context.span_id
    assert root.events == ()
    assert llm.events == ()
    assert tool.events == ()
    all_keys = {key for span in spans.values() for key in (span.attributes or {})}
    assert not any(key.startswith("langfuse.") for key in all_keys)
    assert not any(key.startswith("agent.") for key in all_keys)
    assert "tool.is_error" not in all_keys


async def test_content_is_redacted_by_default() -> None:
    """默认策略不得复制消息、参数或工具结果。"""
    projector, exporter = _projector()

    await _project_all(projector, _successful_events())

    all_keys = {
        key for span in exporter.get_finished_spans() for key in (span.attributes or {})
    }
    assert _CONTENT_KEYS.isdisjoint(all_keys)


async def test_content_uses_standard_genai_json_shapes() -> None:
    """显式采集时使用标准消息 parts 和工具字段。"""
    projector, exporter = _projector(capture_content=True)

    await _project_all(projector, _successful_events())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    llm_attributes = spans["chat test-model"].attributes or {}
    tool_attributes = spans["execute_tool search"].attributes or {}
    assert _json_attribute(llm_attributes, "gen_ai.input.messages") == [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "查询天气"}],
        }
    ]
    assert _json_attribute(llm_attributes, "gen_ai.output.messages") == [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "上海晴"}],
            "finish_reason": "stop",
        }
    ]
    assert _json_attribute(tool_attributes, "gen_ai.tool.call.arguments") == {
        "city": "上海"
    }
    assert _json_attribute(tool_attributes, "gen_ai.tool.call.result") == {
        "content": "晴",
        "is_error": False,
    }


async def test_content_preserves_redacted_multimodal_sources() -> None:
    """Asset 和 Base64 媒体必须按顺序进入 root 与 Generation 输入。"""
    encoded = "dGVzdA=="
    user = Message(
        message_id="multimodal-user",
        session_id="session",
        role=Role.USER,
        content=(
            ImageBlock(
                source=AssetSource(
                    asset_id="sha256-image",
                    media_type="image/jpeg",
                    filename="image.jpg",
                )
            ),
            TextBlock(text="检查图片"),
            ImageBlock(
                source=Base64Source(
                    data=encoded,
                    media_type="image/png",
                )
            ),
        ),
        origin=MessageOrigin.USER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    projector, exporter = _projector(capture_content=True)

    await _project_all(
        projector,
        _successful_events(user_message=user),
    )

    expected = [
        {
            "role": "user",
            "parts": [
                {
                    "type": "uri",
                    "modality": "image",
                    "mime_type": "image/jpeg",
                    "uri": "asset://sha256-image",
                    "filename": "image.jpg",
                },
                {"type": "text", "content": "检查图片"},
                {
                    "type": "blob",
                    "modality": "image",
                    "mime_type": "image/png",
                    "base64_chars": len(encoded),
                    "content_omitted": True,
                },
            ],
        }
    ]
    spans = {span.name: span for span in exporter.get_finished_spans()}
    root_attributes = spans["invoke_agent"].attributes or {}
    llm_attributes = spans["chat test-model"].attributes or {}
    assert _json_attribute(root_attributes, "gen_ai.input.messages") == expected
    assert _json_attribute(llm_attributes, "gen_ai.input.messages") == expected
    assert encoded not in str(root_attributes["gen_ai.input.messages"])
    assert encoded not in str(llm_attributes["gen_ai.input.messages"])


async def test_content_preserves_complete_long_messages() -> None:
    """内容采集必须保留完整长消息，不提供隐式截断。"""
    long_text = "长" * 200
    projector, exporter = _projector(capture_content=True)

    await _project_all(projector, _successful_events(user_text=long_text))

    llm = next(
        span for span in exporter.get_finished_spans() if span.name == "chat test-model"
    )
    rendered = (llm.attributes or {})["gen_ai.input.messages"]
    assert isinstance(rendered, str)
    assert _json_attribute(
        llm.attributes or {},
        "gen_ai.input.messages",
    ) == [
        {
            "role": "user",
            "parts": [{"type": "text", "content": long_text}],
        }
    ]


async def test_usage_and_tool_error_use_standard_attributes() -> None:
    """Token 明细和工具失败只使用标准属性与 OTel Status。"""
    projector, exporter = _projector()

    await _project_all(projector, _successful_events(tool_error=True))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    llm_attributes = spans["chat test-model"].attributes or {}
    assert llm_attributes["gen_ai.usage.input_tokens"] == 100
    assert llm_attributes["gen_ai.usage.output_tokens"] == 20
    assert llm_attributes["gen_ai.usage.cache_read.input_tokens"] == 30
    assert llm_attributes["gen_ai.usage.cache_creation.input_tokens"] == 10
    assert llm_attributes["gen_ai.usage.reasoning.output_tokens"] == 5
    tool = spans["execute_tool search"]
    assert tool.status.status_code is StatusCode.ERROR
    assert (tool.attributes or {})["error.type"] == "tool_error"
    assert tool.events == ()


async def test_stream_message_events_do_not_create_spans() -> None:
    """公开流式消息事件不是 Trace 生命周期事件。"""
    projector, exporter = _projector()
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )
    message = _message(Role.USER, "private", message_id="user")
    events = (
        factory.create(MessagesCompleted(messages=(message,))),
        factory.create(
            MessageBlockStarted(
                message_id="assistant",
                index=0,
                block_id="block",
                block_type=BlockType.TEXT,
            )
        ),
        factory.create(
            MessageBlockDelta(
                message_id="assistant",
                index=0,
                block_id="block",
                block_type=BlockType.TEXT,
                delta="private",
            )
        ),
    )

    await _project_all(projector, events)

    assert exporter.get_finished_spans() == ()


def _compaction_event(*, pruned: bool = False) -> AgentEventEnvelope:
    """构造剪枝或两次摘要尝试事件。"""
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )
    if pruned:
        block = CompactionBlock(action="pruned", pruned_message_ids=("tool",))
        message = Message(
            message_id="compaction-pruned",
            session_id="session",
            role=Role.USER,
            content=(block,),
            origin=MessageOrigin.COMPACTOR,
            run_id="run",
            created_at=_NOW,
            visibility=MessageVisibility.ALL,
        )
        return factory.create(
            ContextCompactionCompleted(
                action="pruned",
                compaction_message=message,
                compacted_message_ids=(),
                pruned_message_ids=("tool",),
                before_tokens=100,
                after_tokens=50,
                usage=Usage(),
            ),
            turn_index=1,
        )
    request = LLMRequest(
        request_id="compact-0",
        messages=(_message(Role.USER, "input-0", message_id="in-0"),),
        config=LLMGenerationConfig(model="summary-model"),
        output_mode=OutputMode.COMPLETE,
    )
    response = LLMResponse(
        blocks=(TextBlock(text="summary-0"),),
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="summary-model",
        finish_reason=FinishReason.STOP,
        response_id="response-0",
    )
    block = CompactionBlock(
        action="summarized",
        summary="summary-0",
        compacted_message_ids=("old",),
    )
    message = Message(
        message_id="compaction-summary",
        session_id="session",
        role=Role.USER,
        content=(block,),
        origin=MessageOrigin.COMPACTOR,
        model="summary-model",
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    return factory.create(
        ContextCompactionCompleted(
            action="summarized",
            compaction_message=message,
            compacted_message_ids=("old",),
            pruned_message_ids=(),
            before_tokens=100,
            after_tokens=40,
            usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30),
            summary_provider_name="provider",
            summary_calls=((request, response),),
            selected_summary_index=0,
        ),
        turn_index=1,
    )


async def _start_compaction_parent(projector: TraceProjector) -> None:
    """启动压缩 span 所属的 agent/turn 父级。"""
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )
    await projector.project(_CONTEXT, factory.create(RunStarted(initial_messages=())))
    await projector.project(
        _CONTEXT,
        factory.create(TurnStarted(turn_index=1), turn_index=1),
    )
    await projector.project(
        _CONTEXT,
        factory.create(ContextCompactionStarted(), turn_index=1),
    )


async def test_trace_projects_the_single_selected_summary_call() -> None:
    """唯一摘要调用独立记录，并标识为最终采用的响应。"""
    projector, exporter = _projector(capture_content=True)

    await _start_compaction_parent(projector)
    await projector.project(_CONTEXT, _compaction_event())

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == [
        "compact summary-model attempt 1",
        "context compaction",
    ]
    attributes = spans[0].attributes or {}
    assert attributes["agentsystem.compaction.selected"] is True
    assert "gen_ai.input.messages" in attributes
    assert "gen_ai.output.messages" in attributes

    private_projector, private_exporter = _projector(capture_content=False)
    await _start_compaction_parent(private_projector)
    await private_projector.project(_CONTEXT, _compaction_event())
    for span in private_exporter.get_finished_spans():
        attributes = span.attributes or {}
        assert "gen_ai.input.messages" not in attributes
        assert "gen_ai.output.messages" not in attributes


async def test_trace_pruning_event_does_not_create_llm_span() -> None:
    """纯剪枝没有发生 LLM 调用，因此不创建 client span。"""
    projector, exporter = _projector()

    await _start_compaction_parent(projector)
    await projector.project(_CONTEXT, _compaction_event(pruned=True))

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["context compaction"]


async def test_failed_run_closes_in_flight_compaction_span_as_error() -> None:
    """Started 后压缩异常时由 RunFailed 关闭整体 span。"""
    projector, exporter = _projector()
    await _start_compaction_parent(projector)
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )

    await projector.project(
        _CONTEXT,
        factory.create(
            RunFailed(
                error=RunError(
                    code="context_compaction_failed",
                    message="Context compaction failed",
                    error_id="compaction-error-id",
                    exception_type="ValueError",
                    stacktrace="Traceback ... invalid compaction scan\n",
                ),
                usage=Usage(),
                timing=RunTiming(
                    started_at=_NOW,
                    ended_at=_NOW,
                    elapsed_ms=0,
                    active_ms=0,
                ),
            ),
            turn_index=1,
        ),
    )

    compaction = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "context compaction"
    )
    assert compaction.status.status_code is StatusCode.ERROR
    assert (compaction.attributes or {})["error.type"] == "context_compaction_failed"


async def test_failed_run_records_one_root_exception_event() -> None:
    """最终失败只在 Agent 根 Span 写一个标准异常事件。"""
    projector, exporter = _projector()
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )
    started = factory.create(RunStarted(initial_messages=()))
    failed = factory.create(
        RunFailed(
            error=RunError(
                code="provider_error",
                message="LLM provider 返回错误",
                error_id="opaque-error-id",
                exception_type="agentsystem.common.errors.RunFailure",
                stacktrace="Traceback ... private upstream error\n",
            ),
            usage=Usage(),
            timing=RunTiming(
                started_at=_NOW,
                ended_at=_NOW,
                elapsed_ms=0,
                active_ms=0,
            ),
        )
    )

    await _project_all(projector, (started, failed))

    root = exporter.get_finished_spans()[-1]
    assert root.status.status_code is StatusCode.ERROR
    assert root.status.description == "LLM provider 返回错误"
    assert (root.attributes or {})["error.type"] == "provider_error"
    assert (root.attributes or {})["agentsystem.error.id"] == "opaque-error-id"
    assert len(root.events) == 1
    exception_event = root.events[0]
    assert exception_event.name == "exception"
    assert exception_event.attributes is not None
    assert exception_event.attributes["exception.type"] == (
        "agentsystem.common.errors.RunFailure"
    )
    assert exception_event.attributes["exception.message"] == ("LLM provider 返回错误")
    assert exception_event.attributes["exception.stacktrace"] == (
        "Traceback ... private upstream error\n"
    )


async def test_failed_run_rejects_missing_diagnostics_before_closing_root() -> None:
    """不完整 RunFailed 必须在修改根 Span 前 fail-fast。"""
    projector, exporter = _projector()
    factory = AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    )
    await projector.project(
        _CONTEXT,
        factory.create(RunStarted(initial_messages=())),
    )

    with pytest.raises(RuntimeError, match="RunFailed 缺少完整错误诊断"):
        await projector.project(
            _CONTEXT,
            factory.create(
                RunFailed(
                    error=RunError(code="provider_error", message="safe"),
                    usage=Usage(),
                    timing=RunTiming(
                        started_at=_NOW,
                        ended_at=_NOW,
                        elapsed_ms=0,
                        active_ms=0,
                    ),
                )
            ),
        )

    assert exporter.get_finished_spans() == ()
