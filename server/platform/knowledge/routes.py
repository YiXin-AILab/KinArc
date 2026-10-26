"""平台知识库资源 API：磁盘 CRUD 与检索测试（单测/批测）。

知识库事实源为磁盘：全局 ``knowledge/<name>/{kb.yaml, documents.json}`` 与
应用级 ``application/<app>/knowledge/<name>/``。scope 路径参数对齐 Skill
路由：``global`` 表示全局库，其他取值按 agent_id 定位应用级库。

检索测试是调试工具：按请求覆盖配置临时构造真实 KnowledgePipeline
（检索 → rerank → 相关性裁判），不产生任何磁盘变更。批测的文本答案
裁判复用 evaluation 的 LiteLLM 直调模式，默认读取
``EVAL_JUDGE_LLM_MODEL/API_KEY/BASE_URL`` 环境变量，请求可覆盖。
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import litellm
import yaml
from agentsystem.common.errors import AdapterError, ServiceError, ServiceErrorKind
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import JsonValue, StrictModel
from agentsystem.config.loader import interpolate_environment
from agentsystem.knowledge.documents import load_documents
from agentsystem.knowledge.embeddings import LiteLLMEmbedder
from agentsystem.knowledge.gate import RelevanceGate
from agentsystem.knowledge.models import (
    KnowledgeBaseManifest,
    RelevanceGateConfig,
    RelevanceLevel,
    RerankConfig,
    RetrievalConfig,
    RetrievalStrategy,
)
from agentsystem.knowledge.pipeline import KnowledgePipeline, PipelineResult
from agentsystem.knowledge.registry import KB_DOCUMENTS_FILENAME, KB_MANIFEST_FILENAME
from agentsystem.knowledge.rerank import LiteLLMReranker
from agentsystem.knowledge.store import KnowledgeStore, ScoredDocument, tokenize
from evaluation.judge import extract_json_object
from evaluation.models import JudgeLlmConfig
from fastapi import APIRouter, status
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from server.platform import store as platform_store
from server.platform.knowledge.questions import load_question_set
from server.platform.store import PlatformStore

_LOGGER = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")
_SEARCH_TIMEOUT_SECONDS = 120.0
_JUDGE_MAX_ATTEMPTS = 2

_SECRET_MASK = "********"
_SECRET_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("retrieval", "embedding", "api_key"),
    ("rerank", "api_key"),
    ("relevance_gate", "api_key"),
)

_DIFF_NOTE = (
    "diff 基于检索同款中英文 tokenizer 的词级重叠 best-effort 结果，"
    "仅供定位参考，不构成精确语义比对"
)


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class PlatformKbSummary(StrictModel):
    """单个知识库的列表摘要。"""

    name: str
    scope: str
    description: str
    doc_count: int | None
    strategy: str | None
    top_k: int | None
    has_rerank: bool
    has_gate: bool
    referenced_by: tuple[str, ...]
    valid: bool
    error: str | None


class PlatformKbListResponse(StrictModel):
    """知识库列表响应。"""

    knowledge_bases: tuple[PlatformKbSummary, ...]


class PlatformKbResponse(StrictModel):
    """单个知识库摘要响应。"""

    knowledge_base: PlatformKbSummary


class PlatformKbCreateRequest(StrictModel):
    """新建知识库请求；创建默认 bm25 检索配置的 kb.yaml 与空文档文件。"""

    scope: str = "global"
    name: str = Field(min_length=1, max_length=64)
    description: str = ""


class PlatformKbManifestResponse(StrictModel):
    """kb.yaml 原文内容响应；``${VAR}`` 占位符保持原样不回显密钥。"""

    name: str
    scope: str
    config: dict[str, JsonValue]


class PlatformKbManifestUpdateRequest(StrictModel):
    """kb.yaml 覆盖写请求；name 必须与路径一致不可修改。"""

    config: dict[str, JsonValue]


class PlatformKbDocument(StrictModel):
    """一条知识库文档；写操作按此结构全字段校验。"""

    doc_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    content: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class PlatformKbDocsResponse(StrictModel):
    """知识库文档列表响应。"""

    knowledge_base: str
    docs: tuple[PlatformKbDocument, ...]


class PlatformKbDocResponse(StrictModel):
    """单条知识库文档响应。"""

    doc: PlatformKbDocument


class PlatformKbRetrievalOverrides(StrictModel):
    """检索覆盖配置；缺省字段跟随库默认，只影响本次调用不落盘。

    ``rerank``/``gate`` 接受完整配置（``${VAR}`` 占位符按环境变量插值），
    或 ``{"enabled": false}`` 显式关闭库默认阶段。
    """

    strategy: RetrievalStrategy | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    rerank: dict[str, JsonValue] | None = None
    gate: dict[str, JsonValue] | None = None


class PlatformKbSearchRequest(PlatformKbRetrievalOverrides):
    """单次检索测试请求；doc_scope 圈定时只在给定文档子集内检索。"""

    query: str = Field(min_length=1)
    doc_scope: tuple[str, ...] | None = Field(default=None, min_length=1)


class PlatformKbSearchChunk(StrictModel):
    """一个检索片段及其裁判结果；filtered 标记被裁判剔除的候选。"""

    doc_id: str
    title: str
    content: str
    score: float
    gate_relevance: str | None
    gate_reason: str | None
    gate_error: bool
    filtered: bool


class PlatformKbSearchResponse(StrictModel):
    """单次检索测试响应。"""

    knowledge_base: str
    query: str
    chunks: tuple[PlatformKbSearchChunk, ...]
    no_relevant_docs: bool


class PlatformKbBatchQuestion(StrictModel):
    """一道批测题；两种答案形式可独立或同时提供。"""

    question: str = Field(min_length=1)
    expected_doc_ids: tuple[str, ...] | None = Field(default=None, min_length=1)
    reference_answer: str | None = None


class PlatformKbJudgeLlmOverride(StrictModel):
    """批测文本裁判模型覆盖；缺省字段回退 EVAL_JUDGE_LLM_* 环境变量。"""

    model: str | None = None
    api_key: SecretStr | None = None
    base_url: str | None = None


class PlatformKbBatchSearchRequest(PlatformKbRetrievalOverrides):
    """批量检索测试请求；questions 与 question_set 二选一。

    ``question_set`` 从该库 ``question_sets/<set>.json`` 取
    status != discarded 的条目作为题目，expected_doc_ids 与
    reference_answer 自动带入。
    """

    questions: tuple[PlatformKbBatchQuestion, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    question_set: str | None = Field(default=None, min_length=1, max_length=64)
    judge_llm: PlatformKbJudgeLlmOverride | None = None
    concurrency: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def validate_exactly_one_question_source(self) -> Self:
        """questions 与 question_set 必须且只能提供一个。"""
        if (self.questions is None) == (self.question_set is None):
            raise ValueError("questions 与 question_set 必须且只能提供一个")
        return self


class PlatformKbMissingDoc(StrictModel):
    """一条未被最终命中的期望文档诊断：全库排名或被裁判剔除标记。"""

    doc_id: str
    rank: int | None
    gate_filtered: bool


class PlatformKbDocMetrics(StrictModel):
    """单题 doc_id 答案指标。"""

    expected_doc_ids: tuple[str, ...]
    top1_hit: bool
    top3_hit: bool
    set_recall: bool
    gate_false_kills: tuple[str, ...]
    missing: tuple[PlatformKbMissingDoc, ...]


class PlatformKbAnswerDiff(StrictModel):
    """参考答案与检索内容的词级重叠 diff。"""

    matched: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]


class PlatformKbAnswerMetrics(StrictModel):
    """单题文本答案指标；裁判失败时 coverage=None 并携带 error。"""

    coverage: float | None
    rationale: str
    diff: PlatformKbAnswerDiff
    diff_note: str
    error: str | None


class PlatformKbBatchItem(StrictModel):
    """单题批测结果；检索失败时 error 非空且其余指标为空。"""

    question: str
    chunks: tuple[PlatformKbSearchChunk, ...]
    no_relevant_docs: bool
    doc_metrics: PlatformKbDocMetrics | None
    answer_metrics: PlatformKbAnswerMetrics | None
    error: str | None


class PlatformKbDocSummary(StrictModel):
    """doc_id 题指标汇总。"""

    count: int
    top1_rate: float
    top3_rate: float
    set_recall_rate: float
    gate_false_kill_count: int


class PlatformKbAnswerSummary(StrictModel):
    """文本题指标汇总；average_coverage 只统计裁判成功的题。"""

    count: int
    judged_count: int
    average_coverage: float | None


class PlatformKbBatchSummary(StrictModel):
    """批测指标汇总，doc_id 题与文本题分开统计。"""

    doc_questions: PlatformKbDocSummary
    answer_questions: PlatformKbAnswerSummary


class PlatformKbBatchSearchResponse(StrictModel):
    """批量检索测试响应。"""

    knowledge_base: str
    results: tuple[PlatformKbBatchItem, ...]
    summary: PlatformKbBatchSummary


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def register_knowledge_base_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册平台知识库资源 CRUD、检索测试与文档导入路由。

    Args:
        router: 接收新路由的 FastAPI 路由器。
        store: 平台资源路径配置。
    """
    from server.platform.knowledge.import_routes import register_kb_import_routes

    _register_kb_crud_routes(router, store)
    _register_kb_doc_routes(router, store)
    _register_kb_search_routes(router, store)
    register_kb_import_routes(router, store)


