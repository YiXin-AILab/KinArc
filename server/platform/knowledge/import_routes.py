"""平台知识库文档上传导入 API：解析预览与批量入库。

上传链路分两步：``POST .../docs/import-preview`` 接收 multipart 文件，
按扩展名调度 ``agentsystem.knowledge.ingestion.ingest_document`` 解析为
候选知识块（含建议字段、统计信息与内容去重标记），只返回预览不落盘；
前端确认编辑后把最终文档提交给 ``POST .../docs/import`` 批量入库。
解析是 CPU 级同步操作，不使用 Job 模式；同步解析与库文件读取经
``asyncio.to_thread()`` 隔离，避免阻塞 FastAPI 事件循环。
"""

import asyncio
import hashlib
from collections.abc import Sequence
from typing import Annotated

import puremagic
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import StrictModel
from agentsystem.knowledge.ingestion import (
    IngestChunk,
    IngestResult,
    IngestStats,
    ingest_document,
)
from agentsystem.knowledge.registry import KB_DOCUMENTS_FILENAME
from fastapi import APIRouter, File, UploadFile, status
from pydantic import Field

from server.platform.knowledge.routes import (
    PlatformKbDocument,
    _load_kb_docs,
    _save_kb_docs,
    _validate_id,
    existing_kb_dir,
)
from server.platform.store import PlatformStore

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 64 * 1024
_SNIFF_BYTES = 8192
_MARKDOWN_EXTENSIONS = (".md", ".markdown")
_SUPPORTED_EXTENSIONS = (*_MARKDOWN_EXTENSIONS, ".pdf")


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class PlatformKbImportPreviewChunk(StrictModel):
    """一个解析出的候选知识块；duplicate_of 标记与库内现有文档内容重复。"""

    index: int
    suggested_doc_id: str
    suggested_title: str
    suggested_category: str
    content: str
    section_path: str
    has_table: bool
    char_count: int
    duplicate_of: str | None


class PlatformKbImportStats(StrictModel):
    """一次解析的统计信息，用于预览页质量提示。"""

    chunk_count: int
    min_chars: int
    max_chars: int
    avg_chars: int
    broken_tables: int
    dropped_images: int
    cleaned_lines: int


class PlatformKbImportPreviewResponse(StrictModel):
    """导入预览响应：解析出的候选块、警告与统计信息，不落盘。"""

    knowledge_base: str
    filename: str
    media_type: str
    chunks: tuple[PlatformKbImportPreviewChunk, ...]
    warnings: tuple[str, ...]
    stats: PlatformKbImportStats


class PlatformKbImportRequest(StrictModel):
    """批量入库请求；docs 至少一条，结构同单条文档创建。"""

    docs: tuple[PlatformKbDocument, ...] = Field(min_length=1)


