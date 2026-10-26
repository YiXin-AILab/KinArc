"""知识库文档加载、校验与 docs_path 安全解析。"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """一条已校验的知识库文档。"""

    doc_id: str
    title: str
    category: str | None
    content: str
    updated_at: str | None


def resolve_docs_path(
    *,
    application_root: Path | None,
    docs_path: str,
    plugin_id: str,
    base_name: str,
) -> Path:
    """把配置中的 docs_path 解析为受控的绝对路径。

    绝对路径直接严格解析（平台知识库 documents.json 位于 Application 根
    目录之外）；相对路径必须解析到 Application 根目录内，缺少
    ApplicationRoot capability 时报错。

    Args:
        application_root: 相对路径的解析根目录；绝对路径不依赖此值。
        docs_path: 配置中的文档文件路径。
        plugin_id: 用于错误定位的插件标识。
        base_name: 用于错误定位的知识库名称。

    Returns:
        已确认存在且满足上述路径约束的绝对路径。

    Raises:
        ValueError: 相对路径缺少 Application 根目录，或解析结果逃逸根目录。
        FileNotFoundError: 文档文件不存在。
    """
    raw = Path(docs_path)
    if raw.is_absolute():
        return raw.resolve(strict=True)
    if application_root is None:
        raise ValueError(
            f"Plugin {plugin_id} 知识库 {base_name} 使用相对 docs_path，"
            "但缺少 ApplicationRoot capability"
        )
    candidate = (application_root / raw).resolve(strict=True)
    try:
        candidate.relative_to(application_root)
    except ValueError as error:
        raise ValueError(
            f"Plugin {plugin_id} 知识库 {base_name} 的 docs_path "
            "必须位于 Application 根目录内"
        ) from error
    return candidate


def load_documents(path: Path) -> tuple[tuple[KnowledgeDocument, ...], str]:
    """读取并校验 JSON 文档数组，计算文件内容摘要。

    Args:
        path: UTF-8 编码的 JSON 文档文件路径。

    Returns:
        按文件顺序排列的文档元组，以及原始文件字节的 SHA-256 摘要。

    Raises:
        OSError: 文件不存在或不可读。
        ValueError: 文件不是 JSON 数组，或条目缺少必填字段、字段类型错误、
            doc_id 重复。
    """
    raw_bytes = path.read_bytes()
    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"知识库文档文件不是合法 JSON：{path.name}") from error
    if not isinstance(raw, list):
        raise ValueError(f"知识库文档文件必须是 JSON 数组：{path.name}")
    documents = tuple(_parse_document(item, index) for index, item in enumerate(raw))
    identifiers = tuple(document.doc_id for document in documents)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"知识库文档 doc_id 重复：{path.name}")
    return documents, content_hash


def _parse_document(item: Any, index: int) -> KnowledgeDocument:
    if not isinstance(item, Mapping):
        raise ValueError(f"知识库第 {index} 条文档必须是 JSON 对象")
    doc_id = _required_text(item, "doc_id", index)
    content = _required_text(item, "content", index)
    title = _optional_text(item, "title", index) or ""
    return KnowledgeDocument(
        doc_id=doc_id,
        title=title,
        category=_optional_text(item, "category", index),
        content=content,
        updated_at=_optional_text(item, "updated_at", index),
    )


def _required_text(item: Mapping[str, Any], field: str, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"知识库第 {index} 条文档缺少非空字段：{field}")
    return value


def _optional_text(item: Mapping[str, Any], field: str, index: int) -> str | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"知识库第 {index} 条文档字段必须是字符串：{field}")
    return value
