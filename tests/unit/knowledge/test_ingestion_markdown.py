"""知识库 Markdown 清洗与结构感知分块测试。"""

import re

import pytest
from agentsystem.knowledge.ingestion import (
    chunk_markdown,
    ingest_markdown,
    normalize_markdown,
)

_DOC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def test_normalize_removes_page_number_lines() -> None:
    """独立成行的页码（- 12 -、13、第 14 页）被删除，相邻正文断行合并。"""
    text = (
        "第一节内容。\n- 12 -\n第二节内容。\n\n"
        "13\n\n第三节内容。\n第 14 页\n第四节内容。"
    )

    cleaned, _ = normalize_markdown(text)

    assert "- 12 -" not in cleaned
    assert "第 14 页" not in cleaned
    assert "第一节内容。第二节内容。" in cleaned
    assert "第三节内容。第四节内容。" in cleaned


def test_normalize_removes_repeated_header_footer_lines() -> None:
    """全文出现 ≥3 次的相同短行视为页眉页脚删除，只出现 2 次的保留。"""
    header = "某公司产品手册"
    text = (
        f"{header}\n\n第一章内容概述。\n\n{header}\n\n"
        f"第二章内容概述。\n\n{header}\n\n第三章内容概述。"
    )

    cleaned, _ = normalize_markdown(text)

    assert header not in cleaned
    assert "第三章内容概述。" in cleaned

    twice = f"{header}\n\n正文第一段。\n\n{header}"
    cleaned_twice, _ = normalize_markdown(twice)
    assert cleaned_twice.count(header) == 2


def test_normalize_merges_chinese_broken_lines_without_space() -> None:
    """中文字符结尾/开头的段落内断行直接拼接，不补空格。"""
    text = "这是第一\n行内容。\n\n下一段保持不变。"

    cleaned, _ = normalize_markdown(text)

    assert "这是第一行内容。" in cleaned
    assert "下一段保持不变。" in cleaned


def test_normalize_merges_english_broken_lines_with_space() -> None:
    """英文断行合并时补一个空格。"""
    text = "First line is broken\nacross two lines."

    cleaned, _ = normalize_markdown(text)

    assert cleaned == "First line is broken across two lines."


def test_normalize_keeps_table_lines_untouched() -> None:
    """列表格行不参与断行合并，列数一致时不产生 warning。"""
    text = "| 配件 | 价格 |\n|---|---|\n| 轮胎 | 800 元 |"

    cleaned, warnings = normalize_markdown(text)

    assert cleaned == text
    assert warnings == ()


def test_normalize_keeps_fenced_code_block_untouched() -> None:
    """围栏代码块内部不清理、不合并。"""
    code = "```python\ndef foo():\n    return 1\n```"
    text = f"前文介绍。\n\n{code}\n\n后文说明。"

    cleaned, _ = normalize_markdown(text)

    assert code in cleaned


def test_normalize_downgrades_overlong_heading() -> None:
    """超过 50 字的标题行降级为正文并记 warning。"""
    title = "这是一个本不该作为标题的超长句子" * 5
    text = f"# {title}\n\n正文内容。"

    cleaned, warnings = normalize_markdown(text)

    assert f"# {title}" not in cleaned
    assert title in cleaned
    assert len(warnings) == 1
    assert "降级" in warnings[0]


def test_normalize_downgrades_heading_ending_with_period() -> None:
    """以中文句号结尾的标题降级为正文，正常标题保留。"""
    text = "## 正常标题\n\n## 这是一个句号结尾的标题。\n\n正文内容。"

    cleaned, warnings = normalize_markdown(text)

    assert "## 正常标题" in cleaned
    assert "## 这是一个句号结尾的标题。" not in cleaned
    assert "这是一个句号结尾的标题。" in cleaned
    assert len(warnings) == 1


def test_normalize_warns_and_keeps_broken_table() -> None:
    """列数不一致的破损表格保留原文并记 warning。"""
    text = "| 配件 | 价格 |\n|---|---|\n| 轮胎 | 800 | 另计 |"

    cleaned, warnings = normalize_markdown(text)

    assert any("表格" in warning for warning in warnings)
    assert "| 轮胎 | 800 | 另计 |" in cleaned


