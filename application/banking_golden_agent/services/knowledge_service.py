"""Banking 独立知识库检索服务。提供基于 BM25 的全文检索与正则 Grep 检索。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import ClassVar

# rank-bm25 0.2.2 does not publish type metadata.
from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]


@dataclass(frozen=True)
class KnowledgeDocument:
    """知识库文档实体。"""

    id: str
    title: str
    content: str


class KnowledgeService:
    """独立知识库检索服务（单例模式）。"""

    _instance: ClassVar["KnowledgeService | None"] = None
    _lock: ClassVar[Lock] = Lock()

    def __init__(self, documents_dir: Path | None = None) -> None:
        if documents_dir is None:
            documents_dir = (
                Path(__file__).resolve().parent.parent / "data" / "documents"
            )
        self._documents_dir = documents_dir
        self._documents: dict[str, KnowledgeDocument] = {}
        self._doc_list: list[KnowledgeDocument] = []
        self._bm25: BM25Okapi | None = None
        self._load_and_index()

    @classmethod
    def get_instance(cls) -> "KnowledgeService":
        """获取或创建知识库服务单例。"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """使用与 Tau BM25 相同的空白分词。"""
        return text.lower().split()

    def _load_and_index(self) -> None:
        """加载文档并构建内存 BM25 倒排索引。"""
        self._documents.clear()
        self._doc_list.clear()
        self._bm25 = None

        if not self._documents_dir.exists():
            return

        for file_path in self._documents_dir.glob("*.json"):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                doc = KnowledgeDocument(
                    id=str(data.get("id", file_path.stem)),
                    title=str(data.get("title", "")),
                    content=str(data.get("content", "")),
                )
                self._documents[doc.id] = doc
                self._doc_list.append(doc)
            except Exception:
                continue

        if not self._doc_list:
            return
        corpus = [self._tokenize(document.content) for document in self._doc_list]
        self._bm25 = BM25Okapi(corpus)

    def search(self, query: str, top_k: int = 10) -> str:
        """基于 BM25 检索知识库并返回相关文档。"""
        preprocessing_started = perf_counter()
        preprocessing_ms = (perf_counter() - preprocessing_started) * 1000

        retrieval_started = perf_counter()
        query_terms = self._tokenize(query)
        results: list[tuple[KnowledgeDocument, float]] = []
        if query_terms and self._bm25 is not None:
            scores = self._bm25.get_scores(query_terms)
            indices = sorted(
                range(len(scores)),
                key=lambda index: scores[index],
                reverse=True,
            )[: min(top_k, len(self._doc_list))]
            results = [
                (self._doc_list[index], float(scores[index])) for index in indices
            ]
        retrieval_ms = (perf_counter() - retrieval_started) * 1000

        postprocessing_started = perf_counter()
        postprocessing_ms = (perf_counter() - postprocessing_started) * 1000
        total_ms = preprocessing_ms + retrieval_ms + postprocessing_ms
        content = (
            _format_results(results) if results else "No relevant documents found."
        )
        return (
            f"{content}\n\n[Timing: retrieval={retrieval_ms:.0f}ms, "
            f"reranking={postprocessing_ms:.0f}ms, total={total_ms:.0f}ms]"
        )

    def grep(self, pattern: str, case_sensitive: bool = False, top_k: int = 10) -> str:
        """使用正则表达式或字面值在所有知识库文档中检索。"""
        if not self._doc_list or not pattern.strip():
            return f"No matches found for pattern: {pattern}"

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags=flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags=flags)

        matches: list[tuple[int, KnowledgeDocument]] = []
        for doc in self._doc_list:
            count = len(regex.findall(doc.content))
            if count > 0:
                matches.append((count, doc))

        if not matches:
            return f"No matches found for pattern: {pattern}"

        matches.sort(key=lambda item: item[0], reverse=True)
        top_matches = matches[:top_k]

        return _format_results(
            [(document, float(count)) for count, document in top_matches]
        )

    def get_document(self, doc_id: str) -> str:
        """按文档 ID 精准获取知识库文档内容。"""
        doc = self._documents.get(doc_id)
        if doc is None:
            return f"Document '{doc_id}' not found."
        return f"Title: {doc.title}\nID: {doc.id}\nContent:\n{doc.content}"


def _format_results(results: list[tuple[KnowledgeDocument, float]]) -> str:
    """使用 Tau knowledge retrieval 的结果格式。"""
    formatted = []
    for index, (document, score) in enumerate(results, 1):
        formatted.append(
            f"{index}. {document.title or 'Untitled'}\n"
            f"   ID: {document.id}\n"
            f"   Score: {score:.4f}\n"
            f"   Content: {document.content}\n"
        )
    return "\n".join(formatted)
