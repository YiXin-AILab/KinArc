"""公司 Hydra 附件平台 AssetStore adapter。"""

import base64
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePath
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from agentsystem.asset.models import (
    AssetContent,
    AssetPutRequest,
    AssetRecord,
)
from agentsystem.common.errors import AdapterError


class HydraAssetStore:
    """封装 Hydra Token、Base64 JSON 上传和前缀读取协议。

    借用 Application 的 HTTP client，不负责关闭它。上传会在大小上限内完整缓存
    正文并获取一次短期 token；读取响应的生命周期限定在 open 上下文内。
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_url: str,
        upload_url: str,
        file_prefix: str,
        business_type: str,
        business_id: str,
        template_code: str,
        item_code: str,
        creator_id: str,
        max_file_bytes: int,
        timeout_seconds: float,
    ) -> None:
        """接收 Application 管理的共享 HTTP client 与业务字段。

        Args:
            client: 生命周期由调用方管理的异步 HTTP client。
            token_url: 获取上传 token 的地址。
            upload_url: 提交 Base64 JSON 的地址。
            file_prefix: 读取资产时追加 fileId 的 URL 前缀。
            business_type: token 请求的业务类型。
            business_id: 业务标识前缀，每次获取 token 时追加随机后缀。
            template_code: token 请求的模板编码。
            item_code: 上传请求的附件条目编码。
            creator_id: token 请求的创建者标识。
            max_file_bytes: 单文件正文大小上限。
            timeout_seconds: 每次 HTTP 请求的超时秒数。

        Raises:
            ValueError: 大小或超时限制不是正数。
        """
        if max_file_bytes <= 0 or timeout_seconds <= 0:
            raise ValueError("Hydra 大小和 timeout 必须为正数")
        self._client = client
        self._token_url = token_url
        self._upload_url = upload_url
        self._file_prefix = file_prefix.rstrip("/") + "/"
        self._business_type = business_type
        self._business_id = business_id
        self._template_code = template_code
        self._item_code = item_code
        self._creator_id = creator_id
        self._max_file_bytes = max_file_bytes
        self._timeout_seconds = timeout_seconds

    async def put(self, request: AssetPutRequest) -> AssetRecord:
        """获取短期 Token 后按平台协议上传 Base64 JSON。

        Args:
            request: 正文及展示元数据；本实现不比对预期大小或摘要字段。

        Returns:
            平台 fileId 与本地计算的正文大小、摘要。

        Raises:
            AdapterError: 正文为空、超限，或平台请求及响应无效。
            TypeError: 正文流产生非 bytes 值。
        """
        body = await _collect_bounded(request.body, self._max_file_bytes)
        checksum = hashlib.sha256(body).hexdigest()
        try:
            token = await self._get_token()
            response = await self._client.post(
                self._upload_url,
                params={"token": token},
                json={
                    "itemCode": self._item_code,
                    "sourceFileName": _safe_filename(request.filename),
                    "fileSource": base64.b64encode(body).decode("ascii"),
                },
                timeout=self._timeout_seconds,
            )
            payload = _success_payload(response)
            asset_id = payload.get("fileId")
            if not isinstance(asset_id, str) or not asset_id:
                raise _asset_error(
                    "asset.put", "asset_upload_failed", "Hydra 上传响应无效"
                )
            record = AssetRecord(
                asset_id=asset_id,
                media_type=request.media_type,
                filename=request.filename,
                size_bytes=len(body),
                checksum_sha256=checksum,
            )
        except AdapterError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise _asset_error(
                "asset.put",
                "asset_upload_failed",
                "Hydra 上传失败",
                retryable=True,
            ) from error
        return record

    @asynccontextmanager
    async def open(self, asset_id: str) -> AsyncIterator[AssetContent]:
        """对 opaque asset id 转义后流式读取平台文件。

        Args:
            asset_id: 平台 fileId，允许安全的多段路径。

        Yields:
            响应 MIME 和带大小限制的正文流；退出上下文会关闭 HTTP 响应。

        Raises:
            AdapterError: 标识无效、文件缺失、网络失败或读取超限。
        """
        url = _hydra_asset_url(self._file_prefix, asset_id)
        try:
            async with self._client.stream(
                "GET",
                url,
                timeout=self._timeout_seconds,
            ) as response:
                if response.status_code == 404:
                    raise _asset_error("asset.open", "asset_not_found", "资产不存在")
                response.raise_for_status()
                media_type = response.headers.get(
                    "Content-Type", "application/octet-stream"
                ).partition(";")[0]
                yield AssetContent(
                    media_type=media_type,
                    body=_httpx_body(response, self._max_file_bytes),
                )
        except AdapterError:
            raise
        except httpx.HTTPError as error:
            raise _asset_error(
                "asset.open",
                "asset_store_unavailable",
                "Hydra 资产读取失败",
                retryable=True,
            ) from error

    async def _get_token(self) -> str:
        response = await self._client.post(
            self._token_url,
            json={
                "busiType": self._business_type,
                "busiId": f"{self._business_id}-{uuid4().hex}",
                "templateCode": self._template_code,
                "creatorId": self._creator_id,
            },
            timeout=self._timeout_seconds,
        )
        payload = _success_payload(response)
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise _asset_error(
                "asset.put",
                "asset_upload_failed",
                "Hydra Token 响应无效",
            )
        return token


async def _httpx_body(
    response: httpx.Response,
    max_file_bytes: int,
) -> AsyncIterator[bytes]:
    """流式输出 HTTPX 正文并执行单文件上限。"""
    size_bytes = 0
    async for chunk in response.aiter_bytes():
        size_bytes += len(chunk)
        if size_bytes > max_file_bytes:
            raise _asset_error("asset.open", "media_too_large", "媒体超过单文件上限")
        if chunk:
            yield chunk


async def _collect_bounded(
    body: AsyncIterable[bytes],
    max_file_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    size_bytes = 0
    async for chunk in body:
        if not isinstance(chunk, bytes):
            raise TypeError("AssetPutRequest.body 必须生成 bytes")
        size_bytes += len(chunk)
        if size_bytes > max_file_bytes:
            raise _asset_error("asset.put", "media_too_large", "媒体超过单文件上限")
        chunks.append(chunk)
    if size_bytes <= 0:
        raise _asset_error("asset.put", "asset_upload_failed", "资产正文不能为空")
    return b"".join(chunks)


def _success_payload(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    raw = response.json()
    if not isinstance(raw, dict) or raw.get("code") != 0:
        raise _asset_error("asset.put", "asset_upload_failed", "Hydra 业务响应失败")
    payload = raw.get("data")
    if not isinstance(payload, dict):
        raise _asset_error("asset.put", "asset_upload_failed", "Hydra 响应结构无效")
    return payload


def _safe_filename(filename: str | None) -> str:
    value = PurePath(filename or "asset.bin").name
    cleaned = "".join(
        character
        for character in value
        if ord(character) >= 32 and character not in {"/", "\\"}
    )
    return cleaned[:255] or "asset.bin"


def _hydra_asset_url(file_prefix: str, asset_id: str) -> str:
    """保留平台 fileId 的路径层级，并独立转义每个安全路径段。"""
    if not asset_id or any(
        ord(character) < 32 or ord(character) == 127 for character in asset_id
    ):
        raise _asset_error("asset.open", "invalid_asset_id", "资产 ID 格式无效")
    segments = asset_id.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise _asset_error("asset.open", "invalid_asset_id", "资产 ID 格式无效")
    return file_prefix + "/".join(quote(segment, safe="") for segment in segments)


def _asset_error(
    operation: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> AdapterError:
    """构造 Hydra adapter 的安全错误。"""
    return AdapterError(
        operation=operation,
        code=code,
        message=message,
        retryable=retryable,
    )
