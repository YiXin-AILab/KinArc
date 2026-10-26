"""知识库文档加载与 docs_path 安全解析测试。"""

import json
from pathlib import Path

import pytest
from agentsystem.knowledge.documents import load_documents, resolve_docs_path


def _write_docs(path: Path, items: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _document(doc_id: str = "doc-1", content: str = "工时费标准") -> dict[str, str]:
    return {
        "doc_id": doc_id,
        "title": f"标题 {doc_id}",
        "category": "pricing",
        "content": content,
        "updated_at": "2026-09-01",
    }


def test_load_documents_returns_documents_and_stable_hash(tmp_path: Path) -> None:
    """合法文档数组解析为 KnowledgeDocument，hash 只随文件内容变化。"""
    path = _write_docs(tmp_path / "kb.json", [_document()])

    documents, first_hash = load_documents(path)
    _, second_hash = load_documents(path)

    assert len(documents) == 1
    document = documents[0]
    assert document.doc_id == "doc-1"
    assert document.title == "标题 doc-1"
    assert document.category == "pricing"
    assert document.updated_at == "2026-09-01"
    assert first_hash == second_hash
    _write_docs(path, [_document(content="新内容")])
    _, changed_hash = load_documents(path)
    assert changed_hash != first_hash


def test_load_documents_rejects_invalid_structure(tmp_path: Path) -> None:
    """非数组、缺字段与重复 doc_id 都在加载期失败。"""
    object_path = _write_docs(tmp_path / "object.json", {"doc_id": "a"})
    with pytest.raises(ValueError, match="数组"):
        load_documents(object_path)

    missing_path = _write_docs(tmp_path / "missing.json", [{"doc_id": "a"}])
    with pytest.raises(ValueError, match="content"):
        load_documents(missing_path)

    duplicate_path = _write_docs(
        tmp_path / "duplicate.json",
        [_document(doc_id="a"), _document(doc_id="a", content="其他")],
    )
    with pytest.raises(ValueError, match="重复"):
        load_documents(duplicate_path)


def test_resolve_docs_path_accepts_relative_path_inside_root(tmp_path: Path) -> None:
    """相对路径解析到 Application 根内的绝对路径。"""
    docs = _write_docs(tmp_path / "app" / "data" / "kb.json", [])

    resolved = resolve_docs_path(
        application_root=tmp_path / "app",
        docs_path="data/kb.json",
        plugin_id="builtin.knowledge.knowledge-plugin",
        base_name="pricing",
    )

    assert resolved == docs


def test_resolve_docs_path_rejects_escape_and_missing_root(tmp_path: Path) -> None:
    """路径逃逸与缺少 ApplicationRoot 的相对路径都报出 Plugin 身份。"""
    docs = _write_docs(tmp_path / "app" / "data" / "kb.json", [])
    _write_docs(tmp_path / "outside.json", [])
    with pytest.raises(ValueError, match="knowledge-plugin"):
        resolve_docs_path(
            application_root=tmp_path / "app",
            docs_path="../outside.json",
            plugin_id="builtin.knowledge.knowledge-plugin",
            base_name="pricing",
        )
    with pytest.raises(ValueError, match="knowledge-plugin"):
        resolve_docs_path(
            application_root=None,
            docs_path="data/kb.json",
            plugin_id="builtin.knowledge.knowledge-plugin",
            base_name="pricing",
        )

    absolute = resolve_docs_path(
        application_root=None,
        docs_path=str(docs),
        plugin_id="builtin.knowledge.knowledge-plugin",
        base_name="pricing",
    )
    assert absolute == docs


def test_resolve_docs_path_accepts_absolute_path_with_application_root(
    tmp_path: Path,
) -> None:
    """平台知识库 documents.json 绝对路径不受 Application 根目录约束。"""
    docs = _write_docs(tmp_path / "knowledge" / "pricing" / "documents.json", [])

    resolved = resolve_docs_path(
        application_root=tmp_path / "app",
        docs_path=str(docs),
        plugin_id="builtin.knowledge.knowledge-plugin",
        base_name="pricing",
    )

    assert resolved == docs
