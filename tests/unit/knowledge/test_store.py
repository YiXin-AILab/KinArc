"""知识库存储的索引构建、失效重建与多策略检索测试。"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.knowledge.documents import KnowledgeDocument, load_documents
from agentsystem.knowledge.embeddings import LiteLLMEmbedder
from agentsystem.knowledge.models import EmbeddingConfig, RetrievalConfig
from agentsystem.knowledge.store import KnowledgeStore, cosine_similarity, tokenize


def _write_docs(path: Path, items: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _fake_vector(text: str) -> list[float]:
    """确定性 hash 向量：共享 token 越多余弦相似度越高。"""
    vector = [0.0] * 512
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:2]) % 512] += 1.0
    return vector


async def _fake_embed(texts: Sequence[str]) -> list[list[float]]:
    return [_fake_vector(text) for text in texts]


def _vector_store(docs_path: Path) -> KnowledgeStore:
    config = RetrievalConfig.model_validate(
        {
            "strategy": "vector",
            "top_k": 8,
            "embedding": {"model": "fake/v1"},
        }
    )
    return KnowledgeStore(
        base_name="pricing",
        docs_path=docs_path,
        retrieval=config,
        embed=_fake_embed,
        embedding_model="fake/v1",
    )


_DOCS = [
    {"doc_id": "fee", "title": "工时费标准", "content": "工时费按小时收取标准费用"},
    {"doc_id": "account", "title": "开户流程", "content": "开户需要身份证明与手机号"},
    {"doc_id": "promo", "title": "促销活动", "content": "夏季促销活动现已正式开启"},
]


async def test_vector_search_ranks_and_persists_index(tmp_path: Path) -> None:
    """向量检索按余弦相似度排序，并把索引持久化到 docs 旁。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    embed_calls = 0

    async def counting_embed(texts: Sequence[str]) -> list[list[float]]:
        nonlocal embed_calls
        embed_calls += 1
        return [_fake_vector(text) for text in texts]

    store = _vector_store(docs_path)
    store._embed = counting_embed

    results = await store.search("工时费收费标准")
    await store.search("工时费收费标准")

    assert [item.document.doc_id for item in results] == ["fee", "account", "promo"]
    assert results[0].score > results[1].score
    assert store.index_path.name == ".kb.index.json"
    assert store.index_path.is_file()
    # 首次检索 embedding 文档与查询各一次；第二次检索只 embedding 查询。
    assert embed_calls == 3


async def test_disk_index_reused_by_new_store_instance(tmp_path: Path) -> None:
    """新 Store 实例命中同 hash 磁盘索引时不再调用文档 embedding。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    first = _vector_store(docs_path)
    await first.search("工时费")

    embedded_texts: list[int] = []

    async def spy_embed(texts: Sequence[str]) -> list[list[float]]:
        embedded_texts.append(len(texts))
        return [_fake_vector(text) for text in texts]

    second = _vector_store(docs_path)
    second._embed = spy_embed
    results = await second.search("工时费")

    assert [item.document.doc_id for item in results][0] == "fee"
    # 只有查询文本参与本次 embedding，文档向量全部来自磁盘索引。
    assert embedded_texts == [1]


async def test_docs_hash_change_triggers_rebuild(tmp_path: Path) -> None:
    """docs 内容变化后自动重建索引，检索结果反映新文档。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    store = _vector_store(docs_path)
    first = await store.search("工时费")
    assert first[0].document.doc_id == "fee"

    _write_docs(
        docs_path,
        [{"doc_id": "new", "title": "工时费新规", "content": "工时费最新收费标准"}],
    )

    second = await store.search("工时费收费标准")

    assert [item.document.doc_id for item in second] == ["new"]
    persisted = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert set(persisted["vectors"]) == {"new"}


async def test_concurrent_searches_share_single_rebuild(tmp_path: Path) -> None:
    """并发检索等待同一次重建完成，文档 embedding 只执行一次。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    started = asyncio.Event()
    release = asyncio.Event()
    build_calls = 0

    async def gated_embed(texts: Sequence[str]) -> list[list[float]]:
        nonlocal build_calls
        if len(texts) > 1:
            build_calls += 1
            started.set()
            await release.wait()
        return [_fake_vector(text) for text in texts]

    store = _vector_store(docs_path)
    store._embed = gated_embed

    first = asyncio.create_task(store.search("工时费"))
    await started.wait()
    second = asyncio.create_task(store.search("开户流程"))
    await asyncio.sleep(0)
    release.set()
    first_results, second_results = await asyncio.gather(first, second)

    assert build_calls == 1
    assert first_results[0].document.doc_id == "fee"
    assert second_results[0].document.doc_id == "account"


async def test_embedding_failure_surfaces_adapter_error(tmp_path: Path) -> None:
    """索引构建期的 embedding 失败不静默降级，经 Embedder 收敛为 AdapterError。"""

    async def failing_embed(**kwargs: object) -> None:
        raise RuntimeError("provider down")

    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    embedder = LiteLLMEmbedder(
        EmbeddingConfig.model_validate({"model": "fake/v1"}),
        embedding=failing_embed,
    )
    store = _vector_store(docs_path)
    store._embed = embedder.embed_texts

    with pytest.raises(AdapterError):
        await store.search("工时费")


async def test_bm25_strategy_ranks_chinese_text_without_embedding(
    tmp_path: Path,
) -> None:
    """BM25 策略用字符 bigram 检索中文，完全不触碰 embedding。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    config = RetrievalConfig.model_validate({"strategy": "bm25", "top_k": 8})
    store = KnowledgeStore(
        base_name="pricing",
        docs_path=docs_path,
        retrieval=config,
    )

    results = await store.search("工时费如何收取")

    assert results[0].document.doc_id == "fee"
    assert results[0].score > 0
    assert not store.index_path.exists()