def _register_kb_crud_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册知识库列表、新建与 kb.yaml 读写路由。"""

    @router.get("/knowledge-bases", response_model=PlatformKbListResponse)
    def list_knowledge_bases() -> PlatformKbListResponse:
        """列出全局与全部应用级知识库摘要（含引用它的 agents）。"""
        return PlatformKbListResponse(knowledge_bases=_list_kb_summaries(store))

    @router.post(
        "/knowledge-bases",
        response_model=PlatformKbResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_knowledge_base(request: PlatformKbCreateRequest) -> PlatformKbResponse:
        """新建知识库：建目录、默认 kb.yaml（bm25）与空 documents.json。"""
        _create_kb(store, request)
        summary = _describe_kb(
            existing_kb_dir(store, request.scope, request.name),
            scope=request.scope,
            references=platform_store.knowledge_base_reference_map(
                store.applications_root
            ),
        )
        return PlatformKbResponse(knowledge_base=summary)

    @router.get(
        "/knowledge-bases/{scope}/{name}",
        response_model=PlatformKbManifestResponse,
    )
    def read_knowledge_base(scope: str, name: str) -> PlatformKbManifestResponse:
        """读取 kb.yaml 原文（库设置：默认策略/裁判配置）；secret 字段脱敏。"""
        kb_dir = existing_kb_dir(store, scope, name)
        return PlatformKbManifestResponse(
            name=name,
            scope=scope,
            config=_mask_secret_fields(
                _load_yaml_mapping(kb_dir / KB_MANIFEST_FILENAME)
            ),
        )

    @router.put(
        "/knowledge-bases/{scope}/{name}",
        response_model=PlatformKbManifestResponse,
    )
    def update_knowledge_base(
        scope: str,
        name: str,
        request: PlatformKbManifestUpdateRequest,
    ) -> PlatformKbManifestResponse:
        """覆盖写 kb.yaml；校验 KnowledgeBaseManifest 且 name 不可修改。"""
        kb_dir = existing_kb_dir(store, scope, name)
        _write_manifest(kb_dir, name, request.config)
        return PlatformKbManifestResponse(
            name=name,
            scope=scope,
            config=_mask_secret_fields(request.config),
        )


def _register_kb_doc_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册知识库文档 CRUD 路由。"""

    @router.get(
        "/knowledge-bases/{scope}/{name}/docs",
        response_model=PlatformKbDocsResponse,
    )
    def list_kb_docs(scope: str, name: str) -> PlatformKbDocsResponse:
        """列出知识库全部文档；documents.json 缺失时返回空列表。"""
        kb_dir = existing_kb_dir(store, scope, name)
        return PlatformKbDocsResponse(
            knowledge_base=name,
            docs=_load_kb_docs(kb_dir / KB_DOCUMENTS_FILENAME),
        )

    @router.post(
        "/knowledge-bases/{scope}/{name}/docs",
        response_model=PlatformKbDocResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_kb_doc(
        scope: str,
        name: str,
        payload: PlatformKbDocument,
    ) -> PlatformKbDocResponse:
        """新增一条知识库文档；doc_id 重复时返回 409。"""
        _validate_id(payload.doc_id, label="文档 ID")
        kb_dir = existing_kb_dir(store, scope, name)
        path = kb_dir / KB_DOCUMENTS_FILENAME
        docs = list(_load_kb_docs(path))
        if any(doc.doc_id == payload.doc_id for doc in docs):
            raise ServiceError(
                kind=ServiceErrorKind.CONFLICT,
                code="kb_doc_exists",
                message=f"文档已存在：{payload.doc_id}",
            )
        docs.append(payload)
        _save_kb_docs(path, tuple(docs))
        return PlatformKbDocResponse(doc=payload)

    @router.put(
        "/knowledge-bases/{scope}/{name}/docs/{doc_id}",
        response_model=PlatformKbDocResponse,
    )
    def replace_kb_doc(
        scope: str,
        name: str,
        doc_id: str,
        payload: PlatformKbDocument,
    ) -> PlatformKbDocResponse:
        """整体替换一条知识库文档；body 的 doc_id 必须与路径一致。"""
        _validate_id(doc_id, label="文档 ID")
        if payload.doc_id != doc_id:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_doc_id_mismatch",
                message=f"body 的 doc_id（{payload.doc_id}）与路径不一致：{doc_id}",
            )
        kb_dir = existing_kb_dir(store, scope, name)
        path = kb_dir / KB_DOCUMENTS_FILENAME
        docs = list(_load_kb_docs(path))
        docs[_doc_index(docs, doc_id)] = payload
        _save_kb_docs(path, tuple(docs))
        return PlatformKbDocResponse(doc=payload)

    @router.delete(
        "/knowledge-bases/{scope}/{name}/docs/{doc_id}",
        response_model=PlatformKbDocResponse,
    )
    def delete_kb_doc(scope: str, name: str, doc_id: str) -> PlatformKbDocResponse:
        """删除一条知识库文档，返回被删除的内容。"""
        _validate_id(doc_id, label="文档 ID")
        kb_dir = existing_kb_dir(store, scope, name)
        path = kb_dir / KB_DOCUMENTS_FILENAME
        docs = list(_load_kb_docs(path))
        removed = docs.pop(_doc_index(docs, doc_id))
        _save_kb_docs(path, tuple(docs))
        return PlatformKbDocResponse(doc=removed)