def test_normalize_replaces_images_with_placeholder() -> None:
    """图片引用替换为占位文字，空 alt 使用「未命名图片」。"""
    text = "![结构图](images/structure.png)\n\n![](images/unknown.png)"

    cleaned, _ = normalize_markdown(text)

    assert "[图片: 结构图]" in cleaned
    assert "[图片: 未命名图片]" in cleaned
    assert "![" not in cleaned


def test_normalize_rejects_text_with_too_many_suspicious_chars() -> None:
    """U+FFFD 或控制字符占比 >1% 时抛 ValueError。"""
    text = "这是一段完全正常的中文正文内容。" * 10 + chr(0xFFFD) * 4

    with pytest.raises(ValueError, match="乱码"):
        normalize_markdown(text)


def test_chunk_builds_section_path_from_heading_tree() -> None:
    """section_path 由祖先标题用 / 拼接。"""
    text = (
        "# 保修政策\n\n"
        "总述。\n\n"
        "## 3.2 易损件保修范围\n\n"
        "轮胎、电瓶等易损件按行驶里程计算保修期。"
    )

    chunks = chunk_markdown(text, source_name="policy", max_chars=800, min_chars=1)

    assert [chunk.section_path for chunk in chunks] == [
        "保修政策",
        "保修政策/3.2 易损件保修范围",
    ]


def test_chunk_prefixes_content_with_last_level_heading() -> None:
    """content 由最末级标题前缀行 + 空行 + 正文组成。"""
    text = "# 保修政策\n\n整车保修三年或十万公里。"

    (chunk,) = chunk_markdown(text, source_name="policy", max_chars=800, min_chars=1)

    assert chunk.content == "保修政策\n\n整车保修三年或十万公里。"
    assert chunk.suggested_title == "保修政策"
    assert chunk.suggested_category == "policy"
    assert chunk.char_count == len(chunk.content)
    assert chunk.has_table is False
    assert chunk.index == 0


def test_chunk_splits_long_section_at_paragraphs_with_overlap() -> None:
    """超过 max_chars 时在段落边界切分，相邻块保留最后一个完整句子重叠。"""
    text = (
        "# 保修政策\n\n"
        "第一段第一句。第一段第二句。\n\n"
        "第二段第一句。第二段第二句。\n\n"
        "第三段第一句。第三段第二句。\n\n"
        "第四段第一句。第四段第二句。"
    )

    chunks = chunk_markdown(text, source_name="test", max_chars=50, min_chars=1)

    assert len(chunks) == 2
    assert chunks[0].section_path == chunks[1].section_path == "保修政策"
    assert "第三段第二句。" in chunks[0].content
    assert chunks[1].content.split("\n\n")[1] == "第三段第二句。"


def test_chunk_keeps_table_atomic_when_exceeding_max_chars() -> None:
    """表格是原子单元，超过 max_chars 也整块保留不拆。"""
    table = "| 配件 | 价格 |\n|---|---|\n| 轮胎 | 800 元 |\n| 电瓶 | 1200 元 |"
    text = f"# 价格表\n\n配件价格如下。\n\n{table}"

    chunks = chunk_markdown(text, source_name="test", max_chars=20, min_chars=1)

    table_chunks = [chunk for chunk in chunks if chunk.has_table]
    assert len(table_chunks) == 1
    assert table in table_chunks[0].content


def test_chunk_merges_fragment_section_into_parent() -> None:
    """不足 min_chars 的碎片叶子 section 向上合并到父级。"""
    parent_body = "本规定适用于全系车型。" * 10
    text = f"# 保修政策\n\n{parent_body}\n\n## 附则\n\n很短。"

    chunks = chunk_markdown(text, source_name="test")

    assert len(chunks) == 1
    assert chunks[0].section_path == "保修政策"
    assert "附则" in chunks[0].content
    assert "很短。" in chunks[0].content


