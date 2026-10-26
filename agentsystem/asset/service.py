"""媒体校验、归档匹配和 AssetSource 替换编排。"""

import asyncio
import base64
import binascii
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager

import puremagic

from agentsystem.asset.fetcher import SafeMediaFetcher, validate_media_url
from agentsystem.asset.models import (
    AssetContent,
    AssetPutRequest,
    AssetRecord,
)
from agentsystem.asset.protocol import AssetStore
from agentsystem.common.errors import AdapterError
from agentsystem.config.models import MediaArchiveRuleConfig
from agentsystem.message.models import (
    DOCUMENT_MEDIA_TYPES,
    AssetSource,
    TextBlock,
    UrlSource,
    UserContentBlock,
    UserMediaBlock,
)

_MIME_PREFIX_BYTES = 8192


class AssetService:
    """在 Inbox 写入前验证媒体并按 Agent 规则选择性归档。

    借用全局 Store 和 Fetcher，负责大小、MIME 与归档规则编排，不管理其连接。
    已有 AssetSource 直接保留；未归档 URL 只校验地址结构，不下载检测正文。
    """

    def __init__(
        self,
        *,
        store: AssetStore,
        fetcher: SafeMediaFetcher,
        max_file_bytes: int,
        upload_timeout_seconds: float,
        allowed_url_schemes: tuple[str, ...] = ("http", "https"),
    ) -> None:
        """保存 Runtime 全局共享依赖和有界限制。

        Args:
            store: 上传和归档使用的资产存储。
            fetcher: 归档 URL 媒体时使用的安全获取器。
            max_file_bytes: 正文大小上限。
            upload_timeout_seconds: 调用 Store.put 的超时秒数。
            allowed_url_schemes: prepare 允许的 URL scheme。

        Raises:
            ValueError: 大小或上传超时不是正数。
        """
        if max_file_bytes <= 0 or upload_timeout_seconds <= 0:
            raise ValueError("AssetService 大小和 timeout 必须为正数")
        self._store = store
        self._fetcher = fetcher
        self._max_file_bytes = max_file_bytes
        self._upload_timeout_seconds = upload_timeout_seconds
        self._allowed_url_schemes = frozenset(allowed_url_schemes)

    async def upload(
        self,
        body: AsyncIterable[bytes],
        *,
        declared_media_type: str | None,
        filename: str | None,
    ) -> AssetRecord:
        """检测并有界流式保存一个 HTTP 上传资产。

        Args:
            body: 单次消费的上传字节流。
            declared_media_type: HTTP 声明 MIME；缺失或通用二进制值允许检测确定。
            filename: 可选展示文件名。

        Returns:
            保存后的资产记录。

        Raises:
            AdapterError: 正文为空、超限、MIME 不符、不受支持或存储失败。
        """
        prefix, replay = await _peek_upload(
            body,
            prefix_bytes=_MIME_PREFIX_BYTES,
            max_file_bytes=self._max_file_bytes,
        )
        detected_media_type = _detect_media_type(
            prefix,
            declared_media_type or "application/octet-stream",
        )
        _validate_supported_media_type(detected_media_type)
        normalized_declared = (declared_media_type or "").lower()
        if normalized_declared not in {"", "application/octet-stream"} and (
            normalized_declared != detected_media_type
        ):
            raise _asset_error("media_type_mismatch", "媒体真实 MIME 与声明不一致")
        request = AssetPutRequest(
            body=replay,
            media_type=detected_media_type,
            filename=filename,
        )
        try:
            async with asyncio.timeout(self._upload_timeout_seconds):
                return await self._store.put(request)
        except AdapterError:
            raise
        except Exception as error:
            raise _asset_error(
                "asset_upload_failed",
                "资产上传失败",
                retryable=True,
            ) from error

    @asynccontextmanager
    async def open(self, asset_id: str) -> AsyncIterator[AssetContent]:
        """打开资产并在 Store reader 生命周期内提供有大小限制的正文流。

        Args:
            asset_id: 当前存储的资产标识。

        Yields:
            已检测 MIME 与包含检测前缀的完整回放流，须在上下文内消费。

        Raises:
            AdapterError: 读取、大小或媒体类型校验失败。
        """
        try:
            async with self._store.open(asset_id) as content:
                prefix, replay = await _peek_upload(
                    content.body,
                    prefix_bytes=_MIME_PREFIX_BYTES,
                    max_file_bytes=self._max_file_bytes,
                )
                media_type = _detect_media_type(prefix, content.media_type)
                _validate_supported_media_type(media_type)
                yield AssetContent(
                    media_type=media_type,
                    body=replay,
                )
        except AdapterError:
            raise
        except Exception as error:
            raise _asset_error(
                "asset_store_unavailable",
                "资产存储暂时不可用",
                retryable=True,
            ) from error

    async def prepare(
        self,
        content: tuple[UserContentBlock, ...],
        rules: tuple[MediaArchiveRuleConfig, ...],
    ) -> tuple[UserContentBlock, ...]:
        """保持 Block 顺序，验证外部媒体并替换匹配归档规则的 Source。

        Args:
            content: 输入文本和媒体块；已有资产引用直接保留。
            rules: 以块类型及来源类型匹配的归档规则。

        Returns:
            顺序不变的内容元组；需归档的媒体来源替换为 AssetSource。

        Raises:
            AdapterError: URL、Base64、MIME 校验或归档失败。
        """
        prepared: list[UserContentBlock] = []
        for block in content:
            if isinstance(block, TextBlock):
                prepared.append(block)
                continue
            prepared.append(await self._prepare_media(block, rules))
        return tuple(prepared)

    async def _prepare_media(
        self,
        block: UserMediaBlock,
        rules: tuple[MediaArchiveRuleConfig, ...],
    ) -> UserMediaBlock:
        source = block.source
        if isinstance(source, AssetSource):
            return block
        should_archive = _matches_archive_rule(block, rules)
        if isinstance(source, UrlSource):
            validate_media_url(source.url, self._allowed_url_schemes)
            if not should_archive:
                return block
            body = await _collect_bounded(
                self._fetcher.fetch(source.url),
                self._max_file_bytes,
            )
        else:
            body = _decode_base64_bounded(source.data, self._max_file_bytes)
        detected_media_type = _detect_media_type(body, source.media_type)
        if detected_media_type != source.media_type.lower():
            raise _asset_error("media_type_mismatch", "媒体真实 MIME 与声明不一致")
        if not should_archive:
            return block
        request = AssetPutRequest(
            body=_single_chunk(body),
            media_type=detected_media_type,
            filename=None,
            expected_size=len(body),
        )
        try:
            async with asyncio.timeout(self._upload_timeout_seconds):
                record = await self._store.put(request)
        except AdapterError:
            raise
        except Exception as error:
            raise _asset_error(
                "asset_upload_failed",
                "资产上传失败",
                retryable=True,
            ) from error
        archived = AssetSource(
            asset_id=record.asset_id,
            media_type=record.media_type,
            filename=record.filename,
        )
        return block.model_copy(update={"source": archived})