def _register_kb_search_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册知识库检索测试（单测/批测）路由。"""

    @router.post(
        "/knowledge-bases/{scope}/{name}/search",
        response_model=PlatformKbSearchResponse,
    )
    async def search_knowledge_base(
        scope: str,
        name: str,
        request: PlatformKbSearchRequest,
    ) -> PlatformKbSearchResponse:
        """单次检索测试：真实管线跑一次，覆盖配置只影响本次调用。"""
        return await execute_kb_search(store, scope, name, request)

    @router.post(
        "/knowledge-bases/{scope}/{name}/batch-search",
        response_model=PlatformKbBatchSearchResponse,
    )
    async def batch_search_knowledge_base(
        scope: str,
        name: str,
        request: PlatformKbBatchSearchRequest,
    ) -> PlatformKbBatchSearchResponse:
        """批量检索测试：有界并发逐题跑管线并汇总 doc_id/文本两类指标。"""
        return await execute_kb_batch_search(store, scope, name, request)


# ---------------------------------------------------------------------------
# 磁盘事实源
# ---------------------------------------------------------------------------


def _list_kb_summaries(store: PlatformStore) -> tuple[PlatformKbSummary, ...]:
    references = platform_store.knowledge_base_reference_map(store.applications_root)
    summaries = [
        _describe_kb(kb_dir, scope="global", references=references)
        for kb_dir in _kb_dirs(store.project_root / "knowledge")
    ]
    for agent_id, root in platform_store.list_agent_knowledge_roots(
        store.applications_root
    ):
        summaries.extend(
            _describe_kb(kb_dir, scope=agent_id, references=references)
            for kb_dir in _kb_dirs(root)
        )
    return tuple(summaries)


def _describe_kb(
    kb_dir: Path,
    *,
    scope: str,
    references: Mapping[str, tuple[str, ...]],
) -> PlatformKbSummary:
    """构造单个磁盘知识库的摘要；kb.yaml 或文档无效时以 valid=False 列出。"""
    name = kb_dir.name
    base = {
        "name": name,
        "scope": scope,
        "referenced_by": references.get(name, ()),
    }
    try:
        manifest = KnowledgeBaseManifest.model_validate(
            _load_yaml_mapping(kb_dir / KB_MANIFEST_FILENAME)
        )
        if manifest.name != name:
            raise ValueError(f"kb.yaml name（{manifest.name}）与目录名（{name}）不一致")
        doc_count = len(_load_kb_docs(kb_dir / KB_DOCUMENTS_FILENAME))
    except (ServiceError, ValidationError, ValueError) as error:
        return PlatformKbSummary(
            **base,
            description="",
            doc_count=None,
            strategy=None,
            top_k=None,
            has_rerank=False,
            has_gate=False,
            valid=False,
            error=str(error),
        )
    return PlatformKbSummary(
        **base,
        description=manifest.description,
        doc_count=doc_count,
        strategy=manifest.retrieval.strategy.value,
        top_k=manifest.retrieval.top_k,
        has_rerank=manifest.rerank is not None,
        has_gate=manifest.relevance_gate is not None,
        valid=True,
        error=None,
    )


def _create_kb(store: PlatformStore, request: PlatformKbCreateRequest) -> None:
    """落盘新知识库目录、默认 kb.yaml 与空 documents.json。"""
    _validate_id(request.name, label="知识库名称")
    root = _kb_scope_root(store, request.scope)
    kb_dir = root / request.name
    if kb_dir.exists():
        raise ServiceError(
            kind=ServiceErrorKind.CONFLICT,
            code="kb_already_exists",
            message=f"知识库已存在：{request.scope}/{request.name}",
        )
    kb_dir.mkdir(parents=True)
    manifest = {
        "name": request.name,
        "description": request.description,
        "retrieval": {"strategy": "bm25", "top_k": 5},
    }
    (kb_dir / KB_MANIFEST_FILENAME).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (kb_dir / KB_DOCUMENTS_FILENAME).write_text("[]\n", encoding="utf-8")


def _write_manifest(
    kb_dir: Path,
    name: str,
    config: Mapping[str, JsonValue],
) -> None:
    """校验并覆盖写 kb.yaml；原文（含 ``${VAR}`` 占位符）按样落盘。"""
    try:
        manifest = KnowledgeBaseManifest.model_validate(config)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_manifest_invalid",
            message=_format_validation_error(error),
        ) from error
    if manifest.name != name:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_name_immutable",
            message=f"kb.yaml name（{manifest.name}）与目录名（{name}）不一致",
        )
    _validate_secret_placeholders(config)
    (kb_dir / KB_MANIFEST_FILENAME).write_text(
        yaml.safe_dump(dict(config), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _validate_secret_placeholders(config: Mapping[str, JsonValue]) -> None:
    """拒绝 secret 字段中的非 ``${ENV_NAME}`` 占位符值。

    错误消息只携带字段路径，绝不包含被拒绝的原始值。
    """
    for path in _SECRET_FIELD_PATHS:
        value = _nested_config_value(config, path)
        if value is None:
            continue
        if not isinstance(value, str) or not _ENV_PLACEHOLDER_RE.fullmatch(value):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_secret_not_placeholder",
                message=(
                    f"{'.'.join(path)} 只接受 ${{ENV_NAME}} 环境变量占位符，"
                    "禁止明文密钥"
                ),
            )


def _mask_secret_fields(config: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """对响应中的 secret 字段脱敏：缺失与 ``${ENV}`` 占位符保持原样，其他替换为掩码。"""
    masked = dict(config)
    for path in _SECRET_FIELD_PATHS:
        node = masked
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, Mapping):
                break
            child = dict(child)
            node[key] = child
            node = child
        else:
            leaf = node.get(path[-1])
            if leaf is None:
                continue
            if isinstance(leaf, str) and _ENV_PLACEHOLDER_RE.fullmatch(leaf):
                continue
            node[path[-1]] = _SECRET_MASK
    return masked


def _nested_config_value(
    config: Mapping[str, JsonValue],
    path: tuple[str, ...],
) -> object:
    """按路径读取嵌套配置值；任一层缺失或不是 mapping 时返回 None。"""
    node: object = config
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _kb_scope_root(store: PlatformStore, scope: str) -> Path:
    """按 scope 定位知识库根：global 为项目 knowledge/，否则为应用 knowledge/。"""
    if scope == "global":
        return store.project_root / "knowledge"
    agent_dir = platform_store.find_agent_dir(store.applications_root, scope)
    if agent_dir is None:
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="agent_not_found",
            message=f"Agent 不存在：{scope}",
        )
    return agent_dir / "knowledge"


def existing_kb_dir(store: PlatformStore, scope: str, name: str) -> Path:
    """定位知识库目录；scope 为 global 或 agent_id，不存在抛 NOT_FOUND。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。

    Returns:
        已确认存在的知识库目录。
    """
    _validate_id(name, label="知识库名称")
    kb_dir = _kb_scope_root(store, scope) / name
    if not kb_dir.is_dir():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="kb_not_found",
            message=f"知识库不存在：{scope}/{name}",
        )
    return kb_dir


def _kb_dirs(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            ),
            key=lambda path: path.name,
        )
    )


def _validate_id(value: str, *, label: str) -> None:
    """校验路径中的名称/ID，杜绝路径逃逸字符。"""
    if not _ID_RE.fullmatch(value):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_id_invalid",
            message=f"{label}不合法：{value}",
        )


def _load_yaml_mapping(path: Path) -> dict[str, JsonValue]:
    """读取 YAML mapping 原文，不做环境变量插值。"""
    if not path.is_file():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="kb_manifest_missing",
            message=f"知识库缺少 {KB_MANIFEST_FILENAME}",
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_manifest_unreadable",
            message=f"kb.yaml 解析失败：{type(error).__name__}",
        ) from error
    if not isinstance(loaded, Mapping):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_manifest_invalid",
            message="kb.yaml 根节点必须是 mapping",
        )
    return {str(key): _json_value(value) for key, value in loaded.items()}


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _load_kb_docs(path: Path) -> tuple[PlatformKbDocument, ...]:
    """读取并校验知识库文档数组；文件缺失视为空库。"""
    if not path.exists():
        return ()
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_docs_unreadable",
            message=f"知识库文件不可读：{path.name}",
        ) from error
    if not isinstance(payload, list):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_docs_invalid",
            message=f"知识库文件顶层必须是数组：{path.name}",
        )
    try:
        return tuple(PlatformKbDocument.model_validate(item) for item in payload)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_docs_invalid",
            message=(
                f"知识库文档结构无效：{path.name}（{error.error_count()} 个错误）"
            ),
        ) from error


def _save_kb_docs(path: Path, docs: tuple[PlatformKbDocument, ...]) -> None:
    """把校验后的文档数组写回知识库文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [doc.model_dump() for doc in docs]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _doc_index(docs: list[PlatformKbDocument], doc_id: str) -> int:
    """定位文档下标；不存在时抛 NOT_FOUND。"""
    for index, doc in enumerate(docs):
        if doc.doc_id == doc_id:
            return index
    raise ServiceError(
        kind=ServiceErrorKind.NOT_FOUND,
        code="kb_doc_not_found",
        message=f"文档不存在：{doc_id}",
    )


