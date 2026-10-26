"""单知识库的文档存储、向量索引持久化与多策略检索。"""

import asyncio
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# rank-bm25 0.2.2 未提供类型元数据。
from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

from agentsystem.common.errors import AdapterError
from agentsystem.knowledge.documents import KnowledgeDocument, load_documents
from agentsystem.knowledge.embeddings import EmbedTexts
from agentsystem.knowledge.models import RetrievalConfig, RetrievalStrategy

LOGGER = logging.getLogger(__name__)

_ASCII_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_CJK_SEGMENT_PATTERN = re.compile(r"[一-鿿]+")


@dataclass(frozen=True, slots=True)
class ScoredDocument:
    """一次检索命中的文档与其当前阶段得分。"""

    document: KnowledgeDocument
    score: float


def tokenize(text: str) -> list[str]:
    """英文按小写词切分，中文按字符级 bigram 切分。"""
    tokens = _ASCII_TOKEN_PATTERN.findall(text.lower())
    for segment in _CJK_SEGMENT_PATTERN.findall(text):
        if len(segment) == 1:
            tokens.append(segment)
        else:
            tokens.extend(
                segment[index : index + 2] for index in range(len(segment) - 1)
            )
    return tokens


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个等长向量的余弦相似度；零向量返回 0。

    Args:
        left: 第一个向量。
        right: 与第一个向量等长的第二个向量。

    Returns:
        余弦相似度；任一向量为零向量时返回 0。

    Raises:
        ValueError: 两个向量长度不一致。
    """
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class KnowledgeStore:
    """一个知识库的内存检索视图，带磁盘向量索引与 hash 失效重建。

    每次检索会比对磁盘文档 hash 以保持新鲜；文档文件缺失或不可读时
    fail-close 抛出 AdapterError，不回退到任何已加载的内存快照。
    """

    def __init__(
        self,
        *,
        base_name: str,
        docs_path: Path,
        retrieval: RetrievalConfig,
        embed: EmbedTexts | None = None,
        embedding_model: str | None = None,
        documents: tuple[KnowledgeDocument, ...] | None = None,
        content_hash: str | None = None,
    ) -> None:
        """绑定文档文件与检索策略；向量索引在首次检索时惰性构建。

        Args:
            base_name: 知识库名称，用于错误定位。
            docs_path: 知识库文档文件路径。
            retrieval: 检索策略、数量上限及混合检索权重。
            embed: 可选文本向量化函数。
            embedding_model: 索引缓存使用的向量模型标识。
            documents: 可选的已加载文档快照。
            content_hash: 已加载文档文件的内容摘要。

        Raises:
            ValueError: 向量或混合检索策略缺少向量化函数。
        """
        if retrieval.strategy is not RetrievalStrategy.BM25 and embed is None:
            raise ValueError(f"知识库 {base_name} 的 vector/hybrid 策略缺少 embed")
        self._base_name = base_name
        self._docs_path = docs_path
        self._retrieval = retrieval
        self._embed = embed
        self._embedding_model = embedding_model
        self._documents = documents or ()
        self._content_hash = content_hash or ""
        self._vectors: dict[str, list[float]] = {}
        self._bm25: BM25Okapi | None = None
        self._ready = False
        self._rebuild_lock = asyncio.Lock()

    @property
    def index_path(self) -> Path:
        """返回与 docs 文件同目录的隐藏向量索引文件路径。"""
        return self._docs_path.parent / f".{self._docs_path.stem}.index.json"

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[ScoredDocument]:
        """按配置策略检索，按得分降序排列，同分时按 doc_id 排序。

        ``top_k`` 缺省时使用检索配置的 top_k；显式传入时只影响本次调用。

        Args:
            query: 用户查询文本。
            top_k: 本次检索的返回数量上限。

        Returns:
            排序后的命中文档及得分；知识库为空时返回空列表。

        Raises:
            AdapterError: 文档缺失或不可读、索引重建或查询向量化失败。
        """
        limit = self._retrieval.top_k if top_k is None else top_k
        return await self._search(query, limit=limit)

    async def search_all(self, query: str) -> list[ScoredDocument]:
        """返回无 top_k 截断的全量排名，供平台检索诊断等调试入口使用。

        Args:
            query: 用户查询文本。

        Returns:
            全部文档及得分，排序规则与 search 相同。

        Raises:
            AdapterError: 文档缺失或不可读、索引重建或查询向量化失败。
        """
        return await self._search(query, limit=None)

    async def _search(
        self,
        query: str,
        *,
        limit: int | None,
    ) -> list[ScoredDocument]:
        await self._ensure_fresh()
        if not self._documents:
            return []
        if self._retrieval.strategy is RetrievalStrategy.BM25:
            return self._bm25_search(query, limit=limit)
        if self._retrieval.strategy is RetrievalStrategy.HYBRID:
            return await self._hybrid_search(query, limit=limit)
        return await self._vector_search(query, limit=limit)

    async def _ensure_fresh(self) -> None:
        try:
            _, file_hash = await asyncio.to_thread(load_documents, self._docs_path)
        except OSError as error:
            raise _documents_unreadable_error(self._base_name) from error
        if self._ready and file_hash == self._content_hash:
            return
        async with self._rebuild_lock:
            if self._ready and file_hash == self._content_hash:
                return
            try:
                documents, file_hash = await asyncio.to_thread(
                    load_documents, self._docs_path
                )
            except OSError as error:
                raise _documents_unreadable_error(self._base_name) from error
            await self._build_indexes(documents, file_hash)

    async def _build_indexes(
        self,
        documents: tuple[KnowledgeDocument, ...],
        content_hash: str,
    ) -> None:
        """用给定文档重建内存索引并标记就绪；调用方必须持有 rebuild 锁。"""
        self._documents = documents
        self._content_hash = content_hash
        self._bm25 = None
        self._vectors = {}
        self._ready = False
        if self._retrieval.strategy is not RetrievalStrategy.VECTOR:
            corpus = [_document_text(document) for document in documents]
            self._bm25 = (
                BM25Okapi([tokenize(text) for text in corpus]) if corpus else None
            )
        if self._retrieval.strategy is not RetrievalStrategy.BM25:
            self._vectors = await self._ensure_vectors(documents, content_hash)
        self._ready = True

    async def _ensure_vectors(
        self,
        documents: tuple[KnowledgeDocument, ...],
        content_hash: str,
    ) -> dict[str, list[float]]:
        """优先复用磁盘索引，hash 或模型变化时重新 embedding 并持久化。"""
        cached = await asyncio.to_thread(_load_index, self.index_path)
        if (
            cached is not None
            and cached.get("content_hash") == content_hash
            and cached.get("embedding_model") == self._embedding_model
            and isinstance(cached.get("vectors"), dict)
        ):
            cached_vectors = _parse_index_vectors(cached["vectors"], documents)
            if cached_vectors is not None:
                return cached_vectors
        if self._embed is None:
            raise AdapterError(
                operation="knowledge.index",
                code="knowledge_index_failed",
                message=f"知识库 {self._base_name} 缺少 embedding 能力",
            )
        texts = [_document_text(document) for document in documents]
        embedded = await self._embed(texts)
        vectors = {
            document.doc_id: vector
            for document, vector in zip(documents, embedded, strict=True)
        }
        payload: dict[str, Any] = {
            "content_hash": content_hash,
            "embedding_model": self._embedding_model,
            "vectors": vectors,
        }
        try:
            await asyncio.to_thread(_write_index, self.index_path, payload)
        except OSError as error:
            # 索引持久化失败不影响本次检索，下次检索会重新构建。
            LOGGER.warning(
                "knowledge_index_persist_failed base=%s error_type=%s",
                self._base_name,
                type(error).__name__,
            )
        return vectors

    async def _vector_search(
        self,
        query: str,
        *,
        limit: int | None,
    ) -> list[ScoredDocument]:
        if self._embed is None:
            raise AdapterError(
                operation="knowledge.search",
                code="knowledge_search_failed",
                message=f"知识库 {self._base_name} 缺少 embedding 能力",
            )
        query_vector = (await self._embed([query]))[0]
        scored = [
            ScoredDocument(
                document=document,
                score=cosine_similarity(
                    query_vector,
                    self._vectors.get(document.doc_id, []) or [0.0] * len(query_vector),
                ),
            )
            for document in self._documents
        ]
        return self._top_k(scored, limit)

    def _bm25_search(self, query: str, *, limit: int | None) -> list[ScoredDocument]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        scored = [
            ScoredDocument(document=document, score=float(scores[index]))
            for index, document in enumerate(self._documents)
        ]
        return self._top_k(scored, limit)

    async def _hybrid_search(
        self,
        query: str,
        *,
        limit: int | None,
    ) -> list[ScoredDocument]:
        vector_results = await self._vector_search_all(query)
        bm25_results = self._bm25_scores(query)
        weight = self._retrieval.hybrid_vector_weight
        vector_norm = _max_normalize(vector_results)
        bm25_norm = _max_normalize(bm25_results)
        scored = [
            ScoredDocument(
                document=document,
                score=(
                    weight * vector_norm.get(document.doc_id, 0.0)
                    + (1.0 - weight) * bm25_norm.get(document.doc_id, 0.0)
                ),
            )
            for document in self._documents
        ]
        return self._top_k(scored, limit)

    async def _vector_search_all(self, query: str) -> dict[str, float]:
        if self._embed is None:
            raise AdapterError(
                operation="knowledge.search",
                code="knowledge_search_failed",
                message=f"知识库 {self._base_name} 缺少 embedding 能力",
            )
        query_vector = (await self._embed([query]))[0]
        return {
            document.doc_id: cosine_similarity(
                query_vector,
                self._vectors.get(document.doc_id, []) or [0.0] * len(query_vector),
            )
            for document in self._documents
        }

    def _bm25_scores(self, query: str) -> dict[str, float]:
        if self._bm25 is None:
            return {}
        scores = self._bm25.get_scores(tokenize(query))
        return {
            document.doc_id: float(scores[index])
            for index, document in enumerate(self._documents)
        }

    def _top_k(
        self,
        scored: list[ScoredDocument],
        limit: int | None,
    ) -> list[ScoredDocument]:
        ordered = sorted(
            scored,
            key=lambda item: (-item.score, item.document.doc_id),
        )
        return ordered if limit is None else ordered[:limit]


def _document_text(document: KnowledgeDocument) -> str:
    return f"{document.title}\n{document.content}"


def _documents_unreadable_error(base_name: str) -> AdapterError:
    """归一化文档文件缺失或不可读的 fail-close 错误，原始 OSError 只作 cause。"""
    return AdapterError(
        operation="knowledge.search",
        code="knowledge_search_failed",
        message=f"知识库 {base_name} 文档缺失或不可读",
    )


def _max_normalize(scores: dict[str, float]) -> dict[str, float]:
    peak = max(scores.values(), default=0.0)
    if peak <= 0.0:
        return {doc_id: 0.0 for doc_id in scores}
    return {doc_id: score / peak for doc_id, score in scores.items()}


def _load_index(index_path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.warning("knowledge_index_unreadable path=%s", index_path)
        return None
    return raw if isinstance(raw, dict) else None


def _parse_index_vectors(
    raw_vectors: dict[Any, Any],
    documents: tuple[KnowledgeDocument, ...],
) -> dict[str, list[float]] | None:
    expected_ids = {document.doc_id for document in documents}
    vectors: dict[str, list[float]] = {}
    for doc_id, vector in raw_vectors.items():
        if (
            not isinstance(doc_id, str)
            or not isinstance(vector, list)
            or not all(isinstance(component, (int, float)) for component in vector)
        ):
            return None
        vectors[doc_id] = [float(component) for component in vector]
    if set(vectors) != expected_ids:
        return None
    return vectors


def _write_index(index_path: Path, payload: dict[str, Any]) -> None:
    temporary = index_path.with_name(f"{index_path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, index_path)
