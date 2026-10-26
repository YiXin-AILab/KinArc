"""Markdown 清洗与结构感知分块（纯函数，无 I/O 副作用）。

面向「文档上传入库」链路：``normalize_markdown`` 修复 PDF 转 Markdown
的典型暗伤（页眉页脚、断行、误识别标题、图片引用），``chunk_markdown``
按标题树做结构感知分块，``ingest_markdown`` 组合两者并产出统计信息。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from agentsystem.knowledge.ingestion.models import (
    IngestChunk,
    IngestResult,
    IngestStats,
)

_DOC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(\S.*)$")
_FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# CJK 统一表意文字、CJK 标点与全角形式
_CJK_PATTERN = re.compile(r"[一-鿿　-〿＀-￯㐀-䶿]")
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？!?])\s*")
_PAGE_NUMBER_PATTERNS = (
    re.compile(r"^-\s*\d{1,6}\s*-$"),
    re.compile(r"^\d{1,6}$"),
    re.compile(r"^第\s*\d{1,6}\s*页$"),
)
_SENTENCE_ENDINGS = "。！？!?"
_SUSPICIOUS_CHAR_RATIO = 0.01
_HEADER_FOOTER_MIN_OCCURRENCES = 3
_HEADER_FOOTER_MAX_LENGTH = 30
_HEADING_MAX_LENGTH = 50
_MAX_OVERLAP_CHARS = 120
_TITLE_PREVIEW_CHARS = 30


@dataclass(frozen=True, slots=True)
class _Normalization:
    """清洗结果与各项计数，供 ingest 组装统计信息。"""

    text: str
    warnings: tuple[str, ...]
    cleaned_lines: int
    broken_tables: int
    dropped_images: int


@dataclass(frozen=True, slots=True)
class _Block:
    """分块阶段的最小内容单元，表格/围栏代码块/列表为原子单元。"""

    kind: str
    text: str
    level: int = 0


@dataclass(slots=True)
class _Section:
    """标题树节点；blocks 只含本节点直接正文，不含子节点内容。"""

    heading: str
    level: int
    blocks: list[_Block] = field(default_factory=list)
    children: list[_Section] = field(default_factory=list)
    parent: _Section | None = None


@dataclass(frozen=True, slots=True)
class _Piece:
    """装箱阶段不可再切分的内容片。"""

    text: str
    has_table: bool


def normalize_markdown(text: str) -> tuple[str, tuple[str, ...]]:
    """清洗 PDF 转出的 Markdown 常见暗伤。

    规则：删除独立成行的页码与全文出现 ≥3 次的短页眉页脚行；合并段落
    内部断行（中文边界直接拼接，否则补一个空格；表格、围栏代码块、列表
    不合并）；把 >50 字或以句号结尾的疑似误识别标题降级为正文；校验
    GFM 表格列数一致性，破损表格保留原文并记 warning；图片引用替换为
    ``[图片: alt]`` 占位文字。

    Args:
        text: 原始 Markdown 文本。

    Returns:
        二元组 ``(清洗后文本, warnings)``。

    Raises:
        ValueError: 文本中 U+FFFD 或控制字符占比超过 1%，疑似编码错误
            或转换质量不佳。
    """
    result = _normalize_impl(text)
    return result.text, result.warnings


def chunk_markdown(
    text: str,
    *,
    source_name: str,
    max_chars: int = 800,
    min_chars: int = 100,
) -> tuple[IngestChunk, ...]:
    """按标题树做结构感知分块，返回候选知识块。

    表格、围栏代码块、列表是原子单元，绝不从中间切开；叶子 section 正文
    超过 ``max_chars`` 时在段落边界切分，相邻块保留最后一个完整句子作为
    重叠；不足 ``min_chars`` 的碎片 section 向上合并到父级。

    Args:
        text: Markdown 文本（通常先经 ``normalize_markdown`` 清洗）。
        source_name: 来源名，作为每个 chunk 的 suggested_category。
        max_chars: 单个 chunk 正文的软上限。
        min_chars: 碎片 section 向上合并的阈值。

    Returns:
        按文档顺序排列的候选知识块。
    """
    root = _build_sections(_parse_blocks(text))
    _merge_fragments(root, min_chars)
    taken: set[str] = set()
    chunks: list[IngestChunk] = []
    for section in _iter_sections(root):
        pieces = _pieces_from_blocks(section.blocks, max_chars)
        for group in _pack_pieces(pieces, max_chars):
            chunk = _build_chunk(section, group, source_name, len(chunks), taken)
            chunks.append(chunk)
    return tuple(chunks)


def ingest_markdown(text: str, *, source_name: str) -> IngestResult:
    """清洗 + 分块 + 组装统计信息，一次完成 Markdown 入库解析。

    Args:
        text: 原始 Markdown 文本。
        source_name: 来源名，作为 suggested_category。

    Returns:
        含 chunks、warnings 与 stats 的完整解析结果。

    Raises:
        ValueError: 文本为空或全空白；或乱码/控制字符占比超过 1%。
    """
    if not text.strip():
        raise ValueError("文档内容为空，无法入库：请上传非空 Markdown 文档")
    normalized = _normalize_impl(text)
    chunks = chunk_markdown(normalized.text, source_name=source_name)
    return IngestResult(
        chunks=chunks,
        warnings=normalized.warnings,
        stats=_build_stats(chunks, normalized),
    )


def _normalize_impl(text: str) -> _Normalization:
    """按固定顺序执行清洗规则并收集计数。"""
    _check_suspicious_chars(text)
    replaced, dropped_images = _replace_images(text)
    kept, cleaned_lines = _remove_noise_lines(replaced.split("\n"))
    downgraded, heading_warnings = _downgrade_headings(kept)
    table_warnings, broken_tables = _validate_tables(downgraded)
    return _Normalization(
        text=_merge_broken_lines(downgraded),
        warnings=tuple(heading_warnings + table_warnings),
        cleaned_lines=cleaned_lines,
        broken_tables=broken_tables,
        dropped_images=dropped_images,
    )


def _check_suspicious_chars(text: str) -> None:
    """乱码或控制字符占比超过阈值时拒绝解析。"""
    if not text:
        return
    suspicious = sum(1 for char in text if _is_suspicious_char(char))
    if suspicious / len(text) > _SUSPICIOUS_CHAR_RATIO:
        raise ValueError(
            "文档包含超过 1% 的乱码（U+FFFD）或控制字符，"
            "疑似文件编码错误或转换质量不佳，请检查源文件后重新上传"
        )


def _is_suspicious_char(char: str) -> bool:
    # U+FFFD 替换字符或非常用控制字符都视为可疑
    return ord(char) == 0xFFFD or (
        unicodedata.category(char) == "Cc" and char not in "\t\n\r"
    )


def _replace_images(text: str) -> tuple[str, int]:
    """把图片引用替换为占位文字，返回替换后的文本与替换数量。"""
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        alt = match.group(1).strip() or "未命名图片"
        return f"[图片: {alt}]"

    return _IMAGE_PATTERN.sub(repl, text), count


def _fence_flags(lines: list[str]) -> list[bool]:
    """标记每一行是否位于围栏代码块内部。"""
    flags: list[bool] = []
    in_fence = False
    for line in lines:
        flags.append(in_fence)
        if _FENCE_PATTERN.match(line):
            in_fence = not in_fence
    return flags


def _is_noise_candidate(line: str, is_fenced: bool) -> bool:
    """判断一行是否可能参与页眉页脚/页码清理（保护结构性行）。"""
    stripped = line.strip()
    return bool(
        stripped
        and not is_fenced
        and len(stripped) <= _HEADER_FOOTER_MAX_LENGTH
        and not _FENCE_PATTERN.match(line)
        and not _HEADING_PATTERN.match(line)
        and not _is_table_line(stripped)
    )


def _is_page_number(stripped: str) -> bool:
    return any(pattern.match(stripped) for pattern in _PAGE_NUMBER_PATTERNS)


def _remove_noise_lines(lines: list[str]) -> tuple[list[str], int]:
    """删除独立成行的页码与出现 ≥3 次的短页眉页脚行。"""
    fenced = _fence_flags(lines)
    counter: Counter[str] = Counter()
    for line, is_fenced in zip(lines, fenced, strict=True):
        if _is_noise_candidate(line, is_fenced):
            counter[line.strip()] += 1
    repeated = {
        line
        for line, count in counter.items()
        if count >= _HEADER_FOOTER_MIN_OCCURRENCES
    }
    kept: list[str] = []
    removed = 0
    for line, is_fenced in zip(lines, fenced, strict=True):
        stripped = line.strip()
        is_repeated = _is_noise_candidate(line, is_fenced) and stripped in repeated
        is_page_number = not is_fenced and _is_page_number(stripped)
        if is_repeated or is_page_number:
            removed += 1
            continue
        kept.append(line)
    return kept, removed


def _is_suspicious_heading(title: str) -> bool:
    return len(title) > _HEADING_MAX_LENGTH or title.endswith(("。", "."))


def _downgrade_headings(lines: list[str]) -> tuple[list[str], list[str]]:
    """把疑似误识别的标题降级为正文（去掉 # 前缀）。"""
    fenced = _fence_flags(lines)
    result: list[str] = []
    warnings: list[str] = []
    for line, is_fenced in zip(lines, fenced, strict=True):
        match = _HEADING_PATTERN.match(line)
        title = match.group(2).strip() if match else ""
        if is_fenced or not title or not _is_suspicious_heading(title):
            result.append(line)
            continue
        warnings.append(f"疑似误识别的标题已降级为正文：{title[:_TITLE_PREVIEW_CHARS]}")
        result.append(title)
    return result, warnings


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|")