def _format_validation_error(error: ValidationError) -> str:
    """只保留字段路径与错误类型，避免错误消息回显 api_key 等配置值。"""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors()
    )


# ---------------------------------------------------------------------------
# 检索测试执行
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PreparedSearch:
    """按请求覆盖配置临时构造的检索管线与未裁剪的底层 store。"""

    pipeline: KnowledgePipeline
    store: KnowledgeStore


class _ScopedKnowledgeStore(KnowledgeStore):
    """把候选裁剪到 doc_scope 子集的调试专用 KnowledgeStore 装饰子类。

    基于全量排名裁剪，保证子集内文档不受全库 top_k 截断影响；BM25/hybrid
    得分仍基于全库语料计算（IDF 与归一化不按子集重算），裁剪只影响进入
    重排/裁判的候选范围。
    """

    def __init__(
        self,
        *,
        delegate: KnowledgeStore,
        doc_ids: Collection[str],
        top_k: int,
    ) -> None:
        self._delegate = delegate
        self._doc_ids = frozenset(doc_ids)
        self._scope_top_k = top_k

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[ScoredDocument]:
        del top_k  # 子集检索始终基于无截断的全量排名再裁剪
        ranked = await self._delegate.search_all(query)
        scoped = [item for item in ranked if item.document.doc_id in self._doc_ids]
        return scoped[: self._scope_top_k]


