"""知识库 PDF 解析与格式调度测试（全部 mock docling，不做真实转换）。"""

from __future__ import annotations

import pytest
from agentsystem.knowledge.ingestion import (
    dispatch as dispatch_module,
)
from agentsystem.knowledge.ingestion import (
    ingest_document,
    ingest_pdf,
    parse_pdf_to_markdown,
)
from agentsystem.knowledge.ingestion import (
    pdf as pdf_module,
)

_SAMPLE_MARKDOWN = "# 保修政策\n\n" + "整车质保三年或十万公里，以先到者为准。" * 6


class _FakeDocument:
    """模拟 docling 文档对象，只暴露 export_to_markdown。"""

    def __init__(self, markdown: str) -> None:
        self._markdown = markdown

    def export_to_markdown(self) -> str:
        return self._markdown


class _FakeConversionResult:
    """模拟 docling ConversionResult。"""

    def __init__(self, markdown: str) -> None:
        self.document = _FakeDocument(markdown)


class _FakeConverter:
    """模拟 DocumentConverter：返回固定 Markdown 或抛出指定异常。"""

    def __init__(self, markdown: str = "", error: Exception | None = None) -> None:
        self._markdown = markdown
        self._error = error

    def convert(self, stream: object) -> _FakeConversionResult:
        if self._error is not None:
            raise self._error
        return _FakeConversionResult(self._markdown)


@pytest.fixture
def fake_converter(monkeypatch: pytest.MonkeyPatch) -> list[_FakeConverter]:
    """把 pdf 模块的转换器工厂替换为 Fake，返回可追加配置的实例列表。"""
    created: list[_FakeConverter] = []

    def factory() -> _FakeConverter:
        if not created:
            created.append(_FakeConverter())
        return created[-1]

    monkeypatch.setattr(pdf_module, "_get_converter", factory)
    return created


def _use_converter(
    created: list[_FakeConverter],
    *,
    markdown: str = "",
    error: Exception | None = None,
) -> None:
    created.append(_FakeConverter(markdown=markdown, error=error))


def test_parse_pdf_returns_markdown(fake_converter: list[_FakeConverter]) -> None:
    """正常路径：docling 转换结果原样返回 Markdown 文本。"""
    _use_converter(fake_converter, markdown="# 标题\n\n正文内容。")

    markdown = parse_pdf_to_markdown(b"%PDF-fake", filename="manual.pdf")

    assert markdown == "# 标题\n\n正文内容。"


def test_parse_pdf_rejects_empty_data() -> None:
    """空字节流直接拒绝，不进入 docling。"""
    with pytest.raises(ValueError, match="PDF 文件内容为空"):
        parse_pdf_to_markdown(b"", filename="empty.pdf")


def test_parse_pdf_rejects_blank_markdown(
    fake_converter: list[_FakeConverter],
) -> None:
    """提取文本全空白（扫描件）→ ValueError，提示不支持扫描件。"""
    _use_converter(fake_converter, markdown="  \n\t \n")

    with pytest.raises(ValueError, match="暂不支持扫描件"):
        parse_pdf_to_markdown(b"%PDF-fake", filename="scan.pdf")


def test_parse_pdf_wraps_docling_errors(
    fake_converter: list[_FakeConverter],
) -> None:
    """docling/底层异常统一包装为 ValueError，不向外泄漏。"""
    _use_converter(fake_converter, error=RuntimeError("docling internal boom"))

    with pytest.raises(ValueError, match="PDF 解析失败：broken.pdf"):
        parse_pdf_to_markdown(b"%PDF-fake", filename="broken.pdf")


def test_ingest_pdf_runs_normalize_and_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest_pdf：parse 之后正确走 normalize（清页码）与 chunk 链路。"""
    text = "# 保修政策\n\n第一段。\n- 12 -\n第二段。\n\n" + "补充说明。" * 40
    monkeypatch.setattr(
        pdf_module, "parse_pdf_to_markdown", lambda data, *, filename: text
    )

    result = ingest_pdf(b"%PDF-fake", source_name="保修手册")

    assert result.chunks
    assert all(chunk.suggested_category == "保修手册" for chunk in result.chunks)
    assert all("- 12 -" not in chunk.content for chunk in result.chunks)
    assert result.stats.chunk_count == len(result.chunks)


def test_ingest_pdf_passes_source_name_as_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest_pdf 把 <source_name>.pdf 作为 docling 文件名传入。"""
    captured: dict[str, str] = {}

    def fake_parse(data: bytes, *, filename: str) -> str:
        captured["filename"] = filename
        return _SAMPLE_MARKDOWN

    monkeypatch.setattr(pdf_module, "parse_pdf_to_markdown", fake_parse)

    ingest_pdf(b"%PDF-fake", source_name="保修手册")

    assert captured["filename"] == "保修手册.pdf"


def test_dispatch_markdown_extension() -> None:
    """.md / .markdown 走 Markdown 链路，source_name 为文件名去扩展名。"""
    text = "# 标题\n\n" + "正文内容。" * 30

    for filename in ("保修政策.md", "保修政策.markdown", "保修政策.MD"):
        result = ingest_document(text.encode("utf-8"), filename=filename)
        assert result.chunks
        assert all(chunk.suggested_category == "保修政策" for chunk in result.chunks)


def test_dispatch_rejects_non_utf8_markdown() -> None:
    """非 UTF-8 编码的 .md → ValueError，提示必须使用 UTF-8。"""
    with pytest.raises(ValueError, match="UTF-8"):
        ingest_document("正文".encode("gbk"), filename="旧文档.md")


def test_dispatch_pdf_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """.pdf 走 PDF 链路，source_name 为文件名去扩展名。"""
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_ingest_pdf(data: bytes, *, source_name: str) -> object:
        captured["data"] = data
        captured["source_name"] = source_name
        return sentinel

    monkeypatch.setattr(dispatch_module, "ingest_pdf", fake_ingest_pdf)

    result = ingest_document(b"%PDF-fake", filename="维修手册.pdf")

    assert result is sentinel
    assert captured["data"] == b"%PDF-fake"
    assert captured["source_name"] == "维修手册"


def test_dispatch_rejects_unsupported_extension() -> None:
    """不支持的扩展名 → ValueError，列出支持的扩展名。"""
    with pytest.raises(ValueError, match=r"不支持的文件类型.*\.md.*\.pdf"):
        ingest_document(b"data", filename="表格.xlsx")

    with pytest.raises(ValueError, match="不支持的文件类型"):
        ingest_document(b"data", filename="无扩展名")