def _column_count(line: str) -> int:
    return len(line.strip().strip("|").split("|"))


def _validate_tables(lines: list[str]) -> tuple[list[str], int]:
    """校验 GFM 表格块列数一致性；破损表格保留原文并记 warning。"""
    fenced = _fence_flags(lines)
    warnings: list[str] = []
    broken = 0
    index = 0
    while index < len(lines):
        if fenced[index] or not _is_table_line(lines[index]):
            index += 1
            continue
        end = index
        while end < len(lines) and not fenced[end] and _is_table_line(lines[end]):
            end += 1
        columns = {_column_count(line) for line in lines[index:end]}
        if len(columns) > 1:
            broken += 1
            warnings.append(
                f"检测到列数不一致的破损表格（第 {index + 1} 行起），已保留原文"
            )
        index = end
    return warnings, broken


def _is_cjk_char(char: str) -> bool:
    return bool(_CJK_PATTERN.match(char))


def _join_lines(lines: list[str]) -> str:
    """合并段落内部断行：中文边界直接拼接，否则补一个空格。"""
    result = lines[0].strip()
    for line in lines[1:]:
        nxt = line.strip()
        if not nxt:
            continue
        if result and (_is_cjk_char(result[-1]) or _is_cjk_char(nxt[0])):
            result += nxt
        else:
            result += " " + nxt
    return result