def _bm25_store(
    docs_path: Path,
    *,
    documents: tuple[KnowledgeDocument, ...] | None = None,
    content_hash: str | None = None,
) -> KnowledgeStore:
    config = RetrievalConfig.model_validate({"strategy": "bm25", "top_k": 8})
    return KnowledgeStore(
        base_name="pricing",
        docs_path=docs_path,
        retrieval=config,
        documents=documents,
        content_hash=content_hash,
    )


async def test_docs_deleted_after_preload_raises_adapter_error(
    tmp_path: Path,
) -> None:
    """启动预加载后文档被删除，检索 fail-close 抛 AdapterError 而非服务旧快照。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    documents, content_hash = load_documents(docs_path)
    store = _bm25_store(
        docs_path,
        documents=documents,
        content_hash=content_hash,
    )
    docs_path.unlink()

    with pytest.raises(AdapterError) as exc_info:
        await store.search("工时费如何收取")

    assert exc_info.value.code == "knowledge_search_failed"


async def test_docs_deleted_after_first_search_raises_adapter_error(
    tmp_path: Path,
) -> None:
    """首次检索成功后删除 documents.json，下一次检索必须失败且不外泄 OSError。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    store = _bm25_store(docs_path)
    assert (await store.search("工时费"))[0].document.doc_id == "fee"
    docs_path.unlink()

    with pytest.raises(AdapterError) as exc_info:
        await store.search("开户流程")

    assert exc_info.value.code == "knowledge_search_failed"
    assert isinstance(exc_info.value.__cause__, OSError)


async def test_docs_unreadable_without_snapshot_raises_adapter_error(
    tmp_path: Path,
) -> None:
    """从未成功加载文档且文件不可读时，收敛为 AdapterError 而非 OSError。"""
    docs_path = tmp_path / "missing.json"
    store = _bm25_store(docs_path)

    with pytest.raises(AdapterError) as exc_info:
        await store.search("工时费")

    assert exc_info.value.code == "knowledge_search_failed"


async def test_docs_restored_after_deletion_recovers_freshness(
    tmp_path: Path,
) -> None:
    """文档删除期间检索失败，恢复后可读且内容变化仍会触发重建。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    store = _bm25_store(docs_path)
    assert (await store.search("工时费"))[0].document.doc_id == "fee"

    docs_path.unlink()
    with pytest.raises(AdapterError):
        await store.search("工时费")

    _write_docs(
        docs_path,
        [{"doc_id": "new", "title": "工时费新规", "content": "工时费最新收费标准"}],
    )

    results = await store.search("工时费收费标准")

    assert [item.document.doc_id for item in results] == ["new"]


async def test_hybrid_weights_interpolate_between_strategies(tmp_path: Path) -> None:
    """weight=1 退化为纯向量排序，weight=0 退化为纯 BM25 排序。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)

    def hybrid_store(weight: float) -> KnowledgeStore:
        config = RetrievalConfig.model_validate(
            {
                "strategy": "hybrid",
                "top_k": 8,
                "hybrid_vector_weight": weight,
                "embedding": {"model": "fake/v1"},
            }
        )
        return KnowledgeStore(
            base_name="pricing",
            docs_path=docs_path,
            retrieval=config,
            embed=_fake_embed,
            embedding_model="fake/v1",
        )

    query = "工时费"
    vector_order = [
        item.document.doc_id for item in await hybrid_store(1.0).search(query)
    ]
    bm25_order = [
        item.document.doc_id for item in await hybrid_store(0.0).search(query)
    ]
    mixed = await hybrid_store(0.5).search(query)

    pure_vector = await _vector_store(docs_path).search(query)
    assert vector_order == [item.document.doc_id for item in pure_vector]
    assert bm25_order[0] == "fee"
    assert mixed[0].document.doc_id == "fee"


async def test_empty_knowledge_base_returns_no_results(tmp_path: Path) -> None:
    """空知识库检索返回空列表而不是报错。"""
    docs_path = _write_docs(tmp_path / "kb.json", [])
    store = _vector_store(docs_path)

    assert await store.search("任意问题") == []


async def test_corrupt_index_file_triggers_rebuild(tmp_path: Path) -> None:
    """损坏的索引文件被忽略并重建，不阻断检索。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    store = _vector_store(docs_path)
    store.index_path.write_text("not json", encoding="utf-8")

    results = await store.search("工时费")

    assert results[0].document.doc_id == "fee"
    json.loads(store.index_path.read_text(encoding="utf-8"))


def test_cosine_similarity_edge_cases() -> None:
    """零向量返回 0，相同向量返回 1。"""
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


async def test_search_top_k_override_only_affects_single_call(
    tmp_path: Path,
) -> None:
    """top_k 覆盖只影响本次调用，缺省调用仍使用配置值。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    config = RetrievalConfig.model_validate({"strategy": "bm25", "top_k": 1})
    store = KnowledgeStore(
        base_name="pricing",
        docs_path=docs_path,
        retrieval=config,
    )

    assert len(await store.search("工时费")) == 1
    assert len(await store.search("工时费", top_k=3)) == 3
    assert len(await store.search("工时费")) == 1


async def test_search_all_returns_untruncated_ranking(tmp_path: Path) -> None:
    """search_all 返回无 top_k 截断的全量排名。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    config = RetrievalConfig.model_validate({"strategy": "bm25", "top_k": 1})
    store = KnowledgeStore(
        base_name="pricing",
        docs_path=docs_path,
        retrieval=config,
    )

    results = await store.search_all("工时费")

    assert len(results) == len(_DOCS)
    assert results[0].document.doc_id == "fee"
