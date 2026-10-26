"""本地或远程 Langfuse 的真实 OTLP/HTTP 摄取测试。"""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agentsystem.common.models import FinishReason, OutputMode, SessionContext
from agentsystem.config.models import RuntimeTraceConfig
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
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolResultBlock,
)
from agentsystem.runtime.events import (
    AgentEventFactory,
    LLMCallCompleted,
    LLMCallStarted,
    RunCompleted,
    RunStarted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    TurnCompleted,
    TurnStarted,
)
from agentsystem.runtime.state import RunTiming
from agentsystem.trace.otel import OTelTraceRecorder, create_otel_provider
from agentsystem.trace.projector import TraceProjector
from tests.langfuse import (
    parse_langfuse_otel_connection,
    wait_for_langfuse_observations,
)

_OTEL_ENDPOINT = os.environ.get("OTEL_ENDPOINT")
_OTEL_AUTHORIZATION = os.environ.get("OTEL_AUTHORIZATION")
_CONNECTION = (
    parse_langfuse_otel_connection(_OTEL_ENDPOINT, _OTEL_AUTHORIZATION)
    if _OTEL_ENDPOINT and _OTEL_AUTHORIZATION
    else None
)
_BASE_URL, _PUBLIC_KEY, _SECRET_KEY = _CONNECTION or ("", "", "")
pytestmark = pytest.mark.skipif(
    _CONNECTION is None,
    reason="需要 OTEL_ENDPOINT/OTEL_AUTHORIZATION 显式启用真实摄取测试",
)