def _is_structural_line(line: str) -> bool:
    return bool(
        _HEADING_PATTERN.match(line) or _LIST_MARKER.match(line) or _is_table_line(line)
    )


def _merge_broken_lines(lines: list[str]) -> str:
    """合并段落内部断行，保护表格、围栏代码块、列表与标题。"""
    merged: list[str] = []
    run: list[str] = []
    in_fence = False

    def flush() -> None:
        if run:
            merged.append(_join_lines(run))
            run.clear()

    for line in lines:
        if _FENCE_PATTERN.match(line):
            flush()
            in_fence = not in_fence
            merged.append(line)
        elif in_fence or not line.strip() or _is_structural_line(line):
            flush()
            merged.append(line)
        else:
            run.append(line)
    flush()
    return "\n".join(merged)


def _is_list_line(line: str) -> bool:
    return bool(_LIST_MARKER.match(line))


def _starts_special_block(line: str) -> bool:
    return bool(
        _FENCE_PATTERN.match(line)
        or _HEADING_PATTERN.match(line)
        or _is_table_line(line)
        or _is_list_line(line)
    )


def _read_run(lines: list[str], start: int, predicate: Callable[[str], bool]) -> int:
    """从 start 起读取连续满足 predicate 的行，返回结束下标。"""
    end = start
    while end < len(lines) and predicate(lines[end]):
        end += 1
    return end