class PlatformKbImportResponse(StrictModel):
    """批量入库响应：成功导入的文档条数。"""

    knowledge_base: str
    imported: int


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def register_kb_import_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册知识库文档上传导入（预览与批量入库）路由。

    Args:
        router: 接收新路由的 FastAPI 路由器。
        store: 平台资源路径配置。
    """

    @router.post(
        "/knowledge-bases/{scope}/{name}/docs/import-preview",
        response_model=PlatformKbImportPreviewResponse,
    )
    async def import_preview(
        scope: str,
        name: str,
        file: Annotated[UploadFile, File()],
    ) -> PlatformKbImportPreviewResponse:
        """解析上传文件并返回候选知识块预览；不落盘、不改动 documents.json。"""
        try:
            kb_dir = existing_kb_dir(store, scope, name)
            filename = file.filename or "upload"
            media_type = _resolve_media_type(filename)
            data = await _read_upload(file)
            if media_type == "markdown":
                _reject_binary_markdown(data, filename)
            result = await asyncio.to_thread(_ingest, data, filename)
            hashes = _content_hashes(
                await asyncio.to_thread(_load_kb_docs, kb_dir / KB_DOCUMENTS_FILENAME)
            )
            return PlatformKbImportPreviewResponse(
                knowledge_base=name,
                filename=filename,
                media_type=media_type,
                chunks=tuple(_preview_chunk(chunk, hashes) for chunk in result.chunks),
                warnings=result.warnings,
                stats=_preview_stats(result.stats),
            )
        finally:
            await file.close()

    @router.post(
        "/knowledge-bases/{scope}/{name}/docs/import",
        response_model=PlatformKbImportResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def import_docs(
        scope: str,
        name: str,
        request: PlatformKbImportRequest,
    ) -> PlatformKbImportResponse:
        """批量追加文档；任何 doc_id 冲突（库内或请求内自重复）整批拒绝。"""
        kb_dir = existing_kb_dir(store, scope, name)
        path = kb_dir / KB_DOCUMENTS_FILENAME
        docs = list(_load_kb_docs(path))
        for doc in request.docs:
            _validate_id(doc.doc_id, label="文档 ID")
        conflicts = _find_conflicts(docs, request.docs)
        if conflicts:
            raise ServiceError(
                kind=ServiceErrorKind.CONFLICT,
                code="kb_doc_exists",
                message=f"文档已存在：{', '.join(conflicts)}",
            )
        docs.extend(request.docs)
        _save_kb_docs(path, tuple(docs))
        return PlatformKbImportResponse(knowledge_base=name, imported=len(request.docs))


# ---------------------------------------------------------------------------
# 校验与解析
# ---------------------------------------------------------------------------


def _resolve_media_type(filename: str) -> str:
    """按扩展名白名单确定 media_type（markdown/pdf）；不支持抛 INVALID。"""
    suffix = filename.rpartition(".")[2].lower()
    if f".{suffix}" in _MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix == "pdf":
        return "pdf"
    supported = "、".join(_SUPPORTED_EXTENSIONS)
    raise ServiceError(
        kind=ServiceErrorKind.INVALID,
        code="kb_unsupported_file_type",
        message=f"不支持的文件类型：{filename}，目前支持：{supported}",
    )


async def _read_upload(file: UploadFile) -> bytes:
    """分块读取上传内容并施加字节上限；空文件与超限均抛 INVALID。"""
    chunks: list[bytes] = []
    size_bytes = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        size_bytes += len(chunk)
        if size_bytes > _MAX_UPLOAD_BYTES:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_file_too_large",
                message="上传文件超过 10MB 上限",
            )
        chunks.append(chunk)
    if size_bytes == 0:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_file_empty",
            message="上传文件为空",
        )
    return b"".join(chunks)


def _reject_binary_markdown(data: bytes, filename: str) -> None:
    """嗅探前 8KB 否决伪装成 Markdown 的明显二进制（如 zip/exe）。"""
    try:
        detected = puremagic.from_string(data[:_SNIFF_BYTES], mime=True)
    except puremagic.PureError:
        return
    if not detected.lower().startswith("text/"):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_binary_content",
            message=f"文件内容是二进制（{detected}），不是 Markdown：{filename}",
        )


def _ingest(data: bytes, filename: str) -> IngestResult:
    """调度 ingestion 解析；解析失败统一收敛为 INVALID 并透传原因。"""
    try:
        return ingest_document(data, filename=filename)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_parse_failed",
            message=str(error),
        ) from error


def _content_hashes(docs: Sequence[PlatformKbDocument]) -> dict[str, str]:
    """建立库内现有文档的内容 sha256 → doc_id 映射，用于预览去重标记。"""
    return {
        hashlib.sha256(doc.content.encode("utf-8")).hexdigest(): doc.doc_id
        for doc in docs
    }


def _preview_stats(stats: IngestStats) -> PlatformKbImportStats:
    return PlatformKbImportStats(
        chunk_count=stats.chunk_count,
        min_chars=stats.min_chars,
        max_chars=stats.max_chars,
        avg_chars=stats.avg_chars,
        broken_tables=stats.broken_tables,
        dropped_images=stats.dropped_images,
        cleaned_lines=stats.cleaned_lines,
    )


def _preview_chunk(
    chunk: IngestChunk,
    hashes: dict[str, str],
) -> PlatformKbImportPreviewChunk:
    digest = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
    return PlatformKbImportPreviewChunk(
        index=chunk.index,
        suggested_doc_id=chunk.suggested_doc_id,
        suggested_title=chunk.suggested_title,
        suggested_category=chunk.suggested_category,
        content=chunk.content,
        section_path=chunk.section_path,
        has_table=chunk.has_table,
        char_count=chunk.char_count,
        duplicate_of=hashes.get(digest),
    )


def _find_conflicts(
    existing: Sequence[PlatformKbDocument],
    incoming: Sequence[PlatformKbDocument],
) -> list[str]:
    """收集全部冲突 doc_id：与库内现有重复，或请求体内自重复。"""
    seen = {doc.doc_id for doc in existing}
    conflicts: list[str] = []
    for doc in incoming:
        if doc.doc_id in seen and doc.doc_id not in conflicts:
            conflicts.append(doc.doc_id)
        seen.add(doc.doc_id)
    return conflicts


__all__ = [
    "PlatformKbImportPreviewResponse",
    "PlatformKbImportRequest",
    "PlatformKbImportResponse",
    "register_kb_import_routes",
]