async def test_langfuse_receives_projected_otel_span_tree() -> None:
    """真实链路必须可查询 agent→turn→LLM/tool 完整 Span 树。"""
    suffix = uuid4().hex
    session_id = f"otel-live-{suffix}"
    run_id = f"run-{suffix}"
    trace_id = f"trace-{suffix}"
    marker = f"Langfuse 输入测试 {suffix}"
    encoded_media = "dGVzdA=="
    now = datetime.now(UTC)
    context = SessionContext(
        app_id="integration-test",
        user_id="otel-test-user",
        agent_id="otel-test-agent",
        session_id=session_id,
    )
    factory = AgentEventFactory(
        session_id=session_id,
        run_id=run_id,
        trace_id=trace_id,
        clock=lambda: now,
    )
    final_message = Message(
        message_id=f"message-{suffix}",
        session_id=session_id,
        role=Role.ASSISTANT,
        content=(TextBlock(text="ok"),),
        origin=MessageOrigin.MODEL,
        run_id=run_id,
        created_at=now,
        visibility=MessageVisibility.ALL,
    )
    request_message = Message(
        message_id=f"request-message-{suffix}",
        session_id=session_id,
        role=Role.USER,
        content=(
            ImageBlock(
                source=AssetSource(
                    asset_id=f"sha256-{suffix}",
                    media_type="image/jpeg",
                    filename="trace-image.jpg",
                )
            ),
            TextBlock(text=marker),
            ImageBlock(
                source=Base64Source(
                    data=encoded_media,
                    media_type="image/png",
                )
            ),
        ),
        origin=MessageOrigin.USER,
        run_id=run_id,
        created_at=now,
        visibility=MessageVisibility.ALL,
    )
    llm_request = LLMRequest(
        request_id="request-1",
        messages=(request_message,),
        config=LLMGenerationConfig(model="integration-model"),
        output_mode=OutputMode.COMPLETE,
    )
    response = LLMResponse(
        blocks=(TextBlock(text="ok"),),
        usage=Usage(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            accounting=UsageAccounting.EXACT,
            cache_read_tokens=80,
            cache_write_tokens=5,
            reasoning_tokens=10,
        ),
        timing=Timing(started_at=now, ended_at=now, duration_ms=0),
        model="integration-model",
        finish_reason=FinishReason.STOP,
        response_id=f"response-{suffix}",
    )
    tool_result = ToolResultBlock(
        tool_call_id="tool-call-1",
        tool_name="integration_tool",
        content="tool-ok",
    )
    tool_message = Message(
        message_id=f"tool-message-{suffix}",
        session_id=session_id,
        role=Role.TOOL,
        content=(tool_result,),
        origin=MessageOrigin.TOOL,
        run_id=run_id,
        created_at=now,
        visibility=MessageVisibility.ALL,
    )
    recorder = OTelTraceRecorder(
        create_otel_provider(
            RuntimeTraceConfig(
                enabled=True,
                service_name="langfuse-integration-test",
                endpoint=_OTEL_ENDPOINT or "",
                headers={
                    "Authorization": _OTEL_AUTHORIZATION or "",
                    "x-langfuse-ingestion-version": "4",
                },
                timeout_seconds=10,
                batch_schedule_delay_ms=100,
            )
        )
    )
    projector = TraceProjector(recorder, capture_content=True)

    events = (
        factory.create(RunStarted(initial_messages=(request_message,))),
        factory.create(TurnStarted(turn_index=1), turn_index=1),
        factory.create(
            LLMCallStarted(
                request_id="request-1",
                provider_name="integration",
                model="integration-model",
                output_mode=OutputMode.COMPLETE,
                request=llm_request,
            ),
            turn_index=1,
        ),
        factory.create(
            LLMCallCompleted(request_id="request-1", response=response),
            turn_index=1,
        ),
        factory.create(
            ToolExecutionStarted(
                tool_call_id="tool-call-1",
                tool_name="integration_tool",
                arguments={"value": 1},
                raw_arguments='{"value":1}',
            ),
            turn_index=1,
        ),
        factory.create(
            ToolExecutionCompleted(
                tool_call_id="tool-call-1",
                tool_name="integration_tool",
                result=tool_result,
                message=tool_message,
                timing=Timing(started_at=now, ended_at=now, duration_ms=0),
            ),
            turn_index=1,
        ),
        factory.create(TurnCompleted(turn_index=1), turn_index=1),
        factory.create(
            RunCompleted(
                final_message=final_message,
                usage=Usage(),
                timing=RunTiming(
                    started_at=now,
                    ended_at=now,
                    elapsed_ms=0,
                    active_ms=0,
                ),
            ),
            turn_index=1,
        ),
    )
    for event in events:
        await projector.project(context, event)
    assert await recorder.force_flush(10)
    await recorder.shutdown(10)

    observations = await wait_for_langfuse_observations(
        base_url=_BASE_URL,
        public_key=_PUBLIC_KEY,
        secret_key=_SECRET_KEY,
        marker=marker,
        required_types={"AGENT", "SPAN", "GENERATION", "TOOL"},
    )
    serialized = json.dumps(observations, ensure_ascii=False)
    assert marker in serialized
    assert encoded_media not in serialized
    by_name = {observation.get("name"): observation for observation in observations}
    assert {
        "invoke_agent",
        "agent turn",
        "chat integration-model",
        "integration_tool",
    }.issubset(by_name)
    root = by_name["invoke_agent"]
    turn = by_name["agent turn"]
    generation = by_name["chat integration-model"]
    tool = by_name["integration_tool"]
    assert root.get("parentObservationId") is None
    assert turn.get("parentObservationId") == root.get("id")
    safe_tree = [
        {
            "name": item.get("name"),
            "id": item.get("id"),
            "parentObservationId": item.get("parentObservationId"),
            "type": item.get("type"),
            "traceId": item.get("traceId"),
        }
        for item in observations
    ]
    assert generation.get("parentObservationId") == turn.get("id"), safe_tree
    assert tool.get("parentObservationId") == turn.get("id"), safe_tree
    assert len({item.get("traceId") for item in (root, turn, generation, tool)}) == 1
    assert root.get("type") == "AGENT"
    assert turn.get("type") == "SPAN"
    assert generation.get("type") == "GENERATION"
    assert tool.get("type") == "TOOL"
    expected_input = [
        {
            "role": "user",
            "parts": [
                {
                    "type": "uri",
                    "modality": "image",
                    "mime_type": "image/jpeg",
                    "uri": f"asset://sha256-{suffix}",
                    "filename": "trace-image.jpg",
                },
                {"type": "text", "content": marker},
                {
                    "type": "blob",
                    "modality": "image",
                    "mime_type": "image/png",
                    "base64_chars": len(encoded_media),
                    "content_omitted": True,
                },
            ],
        }
    ]
    assert _as_json(root.get("input")) == expected_input
    assert "ok" in json.dumps(_as_json(root.get("output")), ensure_ascii=False)
    assert generation.get("input") is not None
    assert generation.get("output") is not None
    assert _as_json(generation.get("input")) == expected_input
    assert "ok" in json.dumps(generation.get("output"), ensure_ascii=False)
    usage_details = generation.get("usageDetails")
    assert isinstance(usage_details, dict)
    assert usage_details.get("input_cached_tokens") == 80
    assert usage_details.get("input_cache_creation") == 5
    # Langfuse 4 normalizes the OTel reasoning field to output_reasoning_tokens.
    assert (
        usage_details.get(
            "output_reasoning_tokens", usage_details.get("reasoning.output_tokens")
        )
        == 10
    )


def _as_json(value: object) -> object:
    """兼容 Langfuse API 返回结构对象或 JSON 字符串。"""
    if isinstance(value, str):
        return json.loads(value)
    return value