def _read_fence_end(lines: list[str], start: int) -> int:
    """返回围栏代码块（含收尾围栏，若有）的结束下标。"""
    end = start + 1
    while end < len(lines) and not _FENCE_PATTERN.match(lines[end]):
        end += 1
    return min(end + 1, len(lines))


def _read_block(lines: list[str], start: int) -> tuple[_Block, int]:
    """从 start 行读取一个内容块（标题/代码/表格/列表/段落）。"""
    line = lines[start]
    heading = _HEADING_PATTERN.match(line)
    if heading:
        level = len(heading.group(1))
        return _Block("heading", heading.group(2).strip(), level), start + 1
    if _FENCE_PATTERN.match(line):
        end = _read_fence_end(lines, start)
        return _Block("code", "\n".join(lines[start:end])), end
    if _is_table_line(line):
        end = _read_run(lines, start, _is_table_line)
        return _Block("table", "\n".join(lines[start:end])), end
    if _is_list_line(line):
        end = _read_run(lines, start, _is_list_line)
        return _Block("list", "\n".join(lines[start:end])), end
    end = _read_run(lines, start, _is_plain_paragraph_line)
    return _Block("paragraph", "\n".join(lines[start:end])), end


def _is_plain_paragraph_line(line: str) -> bool:
    return bool(line.strip()) and not _starts_special_block(line)


def _parse_blocks(text: str) -> list[_Block]:
    """把文本解析为有序内容块，空行只作分隔。"""
    lines = text.split("\n")
    blocks: list[_Block] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        block, index = _read_block(lines, index)
        blocks.append(block)
    return blocks


def _build_sections(blocks: list[_Block]) -> _Section:
    """按标题层级构建 section 树，根节点是无标题的伪 section。"""
    root = _Section(heading="", level=0)
    stack = [root]
    for block in blocks:
        if block.kind != "heading":
            stack[-1].blocks.append(block)
            continue
        while len(stack) > 1 and stack[-1].level >= block.level:
            stack.pop()
        section = _Section(heading=block.text, level=block.level, parent=stack[-1])
        stack[-1].children.append(section)
        stack.append(section)
    return root


def _section_text_length(section: _Section) -> int:
    return sum(len(block.text) for block in section.blocks)


def _merge_fragments(section: _Section, min_chars: int) -> None:
    """自底向上把不足 min_chars 的碎片叶子 section 合并进父级。"""
    for child in section.children:
        _merge_fragments(child, min_chars)
    kept: list[_Section] = []
    for child in section.children:
        if child.children or _section_text_length(child) >= min_chars:
            kept.append(child)
            continue
        section.blocks.append(_Block(kind="paragraph", text=child.heading))
        section.blocks.extend(child.blocks)
    section.children = kept


def _iter_sections(root: _Section) -> Iterator[_Section]:
    """按文档顺序先序遍历 section 树（含根伪 section）。"""
    yield root
    for child in root.children:
        yield from _iter_sections(child)


def _section_path(section: _Section) -> str:
    """祖先标题用 / 拼接，无标题时返回空串。"""
    parts: list[str] = []
    node: _Section | None = section
    while node is not None:
        if node.heading:
            parts.append(node.heading)
        node = node.parent
    return "/".join(reversed(parts))


