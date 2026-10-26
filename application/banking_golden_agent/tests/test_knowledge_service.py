"""Banking knowledge retrieval compatibility tests."""

import json
from pathlib import Path

import pytest

from application.banking_golden_agent.services import knowledge_service
from application.banking_golden_agent.services.knowledge_service import KnowledgeService


def _write_document(
    documents_dir: Path,
    *,
    document_id: str,
    title: str,
    content: str,
) -> None:
    """Write one minimal knowledge document fixture."""
    payload = {"id": document_id, "title": title, "content": content}
    (documents_dir / f"{document_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_bm25_indexes_content_only_and_keeps_zero_score_results(
    tmp_path: Path,
) -> None:
    """Match Tau BM25 tokenization, ranking, and zero-score behavior."""
    _write_document(
        tmp_path,
        document_id="a-title-only",
        title="Needle",
        content="unrelated text",
    )
    _write_document(
        tmp_path,
        document_id="b-content-hit",
        title="Content Hit",
        content="needle appears here",
    )
    _write_document(
        tmp_path,
        document_id="c-unrelated",
        title="Other",
        content="nothing relevant",
    )

    service = KnowledgeService(tmp_path)

    result = service.search("needle", top_k=3)

    assert result.index("ID: b-content-hit") < result.index("ID: a-title-only")
    assert "ID: c-unrelated" in result
    assert "Score:" in result


def test_grep_searches_content_only_and_uses_tau_result_format(tmp_path: Path) -> None:
    """Exclude title-only matches and expose Tau-compatible grep output."""
    _write_document(
        tmp_path,
        document_id="a-title-only",
        title="Needle",
        content="unrelated text",
    )
    _write_document(
        tmp_path,
        document_id="b-content-hit",
        title="Content Hit",
        content="needle appears here",
    )

    service = KnowledgeService(tmp_path)

    result = service.grep("needle")

    assert result == (
        "1. Content Hit\n"
        "   ID: b-content-hit\n"
        "   Score: 1.0000\n"
        "   Content: needle appears here\n"
    )


def test_equal_scores_keep_tau_document_load_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BM25 and Grep ties follow Tau's unsorted directory iteration order."""
    _write_document(
        tmp_path,
        document_id="z-created-first",
        title="Created First",
        content="needle",
    )
    _write_document(
        tmp_path,
        document_id="a-created-second",
        title="Created Second",
        content="needle",
    )
    created_first = tmp_path / "z-created-first.json"
    created_second = tmp_path / "a-created-second.json"
    monkeypatch.setattr(
        Path,
        "glob",
        lambda _path, _pattern: iter((created_first, created_second)),
    )

    service = KnowledgeService(tmp_path)

    for result in (service.search("needle"), service.grep("needle")):
        assert result.index("ID: z-created-first") < result.index(
            "ID: a-created-second"
        )


def test_grep_rejects_whitespace_only_pattern(tmp_path: Path) -> None:
    """Tau treats a whitespace-only Grep pattern as an empty query."""
    _write_document(
        tmp_path,
        document_id="document",
        title="Document",
        content="contains   spaces",
    )

    result = KnowledgeService(tmp_path).grep("   ")

    assert result == "No matches found for pattern:    "


def test_bm25_formats_empty_query_with_tau_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return Tau's empty-result message and complete timing suffix."""
    _write_document(
        tmp_path,
        document_id="document",
        title="Document",
        content="document content",
    )
    clock = iter([1.0, 1.01, 2.0, 2.02, 3.0, 3.03])
    monkeypatch.setattr(
        knowledge_service,
        "perf_counter",
        clock.__next__,
        raising=False,
    )

    result = KnowledgeService(tmp_path).search("")

    assert result == (
        "No relevant documents found.\n\n"
        "[Timing: retrieval=20ms, reranking=30ms, total=60ms]"
    )
