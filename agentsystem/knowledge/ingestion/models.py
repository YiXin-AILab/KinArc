"""知识库文档入库解析的领域模型（纯数据，无 I/O）。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IngestStats:
    """一次解析的统计信息，用于预览页质量提示。"""

    chunk_count: int
    min_chars: int
    max_chars: int
    avg_chars: int
    broken_tables: int
    dropped_images: int
    cleaned_lines: int


@dataclass(frozen=True, slots=True)
class IngestChunk:
    """一个解析出的候选知识块。"""

    index: int
    suggested_doc_id: str
    suggested_title: str
    suggested_category: str
    content: str
    section_path: str
    has_table: bool
    char_count: int


@dataclass(frozen=True, slots=True)
class IngestResult:
    """一次 Markdown 入库解析的完整结果。"""

    chunks: tuple[IngestChunk, ...]
    warnings: tuple[str, ...]
    stats: IngestStats