def _split_paragraph(text: str, max_chars: int) -> list[str]:
    """超长单段落按句子贪心装箱；单句超限时任其超限。"""
    sentences = [s for s in _SENTENCE_SPLIT_PATTERN.split(text) if s.strip()]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current + sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current.strip():
        parts.append(current)
    return parts


def _pieces_from_blocks(blocks: list[_Block], max_chars: int) -> list[_Piece]:
    """把内容块转为不可再切分的内容片，原子单元整块保留。"""
    pieces: list[_Piece] = []
    for block in blocks:
        if block.kind == "paragraph" and len(block.text) > max_chars:
            pieces.extend(
                _Piece(text=part, has_table=False)
                for part in _split_paragraph(block.text, max_chars)
            )
        else:
            pieces.append(_Piece(text=block.text, has_table=block.kind == "table"))
    return pieces


def _last_complete_sentence(text: str) -> str:
    """取文本末尾最后一个完整句子作为重叠，过长时放弃重叠。"""
    endings = [index for index, char in enumerate(text) if char in _SENTENCE_ENDINGS]
    if not endings:
        return ""
    start = endings[-2] + 1 if len(endings) > 1 else 0
    sentence = text[start : endings[-1] + 1].strip()
    return sentence if len(sentence) <= _MAX_OVERLAP_CHARS else ""


def _pack_pieces(pieces: list[_Piece], max_chars: int) -> list[list[_Piece]]:
    """按 max_chars 贪心装箱，跨块边界保留末句重叠。"""
    groups: list[list[_Piece]] = []
    current: list[_Piece] = []
    size = 0
    for piece in pieces:
        if current and size + len(piece.text) > max_chars:
            groups.append(current)
            overlap = _last_complete_sentence("\n\n".join(p.text for p in current))
            current = [_Piece(text=overlap, has_table=False)] if overlap else []
            size = len(overlap)
        current.append(piece)
        size += len(piece.text)
    if current:
        groups.append(current)
    return groups


def _slugify(title: str) -> str:
    """标题 slug 化：小写、非字母数字转 -、去连续 -，最长 64 字符。"""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    return slug.strip("-")[:64].rstrip("-")


def _make_doc_id(title: str, content: str, taken: set[str]) -> str:
    """生成合法且批内唯一的 suggested_doc_id。"""
    base = _slugify(title)
    if not base or not _DOC_ID_PATTERN.match(base):
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]
        base = f"doc-{digest}"
    candidate = base
    counter = 1
    while candidate in taken:
        counter += 1
        suffix = f"-{counter}"
        candidate = f"{base[: 64 - len(suffix)]}{suffix}"
    taken.add(candidate)
    return candidate


def _build_chunk(
    section: _Section,
    group: list[_Piece],
    source_name: str,
    index: int,
    taken: set[str],
) -> IngestChunk:
    """把一组内容片组装为一个候选知识块。"""
    body = "\n\n".join(piece.text for piece in group)
    content = f"{section.heading}\n\n{body}" if section.heading else body
    title = section.heading or body.split("\n", 1)[0].strip()[:_TITLE_PREVIEW_CHARS]
    return IngestChunk(
        index=index,
        suggested_doc_id=_make_doc_id(title, content, taken),
        suggested_title=title,
        suggested_category=source_name,
        content=content,
        section_path=_section_path(section),
        has_table=any(piece.has_table for piece in group),
        char_count=len(content),
    )


def _build_stats(
    chunks: tuple[IngestChunk, ...], normalized: _Normalization
) -> IngestStats:
    """基于 chunk char_count 与清洗计数组装统计信息。"""
    counts = [chunk.char_count for chunk in chunks]
    return IngestStats(
        chunk_count=len(chunks),
        min_chars=min(counts) if counts else 0,
        max_chars=max(counts) if counts else 0,
        avg_chars=round(sum(counts) / len(counts)) if counts else 0,
        broken_tables=normalized.broken_tables,
        dropped_images=normalized.dropped_images,
        cleaned_lines=normalized.cleaned_lines,
    )