def test_chunk_handles_document_without_headings() -> None:
    """无标题文档整体视为一个 section，section_path 为空，标题取首行前 30 字。"""
    text = "本手册介绍产品使用注意事项。\n\n第二章内容继续展开说明。"

    (chunk,) = chunk_markdown(text, source_name="manual", max_chars=800, min_chars=1)

    assert chunk.section_path == ""
    assert chunk.content == text
    assert chunk.suggested_title == "本手册介绍产品使用注意事项。"[:30]
    assert chunk.suggested_doc_id.startswith("doc-")


def test_chunk_splits_headingless_long_document() -> None:
    """无标题超长文档同样按段落切分。"""
    text = "\n\n".join(f"第{index}段内容很长用来凑长度。" for index in range(6))

    chunks = chunk_markdown(text, source_name="test", max_chars=40, min_chars=1)

    assert len(chunks) >= 2
    assert all(chunk.section_path == "" for chunk in chunks)


def test_chunk_slugifies_english_title_for_doc_id() -> None:
    """英文标题 slug 化为小写连字符形式。"""
    text = "# Warranty Policy\n\nAll models are covered for three years."

    (chunk,) = chunk_markdown(text, source_name="policy", max_chars=800, min_chars=1)

    assert chunk.suggested_doc_id == "warranty-policy"


def test_chunk_falls_back_to_hash_doc_id_for_chinese_title() -> None:
    """中文标题 slug 为空时回退 doc-<sha1[:8]>，且满足 doc_id 格式约束。"""
    text = "## 保修政策\n\n整车保修三年或十万公里。"

    (chunk,) = chunk_markdown(text, source_name="policy", max_chars=800, min_chars=1)

    assert chunk.suggested_doc_id.startswith("doc-")
    assert len(chunk.suggested_doc_id) == len("doc-") + 8
    assert _DOC_ID_PATTERN.fullmatch(chunk.suggested_doc_id)


def test_chunk_deduplicates_doc_ids_within_batch() -> None:
    """同一批内重复 doc_id 追加 -2 后缀；中文同标题同内容走 hash 后缀。"""
    text = "# Intro\n\nFirst intro body.\n\n# Intro\n\nSecond intro body."

    chunks = chunk_markdown(text, source_name="test", max_chars=800, min_chars=1)

    assert [chunk.suggested_doc_id for chunk in chunks] == ["intro", "intro-2"]

    chinese = "# 说明\n\n完全相同的内容。\n\n# 说明\n\n完全相同的内容。"
    chinese_chunks = chunk_markdown(
        chinese, source_name="test", max_chars=800, min_chars=1
    )
    first, second = (chunk.suggested_doc_id for chunk in chinese_chunks)
    assert first.startswith("doc-")
    assert second == f"{first}-2"
    assert all(
        _DOC_ID_PATTERN.fullmatch(chunk.suggested_doc_id)
        for chunk in (*chunks, *chinese_chunks)
    )


def test_ingest_markdown_reports_stats_and_warnings() -> None:
    """stats 基于 chunk char_count，并透出清洗计数与破损表格 warning。"""
    paragraph = "整车保修三年或十万公里，以先到者为准。" * 6
    text = (
        "某公司产品手册\n\n某公司产品手册\n\n某公司产品手册\n\n"
        "# 保修政策\n\n"
        "![结构图](images/structure.png)\n\n"
        "| 配件 | 价格 |\n|---|---|\n| 轮胎 | 800 | 另计 |\n\n"
        f"{paragraph}"
    )

    result = ingest_markdown(text, source_name="保修手册")

    assert result.stats.chunk_count == len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.section_path == "保修政策"
    assert result.stats.broken_tables == 1
    assert result.stats.dropped_images == 1
    assert result.stats.cleaned_lines == 3
    assert result.stats.min_chars == result.stats.max_chars == chunk.char_count
    assert result.stats.avg_chars == chunk.char_count
    assert "[图片: 结构图]" in chunk.content
    assert "某公司产品手册" not in chunk.content
    assert any("表格" in warning for warning in result.warnings)


def test_ingest_markdown_rejects_empty_text() -> None:
    """空文本或全空白文本抛 ValueError。"""
    with pytest.raises(ValueError, match="为空"):
        ingest_markdown("", source_name="test")
    with pytest.raises(ValueError, match="为空"):
        ingest_markdown("  \n\t \n", source_name="test")
