"""PDF 解析：docling 转 Markdown 后复用 Markdown 入库链路。

docling 依赖较重（torch 等），导入与转换器初始化成本高，因此 docling 仅在
首次解析时惰性导入，``DocumentConverter`` 做模块级缓存。docling 的类型与
异常一律不外泄：解析失败统一抛 ``ValueError``。
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from agentsystem.knowledge.ingestion.markdown import ingest_markdown
from agentsystem.knowledge.ingestion.models import IngestResult

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

_converter: DocumentConverter | None = None


def _get_converter() -> DocumentConverter:
    """惰性创建并缓存模块级 DocumentConverter，避免重复初始化开销。"""
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter

        _converter = DocumentConverter()
    return _converter


def parse_pdf_to_markdown(data: bytes, *, filename: str) -> str:
    """把 PDF 字节流转换为 Markdown 文本。

    Args:
        data: PDF 文件内容。
        filename: 原始文件名，供 docling 识别格式与报错定位。

    Returns:
        docling 导出的 Markdown 文本。

    Raises:
        ValueError: PDF 文件内容为空、docling 解析失败，或提取文本为
            空（扫描件/图片型 PDF）。
    """
    if not data:
        raise ValueError(f"PDF 文件内容为空：{filename}")
    from docling_core.types.io import DocumentStream

    stream = DocumentStream(name=filename, stream=io.BytesIO(data))
    try:
        result = _get_converter().convert(stream)
        markdown = result.document.export_to_markdown()
    except Exception as exc:
        raise ValueError(f"PDF 解析失败：{filename}（{exc}）") from exc
    if not markdown.strip():
        raise ValueError("无法从 PDF 提取文本内容，暂不支持扫描件")
    return markdown


def ingest_pdf(data: bytes, *, source_name: str) -> IngestResult:
    """解析 PDF 字节流并组装入库结果：parse → normalize → chunk。

    清洗、分块与统计组装完全复用 ``ingest_markdown`` 的链路。

    Args:
        data: PDF 文件内容。
        source_name: 来源名，作为 suggested_category；内部以
            ``<source_name>.pdf`` 作为传给 docling 的文件名。

    Returns:
        含 chunks、warnings 与 stats 的完整解析结果。

    Raises:
        ValueError: PDF 解析失败、提取文本为空，或清洗阶段拒绝内容。
    """
    markdown = parse_pdf_to_markdown(data, filename=f"{source_name}.pdf")
    return ingest_markdown(markdown, source_name=source_name)
