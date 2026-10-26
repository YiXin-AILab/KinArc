"""知识库平台资源与 Plugin 的严格配置模型。"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, StringConstraints, model_validator

from agentsystem.common.models import StrictModel

type KnowledgeBaseName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
"""知识库名，同时作为 kb_search Tool 的 kb 参数枚举值。"""

type ModelName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class RetrievalStrategy(StrEnum):
    """知识库检索策略。"""

    VECTOR = "vector"
    BM25 = "bm25"
    HYBRID = "hybrid"


class RelevanceLevel(StrEnum):
    """相关性裁判的三级判定结果，按严格程度排序。"""

    RELEVANT = "relevant"
    PARTIAL = "partial"
    IRRELEVANT = "irrelevant"


_RELEVANCE_RANK = {
    RelevanceLevel.RELEVANT: 2,
    RelevanceLevel.PARTIAL: 1,
    RelevanceLevel.IRRELEVANT: 0,
}


def relevance_passes(level: RelevanceLevel, minimum: RelevanceLevel) -> bool:
    """判定给定相关性级别是否达到放行阈值。

    Args:
        level: 待判断的相关性级别。
        minimum: 允许通过的最低相关性级别。

    Returns:
        是否达到指定的相关性阈值。
    """
    return _RELEVANCE_RANK[level] >= _RELEVANCE_RANK[minimum]


class EmbeddingConfig(StrictModel):
    """LiteLLM embedding 调用配置。"""

    model: ModelName
    api_key: SecretStr | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)


class RetrievalConfig(StrictModel):
    """单知识库的检索策略与候选数量配置。"""

    strategy: RetrievalStrategy = RetrievalStrategy.VECTOR
    top_k: int = Field(default=8, ge=1, le=100)
    embedding: EmbeddingConfig | None = None
    hybrid_vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_embedding_required(self) -> Self:
        """vector 与 hybrid 策略必须提供 embedding 配置。"""
        if self.strategy is not RetrievalStrategy.BM25 and self.embedding is None:
            raise ValueError("vector/hybrid 检索策略必须配置 embedding")
        return self


class RerankConfig(StrictModel):
    """可选的 LiteLLM rerank 重排序配置。"""

    model: ModelName
    api_key: SecretStr | None = None
    base_url: str | None = None
    top_n: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)


class RelevanceGateConfig(StrictModel):
    """可选的小模型相关性裁判配置。"""

    model: ModelName
    api_key: SecretStr | None = None
    base_url: str | None = None
    min_relevance: RelevanceLevel = RelevanceLevel.PARTIAL
    timeout_seconds: float = Field(default=20.0, gt=0)


class KnowledgeBaseConfig(StrictModel):
    """单知识库声明；docs_path 为 Application 根目录相对路径或绝对路径。"""

    name: KnowledgeBaseName
    description: str = ""
    docs_path: str = Field(min_length=1)
    retrieval: RetrievalConfig
    rerank: RerankConfig | None = None
    relevance_gate: RelevanceGateConfig | None = None


class KnowledgeBaseManifest(StrictModel):
    """平台知识库 kb.yaml 清单；name 必须与所在目录名一致。

    文档固定位于同目录 documents.json，因此清单不携带 docs_path。
    """

    name: KnowledgeBaseName
    description: str = ""
    retrieval: RetrievalConfig
    rerank: RerankConfig | None = None
    relevance_gate: RelevanceGateConfig | None = None


class RelevanceGateDisabled(StrictModel):
    """引用级 relevance_gate 覆盖：显式关闭库默认的相关性裁判。"""

    enabled: Literal[False]


type RelevanceGateOverride = RelevanceGateConfig | RelevanceGateDisabled
"""引用级 relevance_gate 覆盖：整体替换配置，或 enabled=false 关闭。"""


class KnowledgeBaseRef(StrictModel):
    """AgentConfig 中按名引用平台知识库，可整体覆盖检索与裁判配置。

    retrieval 与 relevance_gate 缺省时跟随库默认；relevance_gate 也接受
    ``{enabled: false}`` 显式关闭库默认裁判。
    """

    name: KnowledgeBaseName
    retrieval: RetrievalConfig | None = None
    relevance_gate: RelevanceGateOverride | None = None


class KnowledgePluginOptions(StrictModel):
    """kb_search Plugin 的完整 plugin_options 载荷。"""

    bases: tuple[KnowledgeBaseConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_names(self) -> Self:
        """知识库名在 Tool 枚举中必须唯一。"""
        names = tuple(base.name for base in self.bases)
        if len(names) != len(set(names)):
            raise ValueError("知识库 name 重复")
        return self
