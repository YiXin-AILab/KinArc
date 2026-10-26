"""串联知识库检索、可选重排序和相关性裁判，并构造工具输出。"""

from collections.abc import Mapping
from dataclasses import dataclass

from agentsystem.common.models import JsonValue
from agentsystem.knowledge.documents import KnowledgeDocument
from agentsystem.knowledge.gate import GateVerdict, RelevanceGate
from agentsystem.knowledge.models import RelevanceLevel, relevance_passes
from agentsystem.knowledge.rerank import LiteLLMReranker
from agentsystem.knowledge.store import KnowledgeStore, ScoredDocument
from agentsystem.tool.models import ToolOutput

_NO_RELEVANT_CONTENT = (
    "知识库中未找到与问题相关的内容。"
    "请明确告知用户无法根据现有资料回答该问题，建议用户补充信息或转人工处理，"
    "不要编造答案。"
)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """一次知识库检索的最终结果与评测可消费的稳定字段。

    ``candidates`` 为检索/重排之后、相关性裁判之前的完整候选（含得分），
    供平台检索测试等调试入口还原被裁判剔除的文档；未启用裁判时与最终
    结果一致。
    """

    documents: tuple[KnowledgeDocument, ...]
    scores: Mapping[str, float]
    gate: Mapping[str, JsonValue]
    no_relevant_docs: bool
    candidates: tuple[ScoredDocument, ...] = ()


class KnowledgePipeline:
    """串联单知识库的检索、可选 rerank 与可选相关性裁判。"""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        reranker: LiteLLMReranker | None = None,
        gate: RelevanceGate | None = None,
        min_relevance: RelevanceLevel = RelevanceLevel.PARTIAL,
    ) -> None:
        """绑定管线各阶段；reranker 与 gate 缺省时跳过对应阶段。

        Args:
            store: 知识库检索存储。
            reranker: 可选重排序器。
            gate: 可选相关性裁判。
            min_relevance: 裁判结果允许通过的最低相关性级别。
        """
        self._store = store
        self._reranker = reranker
        self._gate = gate
        self._min_relevance = min_relevance

    async def search(self, query: str) -> PipelineResult:
        """执行完整检索管线并返回最终候选。

        Args:
            query: 用户查询文本。

        Returns:
            最终文档、得分与裁判信息，以及裁判过滤前的候选快照。

        Raises:
            AdapterError: 检索或重排的外部调用失败；裁判失败已内部降级。
        """
        candidates = await self._store.search(query)
        if self._reranker is not None and candidates:
            candidates = await self._rerank(query, candidates)
        pre_gate_candidates = tuple(candidates)
        verdicts: list[GateVerdict] = []
        if self._gate is not None and candidates:
            verdicts = await self._gate.judge(
                query, [candidate.document for candidate in candidates]
            )
            passed = {
                verdict.doc_id
                for verdict in verdicts
                if relevance_passes(verdict.relevance, self._min_relevance)
            }
            candidates = [
                candidate
                for candidate in candidates
                if candidate.document.doc_id in passed
            ]
        documents = tuple(candidate.document for candidate in candidates)
        return PipelineResult(
            documents=documents,
            scores={
                candidate.document.doc_id: candidate.score for candidate in candidates
            },
            gate=_gate_metadata(verdicts),
            no_relevant_docs=not documents,
            candidates=pre_gate_candidates,
        )

    async def _rerank(
        self,
        query: str,
        candidates: list[ScoredDocument],
    ) -> list[ScoredDocument]:
        if self._reranker is None:
            return candidates
        results = await self._reranker.rerank(
            query,
            [
                f"{candidate.document.title}\n{candidate.document.content}"
                for candidate in candidates
            ],
        )
        return [
            ScoredDocument(
                document=candidates[result.index].document,
                score=result.score,
            )
            for result in results
        ]


def format_tool_output(result: PipelineResult) -> ToolOutput:
    """将检索结果转换为模型可见正文和结构化元数据。

    Args:
        result: 检索管线的最终结果。

    Returns:
        工具输出；无相关文档时返回无匹配说明，元数据保留检索事实。
    """
    metadata: dict[str, JsonValue] = {
        "retrieved_doc_ids": [document.doc_id for document in result.documents],
        "scores": dict(result.scores),
        "no_relevant_docs": result.no_relevant_docs,
    }
    if result.gate:
        metadata["gate"] = dict(result.gate)
    if result.no_relevant_docs:
        return ToolOutput(content=_NO_RELEVANT_CONTENT, metadata=metadata)
    content = "\n\n".join(
        f"## [{document.doc_id}] {document.title}\n{document.content}"
        for document in result.documents
    )
    return ToolOutput(content=content, metadata=metadata)


def _gate_metadata(verdicts: list[GateVerdict]) -> dict[str, JsonValue]:
    metadata: dict[str, JsonValue] = {}
    for verdict in verdicts:
        entry: dict[str, JsonValue] = {
            "relevance": verdict.relevance.value,
            "reason": verdict.reason,
        }
        if verdict.gate_error:
            entry["gate_error"] = True
        metadata[verdict.doc_id] = entry
    return metadata
