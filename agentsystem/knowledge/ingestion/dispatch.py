"""按文件扩展名调度文档入库解析。"""

from agentsystem.knowledge.ingestion.markdown import ingest_markdown
from agentsystem.knowledge.ingestion.models import IngestResult
from agentsystem.knowledge.ingestion.pdf import ingest_pdf

_MARKDOWN_EXTENSIONS = (".md", ".markdown")
_PDF_EXTENSIONS = (".pdf",)
_SUPPORTED_EXTENSIONS = _MARKDOWN_EXTENSIONS + _PDF_EXTENSIONS


def ingest_document(data: bytes, *, filename: str) -> IngestResult:
    """按扩展名选择解析链路，生成候选知识块，不写入知识库。

    Markdown 直接解析，PDF 先转为 Markdown；source_name 取去掉扩展名的文件名。

    Args:
        data: 文件内容字节流。
        filename: 原始文件名（含扩展名）。

    Returns:
        含 chunks、warnings 与 stats 的完整解析结果。

    Raises:
        ValueError: 扩展名不受支持、Markdown 不是 UTF-8 编码，或解析
            链路自身失败。
    """
    stem, dot, extension = filename.rpartition(".")
    suffix = f"{dot}{extension}".lower()
    source_name = stem or filename
    if suffix in _MARKDOWN_EXTENSIONS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Markdown 文件必须使用 UTF-8 编码：{filename}") from exc
        return ingest_markdown(text, source_name=source_name)
    if suffix in _PDF_EXTENSIONS:
        return ingest_pdf(data, source_name=source_name)
    supported = "、".join(_SUPPORTED_EXTENSIONS)
    raise ValueError(f"不支持的文件类型：{filename}，目前支持：{supported}")
