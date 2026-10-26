"""检索 → 重排 → 裁判管线的端到端串联与输出格式测试。"""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentsystem.knowledge.gate import RelevanceGate
from agentsystem.knowledge.models import (
    RelevanceGateConfig,
    RelevanceLevel,
    RerankConfig,
    RetrievalConfig,
)
from agentsystem.knowledge.pipeline import KnowledgePipeline, format_tool_output
from agentsystem.knowledge.rerank import LiteLLMReranker
from agentsystem.knowledge.store import KnowledgeStore, tokenize


def _write_docs(path: Path, items: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


_DOCS = [
    {"doc_id": "fee", "title": "工时费标准", "content": "工时费按小时收取标准费用"},
    {"doc_id": "account", "title": "开户流程", "content": "开户需要身份证明与手机号"},
]


def _fake_vector(text: str) -> list[float]:
    vector = [0.0] * 512
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:2]) % 512] += 1.0
    return vector


async def _fake_embed(texts: Sequence[str]) -> list[list[float]]:
    return [_fake_vector(text) for text in texts]


def _store(docs_path: Path) -> KnowledgeStore:
    return KnowledgeStore(
        base_name="pricing",
        docs_path=docs_path,
        retrieval=RetrievalConfig.model_validate(
            {
                "strategy": "vector",
                "top_k": 8,
                "embedding": {"model": "fake/v1"},
            }
        ),
        embed=_fake_embed,
        embedding_model="fake/v1",
    )


def _gate(verdicts: dict[str, str]) -> RelevanceGate:
    """按标题关键词映射判定结果；verdicts 的 key 是 doc_id。"""
    titles = {"fee": "工时费标准", "account": "开户流程"}

    async def fake_completion(**kwargs: Any) -> dict[str, object]:
        user = kwargs["messages"][1]["content"]
        for doc_id, relevance in verdicts.items():
            if f"文档标题：{titles[doc_id]}" in user:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "relevance": relevance,
                                        "reason": f"{doc_id} 判定",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
        raise AssertionError("未匹配的裁判请求")

    return RelevanceGate(
        RelevanceGateConfig.model_validate({"model": "openai/small"}),
        completion=fake_completion,
    )


async def test_pipeline_chains_retrieval_rerank_and_gate(tmp_path: Path) -> None:
    """rerank 结果替换候选顺序与得分，gate metadata 记录逐条判定。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)

    async def fake_rerank(**kwargs: Any) -> dict[str, object]:
        return {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.8},
            ]
        }

    reranker = LiteLLMReranker(
        RerankConfig.model_validate({"model": "jina_ai/jina-reranker-v2"}),
        rerank=fake_rerank,
    )
    gate = _gate({"fee": "relevant", "account": "partial"})
    pipeline = KnowledgePipeline(
        store=_store(docs_path),
        reranker=reranker,
        gate=gate,
        min_relevance=RelevanceLevel.PARTIAL,
    )

    output = format_tool_output(await pipeline.search("工时费"))

    # 检索第一名是 fee，rerank 反转顺序后 account 在前。
    assert output.metadata["retrieved_doc_ids"] == ["account", "fee"]
    assert output.metadata["scores"] == {"account": 0.9, "fee": 0.8}
    gate_meta = output.metadata["gate"]
    assert isinstance(gate_meta, dict)
    assert gate_meta["account"] == {"relevance": "partial", "reason": "account 判定"}
    assert output.metadata["no_relevant_docs"] is False
    assert output.content.startswith("## [account] 开户流程")
    assert "## [fee] 工时费标准" in output.content


async def test_pipeline_gate_filters_below_threshold(tmp_path: Path) -> None:
    """低于 min_relevance 的候选被剔除，min_relevance=relevant 时 partial 也剔除。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    gate = _gate({"fee": "partial", "account": "irrelevant"})

    partial_pipeline = KnowledgePipeline(
        store=_store(docs_path),
        gate=gate,
        min_relevance=RelevanceLevel.PARTIAL,
    )
    strict_pipeline = KnowledgePipeline(
        store=_store(docs_path),
        gate=gate,
        min_relevance=RelevanceLevel.RELEVANT,
    )

    partial_result = await partial_pipeline.search("工时费")
    strict_result = await strict_pipeline.search("工时费")

    assert [document.doc_id for document in partial_result.documents] == ["fee"]
    assert strict_result.documents == ()
    assert strict_result.no_relevant_docs is True


async def test_pipeline_all_filtered_returns_no_relevant_docs(tmp_path: Path) -> None:
    """全部剔除后返回结构化拒答指引，metadata 标注 no_relevant_docs。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    pipeline = KnowledgePipeline(
        store=_store(docs_path),
        gate=_gate({"fee": "irrelevant", "account": "irrelevant"}),
        min_relevance=RelevanceLevel.PARTIAL,
    )

    output = format_tool_output(await pipeline.search("完全无关的问题"))

    assert output.metadata["no_relevant_docs"] is True
    assert output.metadata["retrieved_doc_ids"] == []
    assert "未找到与问题相关的内容" in output.content
    assert "转人工" in output.content


async def test_pipeline_without_optional_stages_returns_vector_scores(
    tmp_path: Path,
) -> None:
    """缺省不启用 rerank/gate 时直接返回向量检索结果，无 gate metadata。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    pipeline = KnowledgePipeline(store=_store(docs_path))

    output = format_tool_output(await pipeline.search("工时费收费标准"))

    assert output.metadata["retrieved_doc_ids"] == ["fee", "account"]
    assert "gate" not in output.metadata
    assert output.metadata["no_relevant_docs"] is False


async def test_pipeline_candidates_include_gate_filtered(tmp_path: Path) -> None:
    """candidates 保留裁判前的完整候选，供调试入口还原被剔除文档。"""
    docs_path = _write_docs(tmp_path / "kb.json", _DOCS)
    pipeline = KnowledgePipeline(
        store=_store(docs_path),
        gate=_gate({"fee": "relevant", "account": "irrelevant"}),
        min_relevance=RelevanceLevel.PARTIAL,
    )

    result = await pipeline.search("工时费")

    assert [document.doc_id for document in result.documents] == ["fee"]
    assert [candidate.document.doc_id for candidate in result.candidates] == [
        "fee",
        "account",
    ]
