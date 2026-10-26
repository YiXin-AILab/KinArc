"""按 Agent plugin_options 注册 kb_search Tool 的内置知识库 Plugin。"""

from pathlib import Path

from pydantic import ValidationError

from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.knowledge.documents import load_documents, resolve_docs_path
from agentsystem.knowledge.embeddings import LiteLLMEmbedder
from agentsystem.knowledge.gate import RelevanceGate
from agentsystem.knowledge.models import (
    KnowledgeBaseConfig,
    KnowledgePluginOptions,
    RelevanceLevel,
)
from agentsystem.knowledge.pipeline import KnowledgePipeline, format_tool_output
from agentsystem.knowledge.rerank import LiteLLMReranker
from agentsystem.knowledge.store import KnowledgeStore
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.service.agents import ApplicationRoot
from agentsystem.tool.models import ToolOutput

TOOL_NAME = "kb_search"

_TOOL_RULES = (
    "当用户问题可能由知识库覆盖时必须先检索再回答；"
    "检索结果标注为无相关内容时不得编造答案，应告知用户并建议转人工。"
)


def register(context: PluginSetupContext) -> None:
    """按配置构建各知识库管线并注册唯一 kb_search Tool；无 bases 时不注册。"""
    raw_options = context.options(context.plugin_id)
    if "bases" not in raw_options:
        return
    try:
        options = KnowledgePluginOptions.model_validate(raw_options)
    except ValidationError as error:
        # 只保留字段路径与错误类型，避免错误消息回显 api_key 等配置值。
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        raise ValueError(
            f"Plugin {context.plugin_id} 知识库配置无效：{details}"
        ) from error
    root_capability = context.get(ApplicationRoot)
    application_root = root_capability.path if root_capability is not None else None
    pipelines = {
        base.name: _build_pipeline(context, base, application_root)
        for base in options.bases
    }

    async def kb_search(query: str, kb: str) -> ToolOutput:
        """在指定知识库中检索与问题相关的文档。"""
        pipeline = pipelines.get(kb)
        if pipeline is None:
            raise ToolError(
                code="knowledge_base_not_found",
                message=f"知识库不存在：{kb}",
            )
        normalized = query.strip()
        if not normalized:
            raise ToolError(
                code="invalid_kb_search_arguments",
                message="query 不能为空",
            )
        try:
            result = await pipeline.search(normalized)
        except AdapterError as error:
            raise ToolError(
                code="knowledge_search_failed",
                message=f"知识库检索失败：{error.message}",
            ) from error
        return format_tool_output(result)

    context.register_tool(
        kb_search,
        name=TOOL_NAME,
        description=_tool_description(options),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "检索问题，应包含用户的核心诉求与关键实体。",
                },
                "kb": {
                    "type": "string",
                    "enum": [base.name for base in options.bases],
                    "description": "目标知识库名称。",
                },
            },
            "required": ["query", "kb"],
            "additionalProperties": False,
        },
        rules=_TOOL_RULES,
    )


def _build_pipeline(
    context: PluginSetupContext,
    base: KnowledgeBaseConfig,
    application_root: Path | None,
) -> KnowledgePipeline:
    """解析路径、预加载文档并按配置组装单知识库管线。"""
    docs_path = resolve_docs_path(
        application_root=application_root,
        docs_path=base.docs_path,
        plugin_id=context.plugin_id,
        base_name=base.name,
    )
    documents, content_hash = load_documents(docs_path)
    embedder = (
        LiteLLMEmbedder(base.retrieval.embedding)
        if base.retrieval.embedding is not None
        else None
    )
    store = KnowledgeStore(
        base_name=base.name,
        docs_path=docs_path,
        retrieval=base.retrieval,
        embed=embedder.embed_texts if embedder is not None else None,
        embedding_model=embedder.model if embedder is not None else None,
        documents=documents,
        content_hash=content_hash,
    )
    reranker = LiteLLMReranker(base.rerank) if base.rerank is not None else None
    gate = None
    min_relevance = RelevanceLevel.PARTIAL
    if base.relevance_gate is not None:
        gate = RelevanceGate(base.relevance_gate)
        min_relevance = base.relevance_gate.min_relevance
    return KnowledgePipeline(
        store=store,
        reranker=reranker,
        gate=gate,
        min_relevance=min_relevance,
    )


def _tool_description(options: KnowledgePluginOptions) -> str:
    catalog = "；".join(
        f"{base.name}（{base.description}）" if base.description else base.name
        for base in options.bases
    )
    return (
        "在指定知识库中检索与问题相关的文档，返回按相关性排序的原文片段。"
        f"可用知识库：{catalog}。"
    )
