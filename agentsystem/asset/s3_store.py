"""S3/OSS compatible AssetStore adapter。"""

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiofiles

from agentsystem.asset.models import (
    LOCAL_ASSET_ID_PATTERN,
    AssetContent,
    AssetPutRequest,
    AssetRecord,
)
from agentsystem.common.errors import AdapterError


class S3AssetStore:
    """使用内容哈希对象键实现去重的 S3-compatible Store。

    借用 Application 管理的 client。写入先暂存到本地文件再选择单次或分片上传，
    最终清理暂存文件；读取句柄由 open 上下文关闭。并发去重不提供互斥保证。
    """

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        multipart_part_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        """接收由 Application 管理生命周期的 aiobotocore client。

        Args:
            client: 已配置网络超时的异步 S3 client。
            bucket: 已存在的目标 bucket 名称。
            multipart_part_bytes: 分片大小，也是单次上传与分片上传的分界。

        Raises:
            ValueError: bucket 为空或分片大小不是正数。
        """
        if not bucket:
            raise ValueError("S3 bucket 不能为空")
        if multipart_part_bytes <= 0:
            raise ValueError("multipart_part_bytes 必须为正数")
        self._client = client
        self._bucket = bucket
        self._multipart_part_bytes = multipart_part_bytes

    async def put(self, request: AssetPutRequest) -> AssetRecord:
        """先流式暂存并计算哈希，再去重上传对应对象键。

        Args:
            request: 正文流、MIME 和可选完整性断言。

        Returns:
            内容寻址 ID 与实际大小、摘要。

        Raises:
            AdapterError: 完整性校验失败或暂存、对象存储操作失败。
        """
        temporary_path = _temporary_path()
        try:
            size_bytes, checksum = await _stage_request(request, temporary_path)
            _verify_request(request, size_bytes, checksum)
            key = _object_key(checksum)
            if not await self._exists(key):
                if size_bytes <= self._multipart_part_bytes:
                    body = await _read_file(temporary_path)
                    await self._client.put_object(
                        Bucket=self._bucket,
                        Key=key,
                        Body=body,
                        ContentType=request.media_type,
                    )
                else:
                    await self._multipart_upload(
                        temporary_path,
                        key,
                        request.media_type,
                    )
        except AdapterError:
            raise
        except Exception as error:
            raise AdapterError(
                operation="asset.put",
                code="asset_store_unavailable",
                message="S3 资产写入失败",
                retryable=True,
            ) from error
        finally:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
        return AssetRecord(
            asset_id=f"sha256-{checksum}",
            media_type=request.media_type,
            filename=request.filename,
            size_bytes=size_bytes,
            checksum_sha256=checksum,
        )

    @asynccontextmanager
    async def open(self, asset_id: str) -> AsyncIterator[AssetContent]:
        """打开 S3 StreamingBody，并在退出上下文时关闭。

        Args:
            asset_id: sha256- 前缀的内容寻址标识。

        Yields:
            对象 ContentType 与上下文内可消费的正文流。

        Raises:
            AdapterError: ID 无效、对象不存在或 get_object 请求失败。
        """
        match = LOCAL_ASSET_ID_PATTERN.fullmatch(asset_id)
        if match is None:
            raise AdapterError(
                operation="asset.open",
                code="invalid_asset_id",
                message="资产 ID 格式无效",
            )
        try:
            response = await self._client.get_object(
                Bucket=self._bucket,
                Key=_object_key(match.group(1)),
            )
            body = response["Body"]
        except Exception as error:
            if _is_missing(error):
                raise AdapterError(
                    operation="asset.open",
                    code="asset_not_found",
                    message="资产不存在",
                ) from error
            raise AdapterError(
                operation="asset.open",
                code="asset_store_unavailable",
                message="S3 资产读取失败",
                retryable=True,
            ) from error
        async with body as stream:

            async def content_body() -> AsyncIterator[bytes]:
                """在对象响应上下文存活期间逐块读取正文。"""
                while chunk := await stream.read(64 * 1024):
                    yield chunk

            media_type = response.get("ContentType")
            yield AssetContent(
                media_type=(
                    media_type
                    if isinstance(media_type, str)
                    else "application/octet-stream"
                ),
                body=content_body(),
            )

    async def _exists(self, key: str) -> bool:
        try:
            await self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as error:
            if _is_missing(error):
                return False
            raise AdapterError(
                operation="asset.exists",
                code="asset_store_unavailable",
                message="S3 资产查询失败",
                retryable=True,
            ) from error
        return True

    async def _multipart_upload(
        self,
        path: Path,
        key: str,
        media_type: str,
    ) -> None:
        """顺序上传分片；普通异常时尝试中止上传，清理由 S3 协议完成。"""
        created = await self._client.create_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            ContentType=media_type,
        )
        upload_id = created.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise AdapterError(
                operation="asset.put",
                code="asset_store_unavailable",
                message="S3 multipart 响应无效",
                retryable=True,
            )
        parts: list[dict[str, object]] = []
        try:
            async with aiofiles.open(path, "rb") as stream:
                part_number = 1
                while body := await stream.read(self._multipart_part_bytes):
                    response = await self._client.upload_part(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=body,
                    )
                    etag = response.get("ETag")
                    if not isinstance(etag, str) or not etag:
                        raise AdapterError(
                            operation="asset.put",
                            code="asset_store_unavailable",
                            message="S3 part 响应无效",
                            retryable=True,
                        )
                    parts.append({"PartNumber": part_number, "ETag": etag})
                    part_number += 1
            await self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
            )
            raise


def _temporary_path() -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix="agent-asset-", suffix=".tmp")
    os.close(descriptor)
    return Path(raw_path)


async def _stage_request(request: AssetPutRequest, path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    async with aiofiles.open(path, "wb") as output:
        async for chunk in request.body:
            if not isinstance(chunk, bytes):
                raise TypeError("AssetPutRequest.body 必须生成 bytes")
            if not chunk:
                continue
            await output.write(chunk)
            digest.update(chunk)
            size_bytes += len(chunk)
    if size_bytes <= 0:
        raise AdapterError(
            operation="asset.put",
            code="asset_integrity_failed",
            message="资产正文不能为空",
        )
    return size_bytes, digest.hexdigest()


def _verify_request(
    request: AssetPutRequest,
    size_bytes: int,
    checksum: str,
) -> None:
    if request.expected_size is not None and request.expected_size != size_bytes:
        raise AdapterError(
            operation="asset.put",
            code="asset_integrity_failed",
            message="资产大小校验失败",
        )
    if request.expected_sha256 is not None and request.expected_sha256 != checksum:
        raise AdapterError(
            operation="asset.put",
            code="asset_integrity_failed",
            message="资产哈希校验失败",
        )


async def _read_file(path: Path) -> bytes:
    async with aiofiles.open(path, "rb") as stream:
        return await stream.read()


def _object_key(checksum: str) -> str:
    return f"sha256/{checksum[:2]}/{checksum}"


def _is_missing(error: Exception) -> bool:
    if isinstance(error, KeyError):
        return True
    raw_response = getattr(error, "response", None)
    if not isinstance(raw_response, dict):
        return False
    response = raw_response.get("Error", {})
    code = response.get("Code") if isinstance(response, dict) else None
    return code in {"404", "NoSuchKey", "NotFound"}
