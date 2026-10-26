"""知识库配置模型的严格校验测试。"""

import pytest
from agentsystem.knowledge.models import (
    KnowledgeBaseManifest,
    KnowledgeBaseRef,
    KnowledgePluginOptions,
    RelevanceGateConfig,
    RelevanceGateDisabled,
    RelevanceLevel,
    RetrievalConfig,
    relevance_passes,
)
from pydantic import ValidationError


def _base() -> dict[str, object]:
    return {
        "name": "pricing",
        "description": "价格/工时费咨询",
        "docs_path": "data/kb/pricing.json",
        "retrieval": {
            "strategy": "vector",
            "top_k": 8,
            "embedding": {"model": "jina_ai/jina-embeddings-v3"},
        },
    }


def test_full_options_parse_with_optional_stages() -> None:
    """完整配置（含 rerank 与 gate）可以解析为严格模型。"""
    raw = _base()
    raw["rerank"] = {"model": "jina_ai/jina-reranker-v2", "top_n": 4}
    raw["relevance_gate"] = {"model": "openai/small", "min_relevance": "relevant"}

    options = KnowledgePluginOptions.model_validate({"bases": [raw]})

    base = options.bases[0]
    assert base.name == "pricing"
    assert base.retrieval.top_k == 8
    assert base.rerank is not None and base.rerank.top_n == 4
    assert base.relevance_gate is not None
    assert base.relevance_gate.min_relevance is RelevanceLevel.RELEVANT


def test_unknown_fields_are_rejected() -> None:
    """严格模型拒绝任何未知字段，防止配置拼写错误静默生效。"""
    raw = _base()
    raw["unknown_field"] = True

    with pytest.raises(ValidationError):
        KnowledgePluginOptions.model_validate({"bases": [raw]})


def test_vector_strategy_requires_embedding() -> None:
    """vector/hybrid 策略缺少 embedding 配置时启动期失败。"""
    with pytest.raises(ValidationError, match="embedding"):
        RetrievalConfig.model_validate({"strategy": "vector"})
    with pytest.raises(ValidationError, match="embedding"):
        RetrievalConfig.model_validate({"strategy": "hybrid"})


def test_bm25_strategy_allows_missing_embedding() -> None:
    """纯 BM25 策略不需要任何 embedding 配置。"""
    config = RetrievalConfig.model_validate({"strategy": "bm25", "top_k": 3})

    assert config.embedding is None
    assert config.top_k == 3


def test_duplicate_base_names_are_rejected() -> None:
    """知识库名是 Tool 枚举值，重复必须拒绝。"""
    with pytest.raises(ValidationError, match="重复"):
        KnowledgePluginOptions.model_validate({"bases": [_base(), _base()]})


def test_empty_bases_and_invalid_values_are_rejected() -> None:
    """空 bases、非法阈值与非法 top_k 都在启动期失败。"""
    with pytest.raises(ValidationError):
        KnowledgePluginOptions.model_validate({"bases": []})
    raw = _base()
    raw["relevance_gate"] = {"model": "openai/small", "min_relevance": "unknown"}
    with pytest.raises(ValidationError):
        KnowledgePluginOptions.model_validate({"bases": [raw]})
    raw = _base()
    retrieval = raw["retrieval"]
    assert isinstance(retrieval, dict)
    retrieval["top_k"] = 0
    with pytest.raises(ValidationError):
        KnowledgePluginOptions.model_validate({"bases": [raw]})


def test_relevance_threshold_ordering() -> None:
    """放行阈值按 relevant > partial > irrelevant 严格排序。"""
    assert relevance_passes(RelevanceLevel.RELEVANT, RelevanceLevel.RELEVANT)
    assert relevance_passes(RelevanceLevel.PARTIAL, RelevanceLevel.PARTIAL)
    assert not relevance_passes(RelevanceLevel.PARTIAL, RelevanceLevel.RELEVANT)
    assert not relevance_passes(RelevanceLevel.IRRELEVANT, RelevanceLevel.PARTIAL)


def test_manifest_parses_without_docs_path() -> None:
    """kb.yaml 清单不含 docs_path，未知字段一律拒绝。"""
    manifest = KnowledgeBaseManifest.model_validate(
        {
            "name": "pricing",
            "description": "价格库",
            "retrieval": {"strategy": "bm25"},
            "relevance_gate": {"model": "openai/small"},
        }
    )

    assert manifest.name == "pricing"
    assert manifest.rerank is None
    with pytest.raises(ValidationError):
        KnowledgeBaseManifest.model_validate(
            {
                "name": "pricing",
                "docs_path": "data/kb.json",
                "retrieval": {"strategy": "bm25"},
            }
        )


def test_ref_gate_override_accepts_replace_or_disable() -> None:
    """引用级 relevance_gate 接受完整配置替换或 enabled=false 关闭。"""
    replaced = KnowledgeBaseRef.model_validate(
        {"name": "pricing", "relevance_gate": {"model": "openai/small"}}
    )
    assert isinstance(replaced.relevance_gate, RelevanceGateConfig)

    disabled = KnowledgeBaseRef.model_validate(
        {"name": "pricing", "relevance_gate": {"enabled": False}}
    )
    assert isinstance(disabled.relevance_gate, RelevanceGateDisabled)

    follow = KnowledgeBaseRef.model_validate({"name": "pricing"})
    assert follow.retrieval is None
    assert follow.relevance_gate is None


def test_ref_rejects_unknown_and_incomplete_gate_fields() -> None:
    """引用模型严格拒绝未知字段与残缺 gate 配置。"""
    with pytest.raises(ValidationError):
        KnowledgeBaseRef.model_validate({"name": "pricing", "unknown": 1})
    with pytest.raises(ValidationError):
        KnowledgeBaseRef.model_validate(
            {"name": "pricing", "relevance_gate": {"enabled": True}}
        )