async def execute_kb_search(
    store: PlatformStore,
    scope: str,
    name: str,
    request: PlatformKbSearchRequest,
) -> PlatformKbSearchResponse:
    """执行单次检索测试并返回片段级结果。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        request: 已校验的请求参数。

    Returns:
        检索片段、裁判信息与质量指标。

    Raises:
        ServiceError: 库不存在、配置无效、外部检索调用失败或整体超时。
    """
    kb_dir = existing_kb_dir(store, scope, name)
    prepared = await asyncio.to_thread(
        _prepare_search, kb_dir, name, request, doc_scope=request.doc_scope
    )
    result = await _run_pipeline(prepared.pipeline, request.query)
    return PlatformKbSearchResponse(
        knowledge_base=name,
        query=request.query,
        chunks=_chunks_from_result(result),
        no_relevant_docs=result.no_relevant_docs,
    )


async def execute_kb_batch_search(
    store: PlatformStore,
    scope: str,
    name: str,
    request: PlatformKbBatchSearchRequest,
) -> PlatformKbBatchSearchResponse:
    """有界并发逐题执行批测；单题失败记 error 不影响整批。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        request: 已校验的请求参数。

    Returns:
        逐题结果、失败说明与聚合指标。
    """
    kb_dir = existing_kb_dir(store, scope, name)
    questions = await asyncio.to_thread(_resolve_batch_questions, kb_dir, request)
    prepared = await asyncio.to_thread(
        _prepare_search, kb_dir, name, request, doc_scope=None
    )
    judge_config = None
    if any(question.reference_answer is not None for question in questions):
        judge_config = resolve_judge_llm_config(request.judge_llm)
    semaphore = asyncio.Semaphore(request.concurrency)
    items = await asyncio.gather(
        *(
            _run_batch_question(prepared, question, judge_config, semaphore)
            for question in questions
        )
    )
    return PlatformKbBatchSearchResponse(
        knowledge_base=name,
        results=tuple(items),
        summary=summarize_batch_results(items),
    )


def _resolve_batch_questions(
    kb_dir: Path,
    request: PlatformKbBatchSearchRequest,
) -> tuple[PlatformKbBatchQuestion, ...]:
    """解析批测题目来源：内联 questions 或问题集（status != discarded）。"""
    if request.questions is not None:
        return request.questions
    assert request.question_set is not None
    question_set = load_question_set(kb_dir, request.question_set)
    questions = tuple(
        PlatformKbBatchQuestion(
            question=item.question,
            expected_doc_ids=item.expected_doc_ids or None,
            reference_answer=item.reference_answer,
        )
        for item in question_set.items
        if item.status != "discarded"
    )
    if not questions:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_question_set_empty",
            message=f"问题集 {request.question_set} 没有可用题目（全部已丢弃或为空）",
        )
    return questions


