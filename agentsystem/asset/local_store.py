"""项目目录内的内容寻址 LocalAssetStore。"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import aiofiles

from agentsystem.asset.models import (
    LOCAL_ASSET_ID_PATTERN,
    AssetContent,
    AssetPutRequest,
    AssetRecord,
)
from agentsystem.common.errors import AdapterError


class LocalAssetStore:
    """在严格 project root 内原子保存 SHA-256 内容寻址文件。

    每次写入独立暂存、校验并替换目标文件；只持久化正文，不保存 MIME 和文件名。
    文件句柄按操作打开和关闭，实例不持有长期连接。
    """

    def __init__(self, *, project_root: Path, root: Path = Path("data/assets")) -> None:
        """解析本地根目录并拒绝绝对、逃逸和符号链接路径。

        Args:
            project_root: 已存在的项目根目录。
            root: 项目内资产目录的相对路径。

        Raises:
            ValueError: root 绝对化、逃逸或包含符号链接。
            OSError: 项目根目录无法解析。
        """
        if root.is_absolute() or ".." in root.parts:
            raise ValueError("AssetStore root 必须位于项目根目录内")
        resolved_project = project_root.resolve(strict=True)
        _reject_symlink_components(resolved_project, root)
        resolved_root = (resolved_project / root).resolve(strict=False)
        try:
            resolved_root.relative_to(resolved_project)
        except ValueError as error:
            raise ValueError("AssetStore root 必须位于项目根目录内") from error
        self._project_root = resolved_project
        self._relative_root = root
        self._root = resolved_root

    async def put(self, request: AssetPutRequest) -> AssetRecord:
        """流式计算大小和哈希，并以原子替换安装最终文件。

        Args:
            request: 单次正文流、展示元数据及可选完整性断言。

        Returns:
            内容寻址 ID 与实际大小、摘要；MIME 和文件名沿用请求值。

        Raises:
            AdapterError: 正文为空或大小、摘要与预期不符。
            TypeError: 正文流产生非 bytes 值。
            OSError: 文件写入、同步或替换失败。
        """
        await asyncio.to_thread(self._validate_root)
        temporary_directory = self._root / ".tmp"
        await asyncio.to_thread(temporary_directory.mkdir, parents=True, exist_ok=True)
        temporary_path = temporary_directory / f"{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            async with aiofiles.open(temporary_path, "wb") as output:
                async for chunk in request.body:
                    if not isinstance(chunk, bytes):
                        raise TypeError("AssetPutRequest.body 必须生成 bytes")
                    if not chunk:
                        continue
                    await output.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                await output.flush()
            if size_bytes <= 0:
                raise AdapterError(
                    operation="asset.put",
                    code="asset_integrity_failed",
                    message="资产正文不能为空",
                )
            await asyncio.to_thread(_fsync_file, temporary_path)
            checksum = digest.hexdigest()
            _verify_expected_metadata(request, size_bytes, checksum)
            destination = self._content_path(checksum)
            await asyncio.to_thread(
                destination.parent.mkdir,
                parents=True,
                exist_ok=True,
            )
            await asyncio.to_thread(os.replace, temporary_path, destination)
            await asyncio.to_thread(_fsync_directory, destination.parent)
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
        """验证 ID 后异步打开对应内容文件，退出上下文时关闭句柄。

        Args:
            asset_id: sha256- 前缀的内容寻址标识。

        Yields:
            通用二进制 MIME 和只能在上下文内消费的正文流。

        Raises:
            AdapterError: ID 格式无效或对应文件不存在。
            OSError: 文件无法打开或读取。
        """
        match = LOCAL_ASSET_ID_PATTERN.fullmatch(asset_id)
        if match is None:
            raise AdapterError(
                operation="asset.open",
                code="invalid_asset_id",
                message="资产 ID 格式无效",
            )
        await asyncio.to_thread(self._validate_root)
        path = self._content_path(match.group(1))
        if not await asyncio.to_thread(path.is_file):
            raise AdapterError(
                operation="asset.open",
                code="asset_not_found",
                message="资产不存在",
            )
        try:
            async with aiofiles.open(path, "rb") as stream:

                async def body() -> AsyncIterator[bytes]:
                    """在外层文件上下文存活期间逐块读取正文。"""
                    while chunk := await stream.read(64 * 1024):
                        yield chunk

                yield AssetContent(
                    media_type="application/octet-stream",
                    body=body(),
                )
        except FileNotFoundError as error:
            raise AdapterError(
                operation="asset.open",
                code="asset_not_found",
                message="资产不存在",
            ) from error

    def _content_path(self, checksum: str) -> Path:
        return self._root / "sha256" / checksum[:2] / checksum

    def _validate_root(self) -> None:
        _reject_symlink_components(self._project_root, self._relative_root)
        resolved = self._root.resolve(strict=False)
        try:
            resolved.relative_to(self._project_root)
        except ValueError as error:
            raise ValueError("AssetStore root 必须位于项目根目录内") from error


def _reject_symlink_components(project_root: Path, relative_root: Path) -> None:
    current = project_root
    for part in relative_root.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("AssetStore root 不能包含符号链接")


def _verify_expected_metadata(
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


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