def _matches_archive_rule(
    block: UserMediaBlock,
    rules: tuple[MediaArchiveRuleConfig, ...],
) -> bool:
    source_type = block.source.type
    return any(
        block.type in rule.block_types and source_type in rule.source_types
        for rule in rules
    )


def _decode_base64_bounded(data: str, max_file_bytes: int) -> bytes:
    if data.startswith("data:"):
        raise _asset_error("invalid_media_source", "Base64Source 不能使用 Data URI")
    try:
        encoded = data.encode("ascii")
    except UnicodeEncodeError as error:
        raise _asset_error("invalid_media_source", "Base64Source 不是 ASCII") from error
    padding = len(encoded) - len(encoded.rstrip(b"="))
    estimated_size = len(encoded) * 3 // 4 - padding
    if estimated_size > max_file_bytes:
        raise _asset_error("media_too_large", "媒体超过单文件上限")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _asset_error("invalid_media_source", "Base64Source 编码无效") from error
    if not decoded:
        raise _asset_error("invalid_media_source", "Base64Source 正文为空")
    if len(decoded) > max_file_bytes:
        raise _asset_error("media_too_large", "媒体超过单文件上限")
    return decoded


def _detect_media_type(body: bytes, declared_media_type: str) -> str:
    try:
        detected = puremagic.from_string(body[:8192], mime=True)
    except puremagic.PureError as error:
        if declared_media_type.lower().startswith("text/"):
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                return declared_media_type.lower()
        raise _asset_error(
            "media_type_mismatch", "媒体真实 MIME 与声明不一致"
        ) from error
    return detected.lower()


async def _collect_bounded(
    body: AsyncIterable[bytes],
    max_file_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    size_bytes = 0
    async for chunk in body:
        size_bytes += len(chunk)
        if size_bytes > max_file_bytes:
            raise _asset_error("media_too_large", "媒体超过单文件上限")
        chunks.append(chunk)
    if size_bytes == 0:
        raise _asset_error("media_validation_failed", "媒体正文为空")
    return b"".join(chunks)


async def _single_chunk(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _peek_upload(
    body: AsyncIterable[bytes],
    *,
    prefix_bytes: int,
    max_file_bytes: int,
) -> tuple[bytes, AsyncIterator[bytes]]:
    """只缓存检测前缀，并返回不丢字节的有界单次回放流。"""
    iterator = body.__aiter__()
    buffered = bytearray()
    pending = b""
    consumed_size = 0
    while len(buffered) < prefix_bytes:
        try:
            chunk = await anext(iterator)
        except StopAsyncIteration:
            break
        if not chunk:
            continue
        consumed_size += len(chunk)
        if consumed_size > max_file_bytes:
            raise _asset_error("media_too_large", "媒体超过单文件上限")
        needed = prefix_bytes - len(buffered)
        buffered.extend(chunk[:needed])
        pending = chunk[needed:]
        if pending:
            break
    if not buffered:
        raise _asset_error("media_validation_failed", "媒体正文为空")
    prefix = bytes(buffered)
    return prefix, _replay_upload(
        prefix,
        pending,
        iterator,
        consumed_size=consumed_size,
        max_file_bytes=max_file_bytes,
    )


async def _replay_upload(
    prefix: bytes,
    pending: bytes,
    iterator: AsyncIterator[bytes],
    *,
    consumed_size: int,
    max_file_bytes: int,
) -> AsyncIterator[bytes]:
    """回放检测所消费的字节并继续执行总大小门禁。"""
    yield prefix
    if pending:
        yield pending
    size_bytes = consumed_size
    async for chunk in iterator:
        if not chunk:
            continue
        size_bytes += len(chunk)
        if size_bytes > max_file_bytes:
            raise _asset_error("media_too_large", "媒体超过单文件上限")
        yield chunk


def _validate_supported_media_type(media_type: str) -> None:
    """只允许现有四类用户媒体 Block 能表达的 MIME。"""
    if media_type in DOCUMENT_MEDIA_TYPES or media_type.startswith(
        ("image/", "audio/", "video/")
    ):
        return
    raise _asset_error("unsupported_media_type", "媒体 MIME 不受支持")


def _asset_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> AdapterError:
    """构造 AssetService 内部统一的安全错误。"""
    return AdapterError(
        operation="asset.prepare",
        code=code,
        message=message,
        retryable=retryable,
    )
