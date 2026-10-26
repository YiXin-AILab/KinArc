"""LiteLLMProvider 完整响应归一化测试。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import FinishReason, OutputMode
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.models import (
    LLMBlockDelta,
    LLMBlockEnd,
    LLMBlockStart,
    LLMEnd,
    LLMEvent,
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    LLMStart,
    UsageAccounting,
)
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    Base64Source,
    BlockType,
    DocumentBlock,
    ImageBlock,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    UrlSource,
    UserMediaBlock,
    VideoBlock,
)
from litellm import ModelResponseStream
from litellm.exceptions import APIConnectionError, BadRequestError
from pydantic import SecretStr
from tests.unit.llm.stream_helpers import text_stream


def request(**provider_options: Any) -> LLMRequest:
    """构造最小 LLM 请求。"""
    message = Message(
        message_id="user-1",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        content=(TextBlock(text="hello"),),
        visibility=MessageVisibility.ALL,
    )
    return LLMRequest(
        request_id="request-1",
        messages=(message,),
        config=LLMGenerationConfig(
            model="openai/deepseek-v4-flash",
            provider_options=provider_options,
        ),
        output_mode=OutputMode.COMPLETE,
    )


def media_request(
    block: UserMediaBlock,
) -> LLMRequest:
    """构造文本与一个媒体 Block 混排的 LLMRequest。"""
    message = Message(
        message_id="user-media",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        content=(TextBlock(text="describe"), block),
        visibility=MessageVisibility.ALL,
    )
    return LLMRequest(
        request_id="request-media",
        messages=(message,),
        config=LLMGenerationConfig(model="openai/test-multimodal"),
        output_mode=OutputMode.COMPLETE,
    )


async def collect_response(
    provider: LiteLLMProvider,
    llm_request: LLMRequest,
) -> tuple[list[LLMEvent], LLMResponse]:
    """收集公共事件协议，并返回唯一 LLMEnd 响应。"""
    events = [event async for event in provider.generate(llm_request)]
    assert isinstance(events[0], LLMStart)
    assert isinstance(events[-1], LLMEnd)
    return events, events[-1].response


@pytest.mark.parametrize("output_mode", list(OutputMode))
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "X-Tenant": "test",
            "x-request-id": "stale",
            "X-Request-ID": "old",
            "requestid": "stale-gateway",
            "RequestId": "old-gateway",
        },
    ],
)
async def test_provider_forwards_request_id_without_mutating_headers(
    output_mode: OutputMode, headers: dict[str, str]
) -> None:
    """上游请求与流事件使用同一 ID，配置不能覆盖每次调用的 ID。"""
    captured: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.append(kwargs)
        return text_stream("ok")

    provider = LiteLLMProvider(completion=completion)
    original = request(extra_headers=headers)
    for request_id in ("request-first", "request-retry"):
        current = original.model_copy(
            update={"request_id": request_id, "output_mode": output_mode}
        )
        events, _ = await collect_response(provider, current)
        assert all(event.request_id == request_id for event in events)
        assert captured[-1]["extra_headers"] == {
            **{
                key: value
                for key, value in headers.items()
                if key.lower() not in {"x-request-id", "requestid"}
            },
            "X-Request-ID": request_id,
            "RequestId": request_id,
        }
    assert original.config.provider_options["extra_headers"] == headers


async def test_provider_forwards_request_id_without_configured_headers() -> None:
    """默认请求也必须携带上游关联 ID。"""
    captured: dict[str, Any] = {}

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return text_stream("ok")

    await collect_response(LiteLLMProvider(completion=completion), request())

    assert captured["extra_headers"] == {
        "X-Request-ID": "request-1",
        "RequestId": "request-1",
    }


@pytest.mark.parametrize("output_mode", list(OutputMode))
@pytest.mark.parametrize(
    ("thinking", "text", "has_tool", "finish_reason", "warns"),
    [
        ("private-thought", None, False, "stop", True),
        ("private-thought", " \n", False, "length", True),
        ("private-thought", "answer", False, "stop", False),
        ("private-thought", None, True, "tool_calls", False),
        (None, None, False, "stop", False),
        (" \n", None, False, "stop", False),
    ],
)
async def test_provider_warns_only_for_thinking_without_text_or_tools(
    caplog: pytest.LogCaptureFixture,
    output_mode: OutputMode,
    thinking: str | None,
    text: str | None,
    has_tool: bool,
    finish_reason: str,
    warns: bool,
) -> None:
    """完整响应只有思考内容时告警，保留响应且不记录思考正文。"""

    async def chunks() -> AsyncIterator[ModelResponseStream]:
        delta: dict[str, Any] = {"reasoning_content": thinking, "content": text}
        if has_tool:
            delta["tool_calls"] = [
                {
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ]
        yield ModelResponseStream(
            id="response-1",
            model="test-model",
            choices=[{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        )

    async def completion(**_: Any) -> AsyncIterator[ModelResponseStream]:
        return chunks()

    caplog.set_level(logging.WARNING)
    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request().model_copy(update={"output_mode": output_mode}),
    )

    records = [
        record
        for record in caplog.records
        if record.name == "agentsystem.llm.litellm_provider"
    ]
    assert len(records) == int(warns)
    if warns:
        assert records[0].levelno == logging.WARNING
        assert "llm_thinking_only_response request_id=request-1" in caplog.text
        assert f"finish_reason={finish_reason}" in caplog.text
        assert "response_id=response-1" in caplog.text
    assert "private-thought" not in caplog.text
    assert response.error is None
    assert response.finish_reason.value == finish_reason


async def test_provider_rejects_unprojected_internal_block_message() -> None:
    """Injection 必须先由 ContextBuilder 投影，不能直接发送给 Provider。"""
    called = False

    async def completion(**_: Any) -> AsyncIterator[ModelResponseStream]:
        nonlocal called
        called = True
        raise AssertionError("内部 Block 不应到达 Provider")

    internal = Message(
        message_id="injection",
        session_id="session",
        role=Role.USER,
        content=(InjectedBlock(target_user_message_id="user"),),
        origin=MessageOrigin.CONTEXT_PROVIDER,
        visibility=MessageVisibility.MODEL,
        run_id="run-1",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    llm_request = request().model_copy(update={"messages": (internal,)})
    iterator = LiteLLMProvider(completion=completion).generate(llm_request)

    assert isinstance(await anext(iterator), LLMStart)
    with pytest.raises(AdapterError, match="模型适配器执行失败"):
        await anext(iterator)
    assert called is False


async def test_canceling_generation_closes_upstream_stream() -> None:
    """Run Task 取消模型读取时必须及时归还上游连接资源。"""

    class BlockingStream:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        def __aiter__(self) -> "BlockingStream":
            return self

        async def __anext__(self) -> ModelResponseStream:
            self.started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed.set()

    stream = BlockingStream()

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        del kwargs
        return cast(AsyncIterator[ModelResponseStream], stream)

    iterator = LiteLLMProvider(completion=completion).generate(request())
    assert isinstance(await anext(iterator), LLMStart)
    pending: asyncio.Future[LLMEvent] = asyncio.ensure_future(anext(iterator))
    await stream.started.wait()
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending

    assert stream.closed.is_set()


async def test_provider_maps_ordered_image_url_content() -> None:
    """图片 URL 必须保持与文本的 Block 顺序并保留 detail。"""
    captured: dict[str, Any] = {}

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return text_stream("ok")

    provider = LiteLLMProvider(
        completion=completion,
    )
    _, response = await collect_response(
        provider,
        media_request(
            ImageBlock(
                source=UrlSource(
                    url="http://public.example/image.png",
                    media_type="image/png",
                ),
                detail="high",
            )
        ),
    )

    assert response.finish_reason is FinishReason.STOP
    assert captured["messages"][0]["content"] == [
        {"type": "text", "text": "describe"},
        {
            "type": "image_url",
            "image_url": {
                "url": "http://public.example/image.png",
                "detail": "high",
            },
        },
    ]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            AudioBlock(source=Base64Source(data="YXVkaW8=", media_type="audio/mpeg")),
            {
                "type": "input_audio",
                "input_audio": {"data": "YXVkaW8=", "format": "mp3"},
            },
        ),
        (
            VideoBlock(source=Base64Source(data="dmlkZW8=", media_type="video/mp4")),
            {
                "type": "video_url",
                "video_url": {"url": "data:video/mp4;base64,dmlkZW8="},
            },
        ),
        (
            DocumentBlock(
                source=UrlSource(
                    url="https://public.example/report.pdf",
                    media_type="application/pdf",
                )
            ),
            {
                "type": "image_url",
                "image_url": {"url": "https://public.example/report.pdf"},
            },
        ),
    ],
)
async def test_provider_maps_audio_video_and_document(
    block: UserMediaBlock,
    expected: dict[str, object],
) -> None:
    """三类媒体使用 LiteLLM 可表达的 URL/Base64 content item。"""
    captured: dict[str, Any] = {}

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return text_stream("ok")

    await collect_response(
        LiteLLMProvider(
            completion=completion,
        ),
        media_request(block),
    )

    assert captured["messages"][0]["content"][1] == expected


async def test_provider_rejects_unprepared_asset_source() -> None:
    """AssetSource 到达 Provider 表示 LLMClient 调用边界被绕过。"""

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        del kwargs
        return text_stream("unused")

    provider = LiteLLMProvider(completion=completion)
    iterator = provider.generate(
        media_request(
            ImageBlock(
                source=AssetSource(
                    asset_id="sha256-" + "a" * 64,
                    media_type="image/png",
                )
            )
        )
    )

    assert isinstance(await anext(iterator), LLMStart)
    with pytest.raises(AdapterError, match="模型适配器执行失败"):
        await anext(iterator)


async def test_audio_request_reaches_provider_without_capability_preflight() -> None:
    """缺少音频能力元数据时仍必须调用 Provider 并保留拒绝详情。"""
    called = False

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        nonlocal called
        del kwargs
        called = True
        raise BadRequestError(
            "provider audio rejection detail",
            model="gemini/gemini-2.5-flash",
            llm_provider="gemini",
        )

    llm_request = media_request(
        AudioBlock(source=Base64Source(data="YXVkaW8=", media_type="audio/mpeg"))
    ).model_copy(
        update={"config": LLMGenerationConfig(model="gemini/gemini-2.5-flash")}
    )
    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        llm_request,
    )

    assert called is True
    assert response.error is not None
    assert response.error.code == "provider_rejected_media"
    assert "provider audio rejection detail" in response.error.message


async def test_provider_media_rejection_preserves_error() -> None:
    """Provider 媒体拒绝必须保留原始正文供内部 Trace 诊断。"""
    called = False

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        nonlocal called
        del kwargs
        called = True
        raise BadRequestError(
            "invalid image provider rejection",
            model="openai/test-model",
            llm_provider="openai",
        )

    _, response = await collect_response(
        LiteLLMProvider(
            completion=completion,
        ),
        media_request(
            ImageBlock(source=Base64Source(data="aW1hZ2U=", media_type="image/png"))
        ),
    )

    assert called is True
    assert response.error is not None
    assert response.error.code == "provider_rejected_media"
    assert "invalid image provider rejection" in response.error.message


async def test_provider_normalizes_text_reasoning_usage_and_options() -> None:
    """完整响应被转换为 Thinking/Text Block 和精确 usage。"""
    captured: dict[str, Any] = {}

    async def chunks() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id="response-1",
            model="deepseek-v4-flash",
            choices=[
                {
                    "finish_reason": None,
                    "delta": {"reasoning_content": "think"},
                }
            ],
        )
        yield ModelResponseStream(
            id="response-1",
            model="deepseek-v4-flash",
            choices=[{"finish_reason": None, "delta": {"content": "Hello!"}}],
        )
        yield ModelResponseStream(
            id="response-1",
            model="deepseek-v4-flash",
            choices=[{"finish_reason": "stop", "delta": {}}],
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {
                    "cached_tokens": 7,
                    "cache_write_tokens": 2,
                },
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        )

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return chunks()

    provider = LiteLLMProvider(
        api_key=SecretStr("secret"),
        api_base="https://example.invalid",
        completion=completion,
    )
    events, response = await collect_response(
        provider,
        request(
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        ),
    )

    assert [type(event) for event in events] == [
        LLMStart,
        LLMBlockStart,
        LLMBlockDelta,
        LLMBlockEnd,
        LLMBlockStart,
        LLMBlockDelta,
        LLMBlockEnd,
        LLMEnd,
    ]
    assert [block.type for block in response.blocks] == ["thinking", "text"]
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.accounting is UsageAccounting.EXACT
    assert response.usage.total_tokens == 15
    assert response.usage.cache_read_tokens == 7
    assert response.usage.cache_write_tokens == 2
    assert response.usage.reasoning_tokens == 3
    assert captured["api_key"] == "secret"
    assert captured["api_base"] == "https://example.invalid"
    assert captured["reasoning_effort"] == "high"
    assert captured["allowed_openai_params"] == ["reasoning_effort"]


async def test_provider_omits_parallel_tool_calls_without_tools() -> None:
    """无工具请求不得向 Provider 发送 parallel_tool_calls。"""
    captured: dict[str, Any] = {}

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return text_stream("ok")

    llm_request = request()
    llm_request = llm_request.model_copy(
        update={
            "config": llm_request.config.model_copy(
                update={"parallel_tool_calls": False}
            )
        }
    )

    await collect_response(LiteLLMProvider(completion=completion), llm_request)

    assert "parallel_tool_calls" not in captured


async def test_provider_sends_parallel_tool_calls_with_tools() -> None:
    """有工具请求必须保留显式 parallel_tool_calls 配置。"""
    captured: dict[str, Any] = {}

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return text_stream("ok")

    llm_request = request()
    llm_request = llm_request.model_copy(
        update={
            "tools": (
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "查询信息",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ),
            "config": llm_request.config.model_copy(
                update={"parallel_tool_calls": False}
            ),
        }
    )

    await collect_response(LiteLLMProvider(completion=completion), llm_request)

    assert captured["parallel_tool_calls"] is False


async def test_provider_normalizes_top_level_usage_fallbacks() -> None:
    """非 OpenAI 提供方的顶层 Usage 明细也必须进入领域模型。"""

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        yield {
            "id": "response-top-level-usage",
            "model": "test-model",
            "choices": [{"finish_reason": "stop", "delta": {}}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
                "cache_read_input_tokens": 6,
                "cache_creation_input_tokens": 1,
                "reasoning_tokens": 2,
            },
        }

    async def completion(**_: Any) -> AsyncIterator[dict[str, Any]]:
        return chunks()

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.usage.cache_read_tokens == 6
    assert response.usage.cache_write_tokens == 1
    assert response.usage.reasoning_tokens == 2


async def test_provider_prefers_nested_usage_details_for_mapping_chunks() -> None:
    """原始 Mapping 同时含两种字段时优先采用 OpenAI 嵌套明细。"""

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        yield {
            "id": "response-preferred-usage",
            "model": "test-model",
            "choices": [{"finish_reason": "stop", "delta": {}}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
                "cache_read_input_tokens": 6,
                "cache_creation_input_tokens": 5,
                "reasoning_tokens": 4,
                "prompt_tokens_details": {
                    "cached_tokens": 3,
                    "cache_write_tokens": 2,
                },
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
        }

    async def completion(**_: Any) -> AsyncIterator[dict[str, Any]]:
        return chunks()

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.usage.cache_read_tokens == 3
    assert response.usage.cache_write_tokens == 2
    assert response.usage.reasoning_tokens == 1


async def test_provider_ignores_invalid_optional_usage_counts() -> None:
    """bool、负数和非数字 Usage 明细不得污染领域 Token 统计。"""

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        yield {
            "id": "response-invalid-usage",
            "model": "test-model",
            "choices": [{"finish_reason": "stop", "delta": {}}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
                "cache_read_input_tokens": True,
                "cache_creation_input_tokens": -1,
                "reasoning_tokens": "invalid",
            },
        }

    async def completion(**_: Any) -> AsyncIterator[dict[str, Any]]:
        return chunks()

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.usage.cache_read_tokens is None
    assert response.usage.cache_write_tokens is None
    assert response.usage.reasoning_tokens is None


async def test_provider_normalizes_multiple_tool_calls() -> None:
    """多个 tool call 保持原始顺序并解析 JSON arguments。"""

    async def chunks() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id="response-2",
            model="test-model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "first",
                                    "arguments": '{"value":1}',
                                },
                            },
                            {
                                "index": 1,
                                "id": "call-2",
                                "function": {
                                    "name": "second",
                                    "arguments": "not-json",
                                },
                            },
                        ],
                    },
                }
            ],
            usage={},
        )

    async def completion(**_: Any) -> AsyncIterator[ModelResponseStream]:
        return chunks()

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.finish_reason is FinishReason.TOOL_CALLS
    calls = [block for block in response.blocks if isinstance(block, ToolCallBlock)]
    assert [call.tool_call_id for call in calls] == ["call-1", "call-2"]
    assert calls[0].arguments == {"value": 1}
    assert calls[1].arguments is None
    assert calls[1].parse_error is not None


async def test_provider_converts_timeout_to_error_response() -> None:
    """Provider timeout 归一化为可重试 LLMError，而不是泄漏 SDK 异常。"""

    async def completion(**_: Any) -> dict[str, Any]:
        raise TimeoutError("provider timeout")

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.finish_reason is FinishReason.ERROR
    assert response.error is not None
    assert response.error.code == "timeout"
    assert response.error.retryable is True


@pytest.mark.parametrize("during_stream", [False, True])
@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError])
async def test_provider_logs_failed_request_id_without_error_body(
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    during_stream: bool,
) -> None:
    """请求或流读取失败都记录关联 ID，原始正文和堆栈留给最终边界。"""

    async def chunks() -> AsyncIterator[ModelResponseStream]:
        raise error_type("private-provider-body")
        yield  # pragma: no cover

    async def completion(**_: Any) -> AsyncIterator[ModelResponseStream]:
        if during_stream:
            return chunks()
        raise error_type("private-provider-body")

    caplog.set_level(logging.WARNING)
    provider = LiteLLMProvider(completion=completion)
    if error_type is RuntimeError:
        with pytest.raises(AdapterError):
            await collect_response(provider, request())
    else:
        _, response = await collect_response(provider, request())
        assert response.error is not None

    records = [
        record
        for record in caplog.records
        if record.name == "agentsystem.llm.litellm_provider"
    ]
    assert len(records) == 1
    assert "request_id=request-1" in records[0].getMessage()
    assert "llm_request_failed" in records[0].getMessage()
    assert records[0].exc_info is None
    assert "private-provider-body" not in caplog.text


async def test_provider_preserves_raw_http_error_response() -> None:
    """HTTP response 正文必须优先于 LiteLLM 包装后的异常文本。"""
    response_body = '{"error":{"message":"unknown model"}}'
    provider_response = httpx.Response(
        400,
        text=response_body,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )

    async def completion(**_: Any) -> dict[str, Any]:
        raise BadRequestError(
            "litellm wrapped error",
            model="test-model",
            llm_provider="test-provider",
            response=provider_response,
        )

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.error is not None
    assert response.error.message == response_body


async def test_provider_falls_back_to_exception_message() -> None:
    """HTTP response 不可用时必须保留异常文本供内部 Trace 诊断。"""

    async def completion(**_: Any) -> dict[str, Any]:
        raise APIConnectionError(
            "provider connection rejected",
            model="test-model",
            llm_provider="test-provider",
        )

    _, response = await collect_response(
        LiteLLMProvider(completion=completion),
        request(),
    )

    assert response.error is not None
    assert "provider connection rejected" in response.error.message


async def test_provider_propagates_unknown_adapter_bug() -> None:
    """未知程序异常不得伪装成可交给模型处理的 Provider 错误。"""

    async def completion(**_: Any) -> dict[str, Any]:
        raise RuntimeError("adapter bug")

    with pytest.raises(AdapterError) as captured:
        _ = [
            event
            async for event in LiteLLMProvider(completion=completion).generate(
                request()
            )
        ]

    assert captured.value.code == "llm_adapter_failed"
    assert isinstance(captured.value.__cause__, RuntimeError)


async def test_stream_provider_aggregates_real_litellm_tool_chunks() -> None:
    """漏拼工具名称或参数 delta 时，本测试必须产生错误的最终 ToolCall。"""
    captured: dict[str, Any] = {}

    async def chunks() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id="response-stream",
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "delta": {"reasoning_content": "先查询"},
                    "finish_reason": None,
                }
            ],
        )
        yield ModelResponseStream(
            id="response-stream",
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "web_",
                                    "arguments": '{"query":"上',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        )
        yield ModelResponseStream(
            id="response-stream",
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "search",
                                    "arguments": '海天气"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        )
        yield ModelResponseStream(
            id="response-stream",
            model="test-model",
            choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        )

    async def completion(**kwargs: Any) -> AsyncIterator[ModelResponseStream]:
        captured.update(kwargs)
        return chunks()

    stream_request = request().model_copy(update={"output_mode": OutputMode.STREAM})
    provider = LiteLLMProvider(completion=completion)
    events = [event async for event in provider.generate(stream_request)]

    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert isinstance(events[0], LLMStart)
    assert [type(event) for event in events[1:-1]] == [
        LLMBlockStart,
        LLMBlockDelta,
        LLMBlockEnd,
        LLMBlockStart,
        LLMBlockDelta,
        LLMBlockDelta,
        LLMBlockEnd,
    ]
    assert isinstance(events[-1], LLMEnd)
    assert isinstance(events[2], LLMBlockDelta)
    assert events[2].block_type is BlockType.THINKING
    assert isinstance(events[5], LLMBlockDelta)
    assert events[5].block_type is BlockType.TOOL_CALL
    assert isinstance(events[6], LLMBlockDelta)
    assert events[6].block_type is BlockType.TOOL_CALL
    assert [block.type for block in events[-1].response.blocks] == [
        "thinking",
        "tool_call",
    ]
    call = events[-1].response.blocks[1]
    assert isinstance(call, ToolCallBlock)
    assert call.tool_name == "web_search"
    assert call.arguments == {"query": "上海天气"}
    assert events[-1].response.usage.total_tokens == 15