def _prepare_search(
    kb_dir: Path,
    name: str,
    overrides: PlatformKbRetrievalOverrides,
    *,
    doc_scope: Collection[str] | None,
) -> _PreparedSearch:
    """按库默认 + 请求覆盖临时构造检索管线；不落盘、不影响运行时配置。"""
    manifest = _load_manifest_for_search(kb_dir, name)
    retrieval = _apply_retrieval_override(manifest.retrieval, overrides)
    rerank_config = _apply_stage_override(
        overrides.rerank, manifest.rerank, RerankConfig, field="rerank"
    )
    gate_config = _apply_stage_override(
        overrides.gate, manifest.relevance_gate, RelevanceGateConfig, field="gate"
    )
    docs_path = kb_dir / KB_DOCUMENTS_FILENAME
    if not docs_path.is_file():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="kb_documents_missing",
            message=f"知识库缺少 {KB_DOCUMENTS_FILENAME}",
        )
    try:
        documents, content_hash = load_documents(docs_path)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_documents_invalid",
            message=str(error),
        ) from error
    embedder = (
        LiteLLMEmbedder(retrieval.embedding)
        if retrieval.embedding is not None
        else None
    )
    store = KnowledgeStore(
        base_name=name,
        docs_path=docs_path,
        retrieval=retrieval,
        embed=embedder.embed_texts if embedder is not None else None,
        embedding_model=embedder.model if embedder is not None else None,
        documents=documents,
        content_hash=content_hash,
    )
    effective_store: KnowledgeStore = store
    if doc_scope is not None:
        known = {document.doc_id for document in documents}
        unknown = sorted(set(doc_scope) - known)
        if unknown:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_doc_scope_unknown",
                message=f"doc_scope 包含库中不存在的文档：{', '.join(unknown)}",
            )
        effective_store = _ScopedKnowledgeStore(
            delegate=store,
            doc_ids=doc_scope,
            top_k=retrieval.top_k,
        )
    reranker = LiteLLMReranker(rerank_config) if rerank_config is not None else None
    gate = None
    min_relevance = RelevanceLevel.PARTIAL
    if gate_config is not None:
        gate = RelevanceGate(gate_config)
        min_relevance = gate_config.min_relevance
    return _PreparedSearch(
        pipeline=KnowledgePipeline(
            store=effective_store,
            reranker=reranker,
            gate=gate,
            min_relevance=min_relevance,
        ),
        store=store,
    )


def _load_manifest_for_search(kb_dir: Path, name: str) -> KnowledgeBaseManifest:
    """加载 kb.yaml 并按环境变量插值；缺失环境变量报变量名不回显值。"""
    raw = _load_yaml_mapping(kb_dir / KB_MANIFEST_FILENAME)
    try:
        interpolated = interpolate_environment(raw, os.environ)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_manifest_env_missing",
            message=str(error),
        ) from error
    try:
        manifest = KnowledgeBaseManifest.model_validate(interpolated)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_manifest_invalid",
            message=_format_validation_error(error),
        ) from error
    if manifest.name != name:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_name_mismatch",
            message=f"kb.yaml name（{manifest.name}）与目录名（{name}）不一致",
        )
    return manifest


def _apply_retrieval_override(
    base: RetrievalConfig,
    overrides: PlatformKbRetrievalOverrides,
) -> RetrievalConfig:
    """用请求级 strategy/top_k 覆盖库默认检索配置。"""
    strategy = overrides.strategy or base.strategy
    if strategy is not RetrievalStrategy.BM25 and base.embedding is None:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_embedding_missing",
            message="vector/hybrid 检索策略必须配置 embedding",
        )
    return base.model_copy(
        update={
            "strategy": strategy,
            "top_k": overrides.top_k if overrides.top_k is not None else base.top_k,
        }
    )


def _apply_stage_override[ConfigT: BaseModel](
    override: Mapping[str, JsonValue] | None,
    base: ConfigT | None,
    model_type: type[ConfigT],
    *,
    field: str,
) -> ConfigT | None:
    """解析 rerank/gate 覆盖：缺省跟随库默认，enabled=false 关闭，否则替换。"""
    if override is None:
        return base
    if override.get("enabled") is False:
        return None
    try:
        interpolated = interpolate_environment(dict(override), os.environ)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_override_env_missing",
            message=f"{field} 覆盖配置：{error}",
        ) from error
    try:
        return model_type.model_validate(interpolated)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_override_invalid",
            message=f"{field} 覆盖配置无效：{_format_validation_error(error)}",
        ) from error


