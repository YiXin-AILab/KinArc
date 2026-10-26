"""LLMClient 调用前媒体准备测试。"""

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from agentsystem.asset.fetcher import SafeMediaFetcher
from agentsystem.asset.models import AssetContent
from agentsystem.asset.protocol import AssetStore
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import FinishReason, OutputMode
from agentsystem.llm.client import LLMClient, LLMClientConfig
from agentsystem.llm.models import (
    LLMEnd,
    LLMEvent,
    LLMGenerationConfig,
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
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    UrlSource,
    UserMediaBlock,
    VideoBlock,
)

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


class RecordingProvider:
    """记录 Client 实际交给 Provider 的临时请求。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    @property
    def provider_name(self) -> str:
        """返回测试 Provider 名称。"""
        return "recording"

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """记录请求并产生最小合法事件流。"""
        self.requests.append(request)
        yield LLMStart(
            request_id=request.request_id,
            created_at=_NOW,
            model=request.config.model,
            output_mode=request.output_mode,
        )
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=LLMResponse(
                blocks=(TextBlock(text="ok"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model=request.config.model,
                finish_reason=FinishReason.STOP,
            ),
        )


class MemoryAssetStore:
    """返回固定媒体正文。"""

    def __init__(self, body: bytes = b"asset-bytes") -> None:
        self.body = body

    @asynccontextmanager
    async def open(self, asset_id: str) -> AsyncIterator[AssetContent]:
        """打开测试资产。"""
        if asset_id == "missing":
            raise AdapterError(
                operation="asset.open",
                code="asset_not_found",
                message="资产不存在",
            )
        yield AssetContent(
            media_type="application/octet-stream",
            body=_body(self.body),
        )


class RecordingFetcher:
    """记录 URL 并返回固定正文。"""

    def __init__(self, body: bytes = b"url-bytes") -> None:
        self.body = body
        self.urls: list[str] = []

    async def fetch(self, url: str) -> AsyncIterator[bytes]:
        """返回测试 URL 正文。"""
        self.urls.append(url)
        yield self.body


async def _body(value: bytes) -> AsyncIterator[bytes]:
    yield value


def _request(block: UserMediaBlock) -> LLMRequest:
    return LLMRequest(
        request_id="request-1",
        messages=(
            Message(
                message_id="message-1",
                session_id="session",
                role=Role.USER,
                content=(TextBlock(text="describe"), block),
                origin=MessageOrigin.USER,
                run_id="run",
                created_at=_NOW,
                visibility=MessageVisibility.ALL,
            ),
        ),
        tools=({"type": "function", "function": {"name": "lookup"}},),
        config=LLMGenerationConfig(model="openai/test"),
        output_mode=OutputMode.COMPLETE,
    )


def _block(kind: str, source: UrlSource | Base64Source | AssetSource) -> UserMediaBlock:
    if kind == "image":
        return ImageBlock(source=source, detail="high")
    if kind == "audio":
        return AudioBlock(source=source)
    if kind == "video":
        return VideoBlock(source=source)
    return DocumentBlock(source=source)


def _media_type(kind: str) -> str:
    return {
        "image": "image/png",
        "audio": "audio/wav",
        "video": "video/mp4",
        "document": "application/pdf",
    }[kind]


def _client(
    provider: RecordingProvider,
    *,
    transport: MediaTransport,
    store: MemoryAssetStore | None = None,
    fetcher: RecordingFetcher | None = None,
    max_file_bytes: int = 1024,
) -> LLMClient:
    return LLMClient(
        LLMClientConfig(
            provider=cast(LLMProvider, provider),
            media_transport=transport,
            asset_store=cast(AssetStore, store or MemoryAssetStore()),
            media_fetcher=cast(SafeMediaFetcher, fetcher or RecordingFetcher()),
            max_file_bytes=max_file_bytes,
        )
    )


async def _generate(client: LLMClient, request: LLMRequest) -> list[LLMEvent]:
    return [event async for event in client.generate(request)]


def test_client_config_rejects_non_positive_file_limit() -> None:
    """错误大小门禁不能进入请求路径。"""
    provider = RecordingProvider()
    with pytest.raises(ValueError, match="max_file_bytes"):
        _client(
            provider,
            transport=MediaTransport.AUTO,
            max_file_bytes=0,
        )


@pytest.mark.parametrize("kind", ["image", "video", "document"])
async def test_auto_preserves_non_audio_url_without_fetching(kind: str) -> None:
    """AUTO 的公开非音频 URL 必须保持小请求体。"""
    provider = RecordingProvider()
    fetcher = RecordingFetcher()
    source = UrlSource(url="https://example.test/media", media_type=_media_type(kind))
    logical = _request(_block(kind, source))

    events = await _generate(
        _client(
            provider,
            transport=MediaTransport.AUTO,
            fetcher=fetcher,
        ),
        logical,
    )

    prepared = cast(UserMediaBlock, provider.requests[0].messages[0].content[1])
    assert prepared.source == source
    assert fetcher.urls == []
    assert logical.messages[0].content[1] == _block(kind, source)
    assert isinstance(events[0], LLMStart)
    assert isinstance(events[-1], LLMEnd)


@pytest.mark.parametrize("kind", ["image", "audio", "video", "document"])
@pytest.mark.parametrize("transport", list(MediaTransport))
async def test_asset_is_resolved_to_base64_for_every_media_block(
    kind: str,
    transport: MediaTransport,
) -> None:
    """Provider 边界不得残留 Runtime Asset 句柄。"""
    provider = RecordingProvider()
    source = AssetSource(
        asset_id="sha256-test",
        media_type=_media_type(kind),
        filename="input.bin",
    )
    logical = _request(_block(kind, source))

    await _generate(_client(provider, transport=transport), logical)

    prepared = cast(UserMediaBlock, provider.requests[0].messages[0].content[1])
    assert prepared.source == Base64Source(
        data=base64.b64encode(b"asset-bytes").decode("ascii"),
        media_type=_media_type(kind),
    )
    assert cast(UserMediaBlock, logical.messages[0].content[1]).source == source
    assert provider.requests[0].request_id == logical.request_id
    assert provider.requests[0].config is logical.config
    assert provider.requests[0].tools == logical.tools


@pytest.mark.parametrize("kind", ["image", "audio", "video", "document"])
async def test_base64_transport_resolves_every_url(kind: str) -> None:
    """BASE64 策略必须在 Client 内安全读取 URL。"""
    provider = RecordingProvider()
    fetcher = RecordingFetcher()
    source = UrlSource(url="https://example.test/media", media_type=_media_type(kind))

    await _generate(
        _client(
            provider,
            transport=MediaTransport.BASE64,
            fetcher=fetcher,
        ),
        _request(_block(kind, source)),
    )

    prepared = cast(UserMediaBlock, provider.requests[0].messages[0].content[1])
    assert prepared.source == Base64Source(
        data=base64.b64encode(b"url-bytes").decode("ascii"),
        media_type=_media_type(kind),
    )
    assert fetcher.urls == ["https://example.test/media"]


async def test_auto_still_resolves_audio_url() -> None:
    """供应商 input_audio 不接受 URL，因此 AUTO 也必须解析音频。"""
    provider = RecordingProvider()
    fetcher = RecordingFetcher()
    source = UrlSource(url="https://example.test/audio", media_type="audio/wav")

    await _generate(
        _client(provider, transport=MediaTransport.AUTO, fetcher=fetcher),
        _request(AudioBlock(source=source)),
    )

    prepared = cast(AudioBlock, provider.requests[0].messages[0].content[1])
    assert isinstance(prepared.source, Base64Source)
    assert fetcher.urls == ["https://example.test/audio"]


async def test_valid_base64_is_preserved_without_mutating_request() -> None:
    """合法内联媒体只需验证，不应被重新编码。"""
    provider = RecordingProvider()
    source = Base64Source(
        data=base64.b64encode(b"inline").decode("ascii"),
        media_type="image/png",
    )
    logical = _request(ImageBlock(source=source))

    await _generate(
        _client(provider, transport=MediaTransport.AUTO),
        logical,
    )

    prepared = cast(ImageBlock, provider.requests[0].messages[0].content[1])
    assert prepared.source == source
    assert logical.messages[0].content[1] == ImageBlock(source=source)


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (
            Base64Source(data="not-base64", media_type="image/png"),
            "invalid_provider_media_mapping",
        ),
        (AssetSource(asset_id="missing", media_type="image/png"), "asset_not_found"),
    ],
)
async def test_expected_media_failure_produces_complete_error_stream(
    source: Base64Source | AssetSource,
    expected_code: str,
) -> None:
    """准备失败仍须满足 Runtime 对 Provider stream 的开始和结束约束。"""
    provider = RecordingProvider()

    events = await _generate(
        _client(provider, transport=MediaTransport.AUTO),
        _request(ImageBlock(source=source)),
    )

    assert [type(event) for event in events] == [LLMStart, LLMEnd]
    end = cast(LLMEnd, events[-1])
    assert end.response.error is not None
    assert end.response.error.code == expected_code
    assert provider.requests == []


async def test_oversized_resolved_media_is_normalized() -> None:
    """有界收集必须在调用 Provider 前拒绝超限正文。"""
    provider = RecordingProvider()
    source = AssetSource(asset_id="sha256-test", media_type="image/png")

    events = await _generate(
        _client(
            provider,
            transport=MediaTransport.AUTO,
            store=MemoryAssetStore(b"too-large"),
            max_file_bytes=4,
        ),
        _request(ImageBlock(source=source)),
    )

    end = cast(LLMEnd, events[-1])
    assert end.response.error is not None
    assert end.response.error.code == "media_limit_exceeded"


async def test_cancellation_during_media_fetch_propagates() -> None:
    """取消属于控制流，Client 不得转换为普通 LLM 错误。"""

    class CancelingFetcher(RecordingFetcher):
        async def fetch(self, url: str) -> AsyncIterator[bytes]:
            del url
            raise asyncio.CancelledError
            yield b"unreachable"

    provider = RecordingProvider()
    request = _request(
        ImageBlock(
            source=UrlSource(
                url="https://example.test/image",
                media_type="image/png",
            )
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await _generate(
            _client(
                provider,
                transport=MediaTransport.BASE64,
                fetcher=CancelingFetcher(),
            ),
            request,
        )
