"""统一 LLM 调用入口与调用前临时媒体准备。"""

import base64
import binascii
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from agentsystem.asset.fetcher import SafeMediaFetcher
from agentsystem.asset.protocol import AssetStore
from agentsystem.common.errors import AdapterError, LLMError
from agentsystem.common.models import FinishReason
from agentsystem.llm.models import (
    LLMEnd,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    LLMStart,
    MediaTransport,
    Timing,
)
from agentsystem.llm.provider import LLMProvider
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    Base64Source,
    DocumentBlock,
    ImageBlock,
    MediaSource,
    Message,
    StoredBlock,
    UrlSource,
    VideoBlock,
)

_MEDIA_BLOCKS = (ImageBlock, AudioBlock, VideoBlock, DocumentBlock)
_MEDIA_ERROR_MESSAGES = {
    "asset_not_found": "媒体资产不存在",
    "invalid_provider_media_mapping": "无法构造模型媒体请求",
    "media_limit_exceeded": "媒体超过模型服务限制",
    "media_resolution_failed": "媒体解析失败",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMClientConfig:
    """LLMClient 的不可变依赖集合，不拥有注入资源的生命周期。

    Attributes:
        provider: 负责模型协议与错误映射的底层提供方。
        media_transport: 非音频 URL 的透传或 Base64 转换策略。
        asset_store: 解析 AssetSource 的存储。
        media_fetcher: 需要转换 URL 时使用的下载器。
        max_file_bytes: 单个媒体正文的最大字节数。
    """

    provider: LLMProvider
    media_transport: MediaTransport
    asset_store: AssetStore
    media_fetcher: SafeMediaFetcher
    max_file_bytes: int

    def __post_init__(self) -> None:
        """拒绝无法执行有界媒体准备的配置。"""
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须为正数")


class LLMClient:
    """在底层 Provider 调用前准备一次临时请求。

    从注入的资产存储或下载器解析媒体，仅构造发送副本，不修改权威消息历史。
    Provider 与媒体资源由外部管理；每次 generate 独立处理请求和错误响应。
    """

    def __init__(self, config: LLMClientConfig) -> None:
        """保存唯一不可变 Client 配置。

        Args:
            config: Provider、媒体依赖及单文件大小限制。
        """
        self._config = config

    @property
    def provider_name(self) -> str:
        """返回 Trace 与事件使用的稳定 Provider 名称。"""
        return self._config.provider.provider_name

    async def generate(self, request: LLMRequest) -> AsyncGenerator[LLMEvent]:
        """准备临时媒体请求并转发底层 Provider 事件。

        Args:
            request: 包含权威消息副本的模型请求。

        Yields:
            Provider 事件；预期媒体失败时产生 Start 与携带错误响应的 End。

        Raises:
            AdapterError: 媒体准备发生未预期错误；Provider 异常继续向外传播。
        """
        started_at = datetime.now(UTC)
        started = monotonic()
        try:
            prepared = await self._prepare_request(request)
        except AdapterError as error:
            yield LLMStart(
                request_id=request.request_id,
                created_at=started_at,
                model=request.config.model,
                output_mode=request.output_mode,
            )
            yield LLMEnd(
                request_id=request.request_id,
                created_at=datetime.now(UTC),
                response=_media_error_response(
                    error,
                    request=request,
                    provider_name=self.provider_name,
                    started_at=started_at,
                    started=started,
                ),
            )
            return
        except Exception as error:
            raise AdapterError(
                operation="llm.prepare",
                code="llm_client_failed",
                message="模型请求准备失败",
            ) from error
        async for event in self._config.provider.generate(prepared):
            yield event

    async def _prepare_request(self, request: LLMRequest) -> LLMRequest:
        messages = tuple(
            [await self._prepare_message(message) for message in request.messages]
        )
        pairs = zip(messages, request.messages, strict=True)
        if all(left is right for left, right in pairs):
            return request
        return request.model_copy(update={"messages": messages})

    async def _prepare_message(self, message: Message) -> Message:
        changed = False
        content: list[StoredBlock] = []
        for block in message.content:
            if not isinstance(block, _MEDIA_BLOCKS):
                content.append(block)
                continue
            source = await self._prepare_source(
                block.source,
                is_audio=isinstance(block, AudioBlock),
            )
            if source is block.source:
                content.append(block)
                continue
            changed = True
            content.append(block.model_copy(update={"source": source}))
        if not changed:
            return message
        return message.model_copy(update={"content": tuple(content)})

    async def _prepare_source(
        self,
        source: MediaSource,
        *,
        is_audio: bool,
    ) -> MediaSource:
        """保留可透传 URL，资产及音频解析为 Base64，避免变更历史来源记录。"""
        if isinstance(source, Base64Source):
            _validate_base64(source.data, self._config.max_file_bytes)
            return source
        if (
            isinstance(source, UrlSource)
            and self._config.media_transport is MediaTransport.AUTO
            and not is_audio
        ):
            return source
        body = await self._resolve_bytes(source)
        return Base64Source(
            data=base64.b64encode(body).decode("ascii"),
            media_type=source.media_type,
        )

    async def _resolve_bytes(self, source: UrlSource | AssetSource) -> bytes:
        if isinstance(source, UrlSource):
            body = self._config.media_fetcher.fetch(source.url)
        else:
            async with self._config.asset_store.open(source.asset_id) as content:
                return await _collect_bounded(
                    content.body,
                    self._config.max_file_bytes,
                )
        return await _collect_bounded(body, self._config.max_file_bytes)


def _validate_base64(data: str, max_file_bytes: int) -> None:
    if data.startswith("data:"):
        raise _media_error("invalid_provider_media_mapping")
    try:
        decoded = base64.b64decode(data.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise _media_error("invalid_provider_media_mapping") from error
    if not decoded:
        raise _media_error("invalid_provider_media_mapping")
    if len(decoded) > max_file_bytes:
        raise _media_error("media_limit_exceeded")


async def _collect_bounded(body: AsyncIterator[bytes], max_file_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size_bytes = 0
    async for chunk in body:
        size_bytes += len(chunk)
        if size_bytes > max_file_bytes:
            raise _media_error("media_limit_exceeded")
        chunks.append(chunk)
    if size_bytes <= 0:
        raise _media_error("media_resolution_failed")
    return b"".join(chunks)


def _media_error(code: str) -> AdapterError:
    return AdapterError(
        operation="llm.prepare",
        code=code,
        message=_MEDIA_ERROR_MESSAGES[code],
    )


def _media_error_response(
    error: AdapterError,
    *,
    request: LLMRequest,
    provider_name: str,
    started_at: datetime,
    started: float,
) -> LLMResponse:
    code = (
        "media_limit_exceeded"
        if error.code in {"media_too_large", "media_limit_exceeded"}
        else error.code
    )
    return LLMResponse(
        blocks=(),
        timing=Timing(
            started_at=started_at,
            ended_at=datetime.now(UTC),
            duration_ms=(monotonic() - started) * 1000,
        ),
        model=request.config.model,
        finish_reason=FinishReason.ERROR,
        error=LLMError(
            code=code,
            message=error.message,
            retryable=error.retryable,
            provider=provider_name,
        ),
    )