async def _run_pipeline(
    pipeline: KnowledgePipeline,
    query: str,
) -> PipelineResult:
    """带整体超时跑一遍真实管线；外部失败收敛为 503。"""
    try:
        return await asyncio.wait_for(
            pipeline.search(query.strip()),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except AdapterError as error:
        raise ServiceError(
            kind=ServiceErrorKind.UNAVAILABLE,
            code="kb_search_failed",
            message=f"知识库检索失败：{error.message}",
        ) from error
    except TimeoutError as error:
        raise ServiceError(
            kind=ServiceErrorKind.UNAVAILABLE,
            code="kb_search_timeout",
            message="知识库检索整体超时",
        ) from error


def _chunks_from_result(result: PipelineResult) -> tuple[PlatformKbSearchChunk, ...]:
    """把管线结果展开为片段列表，含被裁判剔除的候选（filtered=True）。"""
    survivors = {document.doc_id for document in result.documents}
    gate_enabled = bool(result.gate)
    chunks: list[PlatformKbSearchChunk] = []
    for candidate in result.candidates:
        document = candidate.document
        entry = result.gate.get(document.doc_id)
        relevance: str | None = None
        reason: str | None = None
        gate_error = False
        if isinstance(entry, Mapping):
            raw_relevance = entry.get("relevance")
            relevance = raw_relevance if isinstance(raw_relevance, str) else None
            raw_reason = entry.get("reason")
            reason = raw_reason if isinstance(raw_reason, str) else None
            gate_error = entry.get("gate_error") is True
        chunks.append(
            PlatformKbSearchChunk(
                doc_id=document.doc_id,
                title=document.title,
                content=document.content,
                score=candidate.score,
                gate_relevance=relevance,
                gate_reason=reason,
                gate_error=gate_error,
                filtered=gate_enabled and document.doc_id not in survivors,
            )
        )
    return tuple(chunks)


async def _run_batch_question(
    prepared: _PreparedSearch,
    question: PlatformKbBatchQuestion,
    judge_config: JudgeLlmConfig | None,
    semaphore: asyncio.Semaphore,
) -> PlatformKbBatchItem:
    """执行单题检索与两类指标；任何失败降级为该题的 error 字段。"""
    async with semaphore:
        try:
            result = await _run_pipeline(prepared.pipeline, question.question)
            doc_metrics = None
            if question.expected_doc_ids is not None:
                doc_metrics = await _question_doc_metrics(prepared, question, result)
            answer_metrics = None
            if question.reference_answer is not None and judge_config is not None:
                answer_metrics = await _question_answer_metrics(
                    question, result, judge_config
                )
            return PlatformKbBatchItem(
                question=question.question,
                chunks=_chunks_from_result(result),
                no_relevant_docs=result.no_relevant_docs,
                doc_metrics=doc_metrics,
                answer_metrics=answer_metrics,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - 单题失败不炸整批
            log_unexpected_exception(
                _LOGGER,
                "kb_batch_question_failed",
                error,
                operation="platform.kb_batch_search",
            )
            return PlatformKbBatchItem(
                question=question.question,
                chunks=(),
                no_relevant_docs=True,
                doc_metrics=None,
                answer_metrics=None,
                error=f"题目执行失败（{type(error).__name__}）",
            )


async def _question_doc_metrics(
    prepared: _PreparedSearch,
    question: PlatformKbBatchQuestion,
    result: PipelineResult,
) -> PlatformKbDocMetrics:
    """计算 doc_id 答案指标；缺料时补一次无截断全库排名做诊断。"""
    expected = tuple(question.expected_doc_ids or ())
    survivors = tuple(document.doc_id for document in result.documents)
    candidate_ids = tuple(candidate.document.doc_id for candidate in result.candidates)
    full_ranking: tuple[str, ...] | None = None
    if any(
        doc_id not in survivors and doc_id not in candidate_ids for doc_id in expected
    ):
        ranked = await prepared.store.search_all(question.question.strip())
        full_ranking = tuple(item.document.doc_id for item in ranked)
    return compute_doc_metrics(
        expected_doc_ids=expected,
        surviving_doc_ids=survivors,
        candidate_doc_ids=candidate_ids,
        full_ranking=full_ranking,
    )


async def _question_answer_metrics(
    question: PlatformKbBatchQuestion,
    result: PipelineResult,
    judge_config: JudgeLlmConfig,
) -> PlatformKbAnswerMetrics:
    """计算文本答案指标：裁判覆盖分（失败降级）+ 词级重叠 diff。"""
    reference = question.reference_answer or ""
    content = "\n\n".join(
        f"{document.title}\n{document.content}" for document in result.documents
    )
    coverage: float | None = None
    rationale = ""
    error: str | None = None
    try:
        coverage, rationale = await judge_answer_coverage(
            config=judge_config,
            question=question.question,
            reference_answer=reference,
            content=content,
        )
    except Exception as judge_error:  # noqa: BLE001 - 裁判失败降级为 error 字段
        log_unexpected_exception(
            _LOGGER,
            "kb_answer_judge_failed",
            judge_error,
            operation="platform.kb_batch_search",
        )
        error = f"裁判评分失败（{type(judge_error).__name__}）"
    return PlatformKbAnswerMetrics(
        coverage=coverage,
        rationale=rationale,
        diff=token_overlap_diff(reference, content),
        diff_note=_DIFF_NOTE,
        error=error,
    )


# ---------------------------------------------------------------------------
# 指标与 diff 纯函数
# ---------------------------------------------------------------------------


def compute_doc_metrics(
    *,
    expected_doc_ids: Sequence[str],
    surviving_doc_ids: Sequence[str],
    candidate_doc_ids: Sequence[str],
    full_ranking: Sequence[str] | None,
) -> PlatformKbDocMetrics:
    """由最终命中、裁判前候选与全库排名计算 doc_id 答案指标。

    Top1/Top3 命中针对最终（裁判后）结果；期望文档检索到但被裁判剔除
    计入 gate_false_kills；未命中的期望文档给出全库实际排名（1 起），
    不在库中则 rank=None。

    Args:
        expected_doc_ids: 期望命中的文档标识。
        surviving_doc_ids: 裁判过滤后的命中文档标识。
        candidate_doc_ids: 裁判过滤前的候选文档标识。
        full_ranking: 全库文档排名；不可用时为 None。

    Returns:
        命中指标、被裁判剔除的期望文档与未命中文档排名。
    """
    survivors = list(surviving_doc_ids)
    candidates = list(candidate_doc_ids)
    ranking = list(full_ranking) if full_ranking is not None else None
    missing: list[PlatformKbMissingDoc] = []
    for doc_id in expected_doc_ids:
        if doc_id in survivors:
            continue
        if doc_id in candidates:
            missing.append(
                PlatformKbMissingDoc(
                    doc_id=doc_id,
                    rank=candidates.index(doc_id) + 1,
                    gate_filtered=True,
                )
            )
        elif ranking is not None and doc_id in ranking:
            missing.append(
                PlatformKbMissingDoc(
                    doc_id=doc_id,
                    rank=ranking.index(doc_id) + 1,
                    gate_filtered=False,
                )
            )
        else:
            missing.append(
                PlatformKbMissingDoc(doc_id=doc_id, rank=None, gate_filtered=False)
            )
    return PlatformKbDocMetrics(
        expected_doc_ids=tuple(expected_doc_ids),
        top1_hit=bool(survivors) and survivors[0] in expected_doc_ids,
        top3_hit=any(doc_id in expected_doc_ids for doc_id in survivors[:3]),
        set_recall=all(doc_id in survivors for doc_id in expected_doc_ids),
        gate_false_kills=tuple(
            doc_id
            for doc_id in expected_doc_ids
            if doc_id in candidates and doc_id not in survivors
        ),
        missing=tuple(missing),
    )


def token_overlap_diff(reference: str, content: str) -> PlatformKbAnswerDiff:
    """参考答案与检索内容的词级重叠 diff（best-effort，见 diff_note）。

    Args:
        reference: 参考答案正文。
        content: 正文文本。

    Returns:
        词级重叠差异及方法局限说明。
    """
    reference_tokens = tuple(dict.fromkeys(tokenize(reference)))
    content_tokens = tuple(dict.fromkeys(tokenize(content)))
    content_set = set(content_tokens)
    reference_set = set(reference_tokens)
    return PlatformKbAnswerDiff(
        matched=tuple(token for token in reference_tokens if token in content_set),
        missing=tuple(token for token in reference_tokens if token not in content_set),
        extra=tuple(token for token in content_tokens if token not in reference_set),
    )


def summarize_batch_results(
    items: Sequence[PlatformKbBatchItem],
) -> PlatformKbBatchSummary:
    """按 doc_id 题/文本题两类汇总批测指标；失败题不计入任何一类。"""
    doc_metrics = [
        item.doc_metrics
        for item in items
        if item.error is None and item.doc_metrics is not None
    ]
    answer_metrics = [
        item.answer_metrics
        for item in items
        if item.error is None and item.answer_metrics is not None
    ]
    coverages = [
        metrics.coverage for metrics in answer_metrics if metrics.coverage is not None
    ]
    return PlatformKbBatchSummary(
        doc_questions=PlatformKbDocSummary(
            count=len(doc_metrics),
            top1_rate=_rate(
                sum(1 for metrics in doc_metrics if metrics.top1_hit),
                len(doc_metrics),
            ),
            top3_rate=_rate(
                sum(1 for metrics in doc_metrics if metrics.top3_hit),
                len(doc_metrics),
            ),
            set_recall_rate=_rate(
                sum(1 for metrics in doc_metrics if metrics.set_recall),
                len(doc_metrics),
            ),
            gate_false_kill_count=sum(
                len(metrics.gate_false_kills) for metrics in doc_metrics
            ),
        ),
        answer_questions=PlatformKbAnswerSummary(
            count=len(answer_metrics),
            judged_count=len(coverages),
            average_coverage=(sum(coverages) / len(coverages) if coverages else None),
        ),
    )


def _rate(hits: int, total: int) -> float:
    return hits / total if total else 0.0


# ---------------------------------------------------------------------------
# 文本答案裁判
# ---------------------------------------------------------------------------


_COVERAGE_PROMPT = """你是检索质量裁判。给定用户问题、参考答案和知识库实际检索到的内容，
评估检索内容对参考答案的内容覆盖度：
- 参考答案中的关键事实（价格、政策、流程、数值）在检索内容中能否找到；
- 允许同义改写与措辞差异，只看事实是否被覆盖；
- score 为被覆盖的关键事实比例。

用户问题：{question}
参考答案：{reference_answer}
检索内容：{content}

只输出 JSON，不要输出其他内容：
{{"score": 0到1之间的小数, "rationale": "一句话评分理由"}}"""


async def judge_answer_coverage(
    *,
    config: JudgeLlmConfig,
    question: str,
    reference_answer: str,
    content: str,
) -> tuple[float, str]:
    """调用裁判模型打内容覆盖分；重试一次后仍失败则抛错由调用方降级。

    Args:
        config: 当前组件使用的配置。
        question: 用户问题正文。
        reference_answer: 参考答案正文。
        content: 正文文本。

    Returns:
        覆盖分数与裁判理由。

    Raises:
        Exception: 裁判调用或输出解析失败（含超时）。
    """
    last_error: Exception | None = None
    for _ in range(_JUDGE_MAX_ATTEMPTS):
        try:
            return await asyncio.wait_for(
                _request_coverage(
                    config,
                    question=question,
                    reference_answer=reference_answer,
                    content=content,
                ),
                timeout=config.timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - 重试一次后由调用方降级
            last_error = error
    assert last_error is not None
    raise last_error


async def _request_coverage(
    config: JudgeLlmConfig,
    *,
    question: str,
    reference_answer: str,
    content: str,
) -> tuple[float, str]:
    # api_key 只进入 litellm 调用参数，不写入任何日志或响应。
    response = await litellm.acompletion(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        messages=[
            {
                "role": "user",
                "content": _COVERAGE_PROMPT.format(
                    question=question,
                    reference_answer=reference_answer,
                    content=content or "（无：本次检索未返回任何文档）",
                ),
            }
        ],
    )
    response_content = response.choices[0].message.content
    if not isinstance(response_content, str) or not response_content.strip():
        raise ValueError("裁判输出为空")
    parsed = extract_json_object(response_content)
    score = parsed.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("裁判输出缺少数值 score")
    if not 0.0 <= float(score) <= 1.0:
        raise ValueError("裁判 score 超出 0-1 范围")
    rationale = parsed.get("rationale")
    return float(score), str(rationale) if rationale is not None else ""


def resolve_judge_llm_config(
    override: PlatformKbJudgeLlmOverride | None,
) -> JudgeLlmConfig:
    """解析裁判模型配置：请求覆盖优先，缺省回退 EVAL_JUDGE_LLM_* 环境变量。"""
    model = (override.model if override and override.model else None) or (
        os.environ.get("EVAL_JUDGE_LLM_MODEL") or ""
    )
    api_key = (
        override.api_key.get_secret_value()
        if override and override.api_key is not None
        else None
    ) or (os.environ.get("EVAL_JUDGE_LLM_API_KEY") or "")
    base_url = (override.base_url if override and override.base_url else None) or (
        os.environ.get("EVAL_JUDGE_LLM_BASE_URL") or ""
    )
    try:
        return JudgeLlmConfig(model=model, api_key=api_key, base_url=base_url)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_judge_llm_missing",
            message=(
                "缺少裁判模型配置：请在请求 judge_llm 或环境变量 "
                "EVAL_JUDGE_LLM_MODEL/API_KEY/BASE_URL 中提供"
            ),
        ) from error


__all__ = [
    "PlatformKbBatchSearchRequest",
    "PlatformKbBatchSearchResponse",
    "PlatformKbSearchRequest",
    "PlatformKbSearchResponse",
    "compute_doc_metrics",
    "execute_kb_batch_search",
    "execute_kb_search",
    "existing_kb_dir",
    "judge_answer_coverage",
    "register_knowledge_base_routes",
    "resolve_judge_llm_config",
    "summarize_batch_results",
    "token_overlap_diff",
]
