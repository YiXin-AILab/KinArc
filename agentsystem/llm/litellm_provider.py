"""基于 LiteLLM acompletion 的完整响应 adapter。"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from litellm import acompletion
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    BadRequestError,
    ContentPolicyViolationError,
    ContextWindowExceededError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
    UnprocessableEntityError,
)
from pydantic import SecretStr, TypeAdapter

from agentsystem.common.errors import AdapterError, LLMError
from agentsystem.common.models import FinishReason, JsonValue
from agentsystem.llm.models import (
    LLMBlockDelta,
    LLMBlockEnd,
    LLMBlockStart,
    LLMEnd,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    LLMStart,
    Timing,
    Usage,
    UsageAccounting,
)
from agentsystem.message.models import (
    AssetSource,
    AssistantBlock,
    AudioBlock,
    Base64Source,
    BlockType,
    CompactionBlock,
    DocumentBlock,
    ImageBlock,
    InjectedBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UrlSource,
    UserMediaBlock,
    VideoBlock,
)

type Completion = Callable[..., Awaitable[Any]]
_LOGGER = logging.getLogger(__name__)
_ARGUMENTS_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(
    dict[str, JsonValue]
)
_HEADERS_ADAPTER = TypeAdapter(dict[str, str])
_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}
_RESERVED_OPTIONS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "timeout",
    "tools",
}
_EXPECTED_PROVIDER_ERRORS = (
    TimeoutError,
    Timeout,
    ContextWindowExceededError,
    BadRequestError,
    NotFoundError,
    ContentPolicyViolationError,
    InternalServerError,
    AuthenticationError,
    APIError,
    APIConnectionError,
    RateLimitError,
    ServiceUnavailableError,
    PermissionDeniedError,
    UnprocessableEntityError,
)


@dataclass(slots=True)
class _OpenBlock:
    """单个流式 Block 的 Provider 内部聚合状态。"""

    index: int
    block_id: str
    block_type: BlockType
    content: str = ""
    tool_call_id: str = ""
    tool_name: str = ""


@dataclass(slots=True)
class _StreamState:
    """一次 LiteLLM stream 的可变聚合状态。"""

    blocks: list[AssistantBlock]
    open_blocks: dict[tuple[BlockType, int], _OpenBlock]
    finish_reason: str = "stop"
    response_id: str | None = None
    model: str | None = None
    usage: Usage | None = None
    next_index: int = 0
    first_token_ms: float | None = None


class LiteLLMProvider:
    """隔离 LiteLLM 请求、响应类型和异常的异步 adapter。

    每次调用使用独立聚合状态，内部始终消费流式响应并在退出时关闭上游流。
    增量事件之外还产生唯一完整响应；预期调用错误归一化，未预期异常交由上层处理。
    """

    def __init__(
        self,
        *,
        api_key: SecretStr | None = None,
        api_base: str | None = None,
        timeout_seconds: float = 60.0,
        completion: Completion | None = None,
    ) -> None:
        """保存凭证和可注入 completion，便于确定性测试。

        Args:
            api_key: 可选提供方密钥，仅构造调用参数时解包。
            api_base: 可选模型服务地址。
            timeout_seconds: 传给 LiteLLM 的调用超时秒数。
            completion: 注入的异步调用入口；缺省使用 acompletion。

        Raises:
            ValueError: 超时不是正数。
        """
        if timeout_seconds <= 0:
            raise ValueError("LLM timeout_seconds 必须为正数")
        self._api_key = api_key
        self._api_base = api_base
        self._timeout_seconds = timeout_seconds
        self._completion = completion or cast(Completion, acompletion)

    @property
    def provider_name(self) -> str:
        """返回稳定 Provider 标识。"""
        return "litellm"

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """惰性调用 acompletion(stream=True) 并产生规范化事件。

        调用与流消费中的预期错误进入结束响应，未预期错误包装为 AdapterError。
        finally 中关闭上游流的异常原样传播，此时不保证产生结束事件。

        Args:
            request: 已准备好媒体来源的模型请求。

        Yields:
            开始、块边界与增量事件，以及携带完整成功或错误响应的结束事件。

        Raises:
            AdapterError: 参数映射、响应解析或 SDK 调用及流消费发生未预期错误。
        """
        started_at = datetime.now(UTC)
        started = monotonic()
        yield LLMStart(
            request_id=request.request_id,
            created_at=started_at,
            model=request.config.model,
            output_mode=request.output_mode,
        )
        state = _StreamState(blocks=[], open_blocks={})
        stream: object | None = None
        try:
            stream = await self._completion(**await self._request_arguments(request))
            async for raw in cast(AsyncIterator[Any], stream):
                for event in self._consume_chunk(raw, request, state, started):
                    yield event
            for event in self._finish_open_blocks(request, state):
                yield event
            response = self._stream_response(state, request, started_at, started)
        except Exception as error:
            # 仅记录安全关联摘要，完整异常链仍由最终所有权边界记录一次。
            _LOGGER.warning(
                "llm_request_failed request_id=%s exception_type=%s",
                request.request_id,
                type(error).__name__,
            )
            if isinstance(error, (AdapterError, *_EXPECTED_PROVIDER_ERRORS)):
                response = self._error_response(error, request, started_at, started)
            else:
                raise AdapterError(
                    operation="llm.generate",
                    code="llm_adapter_failed",
                    message="模型适配器执行失败",
                ) from error
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                close_result = close()
                if isawaitable(close_result):
                    await close_result
        yield LLMEnd(
            request_id=request.request_id,
            created_at=datetime.now(UTC),
            response=response,
        )

    async def _request_arguments(self, request: LLMRequest) -> dict[str, Any]:
        """构造 SDK 参数并保护保留字段，强制使用本次 request_id 覆盖关联请求头。"""
        options = dict(request.config.provider_options)
        overlap = _RESERVED_OPTIONS.intersection(options)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"provider_options 不能覆盖保留参数：{names}")
        arguments: dict[str, Any] = {
            "model": request.config.model,
            "messages": [
                await self._message_to_provider(item) for item in request.messages
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": self._timeout_seconds,
        }
        if request.tools:
            arguments["tools"] = [dict(tool) for tool in request.tools]
        if self._api_key is not None:
            arguments["api_key"] = self._api_key.get_secret_value()
        if self._api_base is not None:
            arguments["api_base"] = self._api_base
        optional = {
            "temperature": request.config.temperature,
            "max_tokens": request.config.max_output_tokens,
            "tool_choice": request.config.tool_choice,
        }
        arguments.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        if request.tools and request.config.parallel_tool_calls is not None:
            arguments["parallel_tool_calls"] = request.config.parallel_tool_calls
        if request.config.stop:
            arguments["stop"] = list(request.config.stop)
        arguments.update(options)
        headers = _HEADERS_ADAPTER.validate_python(
            arguments.get("extra_headers", {}), strict=True
        )
        # HTTP 头大小写不敏感；配置中的旧 ID 不能覆盖本次调用的关联 ID。
        arguments["extra_headers"] = {
            **{
                key: value
                for key, value in headers.items()
                if key.lower() not in {"x-request-id", "requestid"}
            },
            "X-Request-ID": request.request_id,
            "RequestId": request.request_id,
        }
        if "reasoning_effort" in options:
            arguments["allowed_openai_params"] = ["reasoning_effort"]
        return arguments

    async def _message_to_provider(
        self,
        message: Message,
    ) -> dict[str, Any]:
        """把已投影的消息转为 SDK 载荷，拒绝尚未渲染的内部上下文块。"""
        if isinstance(message.content[0], (CompactionBlock, InjectedBlock)):
            raise ValueError("内部 Compaction/Injection Message 必须先完成投影")
        if message.role is Role.TOOL:
            tool_result = message.content[0]
            if not isinstance(tool_result, ToolResultBlock):
                raise ValueError("TOOL Message 缺少 ToolResultBlock")
            return {
                "role": "tool",
                "tool_call_id": tool_result.tool_call_id,
                "content": tool_result.content,
            }
        media_blocks = tuple(
            block for block in message.content if isinstance(block, _MEDIA_BLOCKS)
        )
        content: str | list[dict[str, Any]]
        if media_blocks:
            content = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, _MEDIA_BLOCKS):
                    content.append(self._media_item(block))
        else:
            content = "".join(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
        payload: dict[str, Any] = {
            "role": message.role.value,
            "content": content,
        }
        tool_calls = [
            block for block in message.content if isinstance(block, ToolCallBlock)
        ]
        if tool_calls:
            payload["tool_calls"] = [
                {
                    "id": block.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": block.tool_name,
                        "arguments": block.raw_arguments,
                    },
                }
                for block in tool_calls
            ]
        return payload

    def _media_item(
        self,
        block: UserMediaBlock,
    ) -> dict[str, Any]:
        source = block.source
        if isinstance(block, ImageBlock):
            url = _provider_media_url(source)
            return {
                "type": "image_url",
                "image_url": {"url": url, "detail": block.detail},
            }
        if isinstance(block, AudioBlock):
            if not isinstance(source, Base64Source):
                raise ValueError("Provider-ready AudioBlock 必须使用 Base64Source")
            return {
                "type": "input_audio",
                "input_audio": {
                    "data": source.data,
                    "format": _audio_format(source.media_type),
                },
            }
        if isinstance(block, VideoBlock):
            return {
                "type": "video_url",
                "video_url": {"url": _provider_media_url(source)},
            }
        return {
            "type": "image_url",
            "image_url": {"url": _provider_media_url(source)},
        }

    def _consume_chunk(
        self,
        raw: Any,
        request: LLMRequest,
        state: _StreamState,
        started: float,
    ) -> tuple[LLMBlockStart | LLMBlockDelta | LLMBlockEnd, ...]:
        """消费一个真实 LiteLLM chunk，并返回当下形成的边界事件。"""
        self._record_chunk_metadata(raw, state)
        choices = cast(Sequence[Any], _value(raw, "choices", ()) or ())
        if not choices:
            return ()
        choice = choices[0]
        finish = _value(choice, "finish_reason", None)
        if finish:
            state.finish_reason = str(finish)
        delta = _value(choice, "delta", {})
        events: list[LLMBlockStart | LLMBlockDelta | LLMBlockEnd] = []
        reasoning = _optional_text(_value(delta, "reasoning_content", None))
        content = _optional_text(_value(delta, "content", None))
        if reasoning:
            events.extend(
                self._consume_text_delta(
                    request, state, BlockType.THINKING, reasoning, started
                )
            )
        if content:
            events.extend(
                self._consume_text_delta(
                    request, state, BlockType.TEXT, content, started
                )
            )
        tool_calls = cast(Sequence[Any], _value(delta, "tool_calls", ()) or ())
        for tool_call in tool_calls:
            events.extend(self._consume_tool_delta(request, state, tool_call, started))
        return tuple(events)

    def _consume_text_delta(
        self,
        request: LLMRequest,
        state: _StreamState,
        block_type: BlockType,
        delta: str,
        started: float,
    ) -> tuple[LLMBlockStart | LLMBlockDelta | LLMBlockEnd, ...]:
        events: list[LLMBlockStart | LLMBlockDelta | LLMBlockEnd] = list(
            self._close_other_text_blocks(request, state, block_type)
        )
        block, start_event = self._ensure_block(request, state, block_type, 0)
        if start_event is not None:
            events.append(start_event)
        block.content += delta
        self._record_first_token(state, started)
        events.append(self._delta_event(request, block, delta))
        return tuple(events)

    def _consume_tool_delta(
        self,
        request: LLMRequest,
        state: _StreamState,
        raw_call: Any,
        started: float,
    ) -> tuple[LLMBlockStart | LLMBlockDelta | LLMBlockEnd, ...]:
        """按上游工具序号累积名称、调用 ID 和参数，参数仅在块闭合后解析。"""
        events: list[LLMBlockStart | LLMBlockDelta | LLMBlockEnd] = list(
            self._close_non_tool_blocks(request, state)
        )
        provider_index = int(_value(raw_call, "index", 0) or 0)
        function = _value(raw_call, "function", {})
        tool_name = _optional_text(_value(function, "name", None)) or ""
        tool_call_id = _optional_text(_value(raw_call, "id", None)) or ""
        arguments = _optional_text(_value(function, "arguments", None)) or ""
        block, start_event = self._ensure_block(
            request,
            state,
            BlockType.TOOL_CALL,
            provider_index,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        if start_event is not None:
            events.append(start_event)
        block.tool_name += tool_name
        block.tool_call_id += tool_call_id
        if arguments:
            block.content += arguments
            self._record_first_token(state, started)
            events.append(self._delta_event(request, block, arguments))
        return tuple(events)

    def _ensure_block(
        self,
        request: LLMRequest,
        state: _StreamState,
        block_type: BlockType,
        provider_index: int,
        *,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> tuple[_OpenBlock, LLMBlockStart | None]:
        """复用活动块；首次出现时分配独立公开块序号并产生一次开始事件。"""
        key = (block_type, provider_index)
        existing = state.open_blocks.get(key)
        if existing is not None:
            return existing, None
        block = _OpenBlock(
            index=state.next_index,
            block_id=str(uuid4()),
            block_type=block_type,
        )
        state.next_index += 1
        state.open_blocks[key] = block
        return block, LLMBlockStart(
            request_id=request.request_id,
            created_at=datetime.now(UTC),
            index=block.index,
            block_id=block.block_id,
            block_type=block_type,
            tool_name=tool_name or None,
            tool_call_id=tool_call_id or None,
        )

    def _close_other_text_blocks(
        self,
        request: LLMRequest,
        state: _StreamState,
        current: BlockType,
    ) -> tuple[LLMBlockEnd, ...]:
        keys = [
            key
            for key in state.open_blocks
            if key[0] in {BlockType.THINKING, BlockType.TEXT} and key[0] is not current
        ]
        return tuple(self._close_block(request, state, key) for key in keys)

    def _close_non_tool_blocks(
        self,
        request: LLMRequest,
        state: _StreamState,
    ) -> tuple[LLMBlockEnd, ...]:
        keys = [key for key in state.open_blocks if key[0] is not BlockType.TOOL_CALL]
        return tuple(self._close_block(request, state, key) for key in keys)

    def _finish_open_blocks(
        self,
        request: LLMRequest,
        state: _StreamState,
    ) -> tuple[LLMBlockEnd, ...]:
        keys = sorted(state.open_blocks, key=lambda key: state.open_blocks[key].index)
        return tuple(self._close_block(request, state, key) for key in keys)

    def _close_block(
        self,
        request: LLMRequest,
        state: _StreamState,
        key: tuple[BlockType, int],
    ) -> LLMBlockEnd:
        block = state.open_blocks.pop(key)
        final = _assistant_block(block)
        state.blocks.append(final)
        return LLMBlockEnd(
            request_id=request.request_id,
            created_at=datetime.now(UTC),
            index=block.index,
            block_id=block.block_id,
            block=final,
        )

    @staticmethod
    def _delta_event(
        request: LLMRequest,
        block: _OpenBlock,
        delta: str,
    ) -> LLMBlockDelta:
        return LLMBlockDelta(
            request_id=request.request_id,
            created_at=datetime.now(UTC),
            index=block.index,
            block_id=block.block_id,
            block_type=block.block_type,
            delta=delta,
        )

    @staticmethod
    def _record_first_token(
        state: _StreamState,
        started: float,
    ) -> None:
        if state.first_token_ms is None:
            state.first_token_ms = max(0.0, (monotonic() - started) * 1_000)

    @staticmethod
    def _record_chunk_metadata(raw: Any, state: _StreamState) -> None:
        state.response_id = _optional_text(_value(raw, "id", None)) or state.response_id
        state.model = _optional_text(_value(raw, "model", None)) or state.model
        raw_usage = _value(raw, "usage", None)
        if raw_usage is not None:
            state.usage = _usage(raw_usage)

    def _stream_response(
        self,
        state: _StreamState,
        request: LLMRequest,
        started_at: datetime,
        started: float,
    ) -> LLMResponse:
        if any(
            isinstance(block, ThinkingBlock) and block.thinking.strip()
            for block in state.blocks
        ) and not any(
            isinstance(block, ToolCallBlock)
            or (isinstance(block, TextBlock) and block.text.strip())
            for block in state.blocks
        ):
            _LOGGER.warning(
                "llm_thinking_only_response request_id=%s response_id=%s "
                "finish_reason=%s",
                request.request_id,
                state.response_id,
                state.finish_reason,
            )
        return LLMResponse(
            blocks=tuple(state.blocks),
            usage=state.usage or Usage(),
            timing=_timing(started_at, started, state.first_token_ms),
            model=state.model or request.config.model,
            finish_reason=_FINISH_REASONS.get(
                state.finish_reason,
                FinishReason.STOP,
            ),
            response_id=state.response_id,
        )

    def _error_response(
        self,
        error: Exception,
        request: LLMRequest,
        started_at: datetime,
        started: float,
    ) -> LLMResponse:
        code, retryable = _normalized_error(error, request)
        return LLMResponse(
            blocks=(),
            timing=_timing(started_at, started),
            model=request.config.model,
            finish_reason=FinishReason.ERROR,
            error=LLMError(
                code=code,
                message=_provider_error_message(error, _ERROR_MESSAGES[code]),
                retryable=retryable,
                provider=self.provider_name,
            ),
        )


_MEDIA_BLOCKS = (ImageBlock, AudioBlock, VideoBlock, DocumentBlock)

_ERROR_MESSAGES = {
    "timeout": "模型请求超时",
    "provider_error": "模型服务调用失败",
    "asset_not_found": "媒体资产不存在",
    "media_resolution_failed": "媒体解析失败",
    "invalid_provider_media_mapping": "无法构造模型媒体请求",
    "provider_rejected_media": "模型服务拒绝媒体输入",
    "media_limit_exceeded": "媒体超过模型服务限制",
}


def _provider_error_message(error: Exception, fallback: str) -> str:
    """优先返回上游 HTTP 正文，否则回退到异常文本或稳定文案。"""
    try:
        response = getattr(error, "response", None)
        response_text = getattr(response, "text", None)
    except Exception:
        response_text = None
    if isinstance(response_text, str) and response_text:
        return response_text
    exception_text = str(error)
    return exception_text or fallback


def _normalized_error(error: Exception, request: LLMRequest) -> tuple[str, bool]:
    """结合异常类别和请求是否含媒体推导稳定错误码及可重试标志。"""
    if isinstance(error, AdapterError):
        if error.code == "media_too_large":
            return "media_limit_exceeded", False
        if error.code == "asset_not_found":
            return "asset_not_found", False
        return error.code, error.retryable
    error_name = type(error).__name__.lower()
    is_timeout = isinstance(error, TimeoutError) or "timeout" in error_name
    if is_timeout:
        return "timeout", True
    raw = str(error).lower()
    if _request_has_media(request):
        if any(marker in raw for marker in ("too large", "size", "limit", "length")):
            return "media_limit_exceeded", False
        if any(
            marker in raw
            for marker in ("image", "audio", "video", "media", "pdf", "file")
        ):
            return "provider_rejected_media", False
    retryable = "ratelimit" in error_name or "connection" in error_name
    return "provider_error", retryable


def _request_has_media(request: LLMRequest) -> bool:
    return any(
        isinstance(block, _MEDIA_BLOCKS)
        for message in request.messages
        for block in message.content
    )


def _provider_media_url(source: UrlSource | Base64Source | AssetSource) -> str:
    if isinstance(source, UrlSource):
        return source.url
    if isinstance(source, Base64Source):
        return f"data:{source.media_type};base64,{source.data}"
    raise ValueError("Provider-ready 媒体不能包含 AssetSource")


def _audio_format(media_type: str) -> str:
    formats = {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/ogg": "ogg",
        "audio/mp4": "mp4",
        "audio/webm": "webm",
    }
    try:
        return formats[media_type.lower()]
    except KeyError as error:
        raise ValueError("Provider-ready AudioBlock MIME 不受支持") from error


def _parse_arguments(
    raw_arguments: str,
) -> tuple[dict[str, JsonValue] | None, str | None]:
    try:
        return _ARGUMENTS_ADAPTER.validate_python(json.loads(raw_arguments)), None
    except (json.JSONDecodeError, ValueError) as error:
        return None, str(error)


def _usage(raw_usage: Any) -> Usage:
    """兼容 SDK 统计字段变体，保留缺失可选计数并按总量加和判断统计精度。"""
    input_tokens = int(_value(raw_usage, "prompt_tokens", 0) or 0)
    output_tokens = int(_value(raw_usage, "completion_tokens", 0) or 0)
    total_tokens = int(_value(raw_usage, "total_tokens", 0) or 0)
    prompt_details = _value(raw_usage, "prompt_tokens_details", {}) or {}
    completion_details = _value(raw_usage, "completion_tokens_details", {}) or {}
    exact = total_tokens == input_tokens + output_tokens
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        accounting=UsageAccounting.EXACT if exact else UsageAccounting.PARTIAL,
        reasoning_tokens=_first_token_count(
            _value(completion_details, "reasoning_tokens", None),
            _value(raw_usage, "reasoning_tokens", None),
        ),
        cache_read_tokens=_first_token_count(
            _value(prompt_details, "cached_tokens", None),
            _value(raw_usage, "cache_read_input_tokens", None),
            _value(raw_usage, "cache_read_tokens", None),
        ),
        cache_write_tokens=_first_token_count(
            _value(prompt_details, "cache_write_tokens", None),
            _value(prompt_details, "cache_creation_tokens", None),
            _value(raw_usage, "cache_creation_input_tokens", None),
            _value(raw_usage, "cache_write_input_tokens", None),
            _value(raw_usage, "cache_write_tokens", None),
        ),
    )


def _optional_token_count(value: Any) -> int | None:
    """把提供方可选 Token 数量安全转换为非负整数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _first_token_count(*values: Any) -> int | None:
    """返回第一个合法数量，保留有意义的零值。"""
    for value in values:
        parsed = _optional_token_count(value)
        if parsed is not None:
            return parsed
    return None


def _timing(
    started_at: datetime,
    started: float,
    first_token_ms: float | None = None,
) -> Timing:
    ended_at = datetime.now(UTC)
    return Timing(
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=max(0.0, (monotonic() - started) * 1_000),
        first_token_ms=first_token_ms,
    )


def _value(source: Any, name: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _assistant_block(block: _OpenBlock) -> AssistantBlock:
    if block.block_type is BlockType.THINKING:
        return ThinkingBlock(thinking=block.content)
    if block.block_type is BlockType.TEXT:
        return TextBlock(text=block.content)
    arguments, parse_error = _parse_arguments(block.content)
    return ToolCallBlock(
        tool_call_id=block.tool_call_id,
        tool_name=block.tool_name,
        arguments=arguments,
        raw_arguments=block.content,
        parse_error=parse_error,
    )
