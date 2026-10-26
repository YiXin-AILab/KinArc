"""知识库问题集生成与审核 API：LLM 生成候选、人工审核、沉淀为评测集。

生成是后台 Job（参考 evaluation_runner 的后台线程 + 轮询模式，Job 状态
仅存进程内存，重启后不可查）：

- ``POST /v1/platform/knowledge-bases/generate`` 启动生成 Job，LLM 使用
  ``EVAL_JUDGE_LLM_*`` 环境变量（LiteLLM 直调，复用 evaluation/judge.py
  的 JSON 解析与重试模式）；
- 逐库产出：每个源知识库独立生成并写入各自的
  ``question_sets/<set-name>.json``（同一个 Job 共用同一 set 名），
  候选全部以 pending 进入审核队列；
- ``GET /v1/platform/kb-gen-jobs/{job_id}`` 轮询状态与逐库结果摘要；
- 审核（采纳/编辑/丢弃/全部采纳）与「沉淀为评测集」（accepted 条目转
  EvalCase 追加进 eval_tasks/<task>/datasets/<dataset>.json）。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

import litellm
import yaml
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import JsonValue, StrictModel
from agentsystem.knowledge.documents import KnowledgeDocument, load_documents
from agentsystem.knowledge.registry import KB_DOCUMENTS_FILENAME, KB_MANIFEST_FILENAME
from agentsystem.knowledge.store import tokenize
from evaluation.models import EvalCase, Expected, JudgeLlmConfig
from fastapi import APIRouter, status
from pydantic import Field, ValidationError

from server.platform.knowledge.questions import (
    CaseType,
    QuestionSet,
    QuestionSetItem,
    QuestionSetSummary,
    QuestionSetView,
    QuestionStatus,
    accept_all_items,
    list_question_sets,
    load_question_set,
    new_set_name,
    patch_question_set_item,
    save_question_set,
    summarize_question_set,
)
from server.platform.knowledge.routes import existing_kb_dir
from server.platform.store import PlatformStore

_LOGGER = logging.getLogger(__name__)

type CompletionFn = Callable[[str], str]
"""同步的 prompt → 文本补全端口，测试可注入确定实现。"""

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?\n]+")
_DEDUP_THRESHOLD = 0.8
_CATALOG_DOC_LIMIT = 20
_LLM_MAX_ATTEMPTS = 2

_DEFAULT_DISTRIBUTION: dict[CaseType, float] = {
    "single_doc": 0.5,
    "multi_doc": 0.25,
    "boundary": 0.15,
    "out_of_scope": 0.10,
}
"""未在请求中指定时的默认 case 类型分布；guidance 原文另随 Prompt 传给 LLM。"""


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class PlatformKbGenerateRequest(StrictModel):
    """问题集生成请求；sources 为 ``name``（全局）或 ``<agent_id>/<name>``。"""

    sources: tuple[str, ...] = Field(min_length=1, max_length=8)
    guidance: str = ""
    count: int = Field(default=10, ge=1, le=100)


class PlatformKbGenerateResponse(StrictModel):
    """生成 Job 启动响应。"""

    job_id: str
    status: str


class PlatformKbGenKbResult(StrictModel):
    """单个源知识库的生成结果摘要；shortfall 如实报告补生成后仍不足的类型。"""

    source: str
    status: str
    question_set: str | None
    generated: int
    kept: int
    type_counts: dict[str, int]
    shortfall: dict[str, int]
    error: str | None


class PlatformKbGenJobResponse(StrictModel):
    """生成 Job 状态与结果摘要。"""

    job_id: str
    status: str
    created_at: float
    finished_at: float | None
    sources: tuple[str, ...]
    count: int
    results: tuple[PlatformKbGenKbResult, ...]
    error: str | None


class PlatformKbQuestionSetListResponse(StrictModel):
    """问题集列表响应。"""

    question_sets: tuple[QuestionSetSummary, ...]


class PlatformKbQuestionItemPatch(StrictModel):
    """单条候选审核更新；内容字段被修改时 edited 自动置 True。"""

    status: str | None = None
    question: str | None = Field(default=None, min_length=1)
    expected_doc_ids: tuple[str, ...] | None = None
    reference_answer: str | None = None


class PlatformKbQuestionItemResponse(StrictModel):
    """单条候选响应。"""

    index: int
    item: QuestionSetItem


class PlatformKbToEvalDatasetRequest(StrictModel):
    """沉淀为评测集请求：accepted 条目追加进指定任务的数据集文件。"""

    task_id: str = Field(min_length=1, max_length=64)
    dataset: str = Field(min_length=1, max_length=64)


class PlatformKbToEvalDatasetResponse(StrictModel):
    """沉淀结果：追加条数、按 id 去重跳过条数与数据集总条数。"""

    task_id: str
    dataset: str
    appended: int
    skipped: int
    total_cases: int


# ---------------------------------------------------------------------------
# 生成 Job 注册表
# ---------------------------------------------------------------------------


class KbGenJobStatus(StrEnum):
    """生成 Job 的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class KbGenJobView:
    """生成 Job 的只读快照。"""

    job_id: str
    status: KbGenJobStatus
    created_at: float
    finished_at: float | None
    sources: tuple[str, ...]
    count: int
    guidance: str
    results: tuple[PlatformKbGenKbResult, ...]
    error: str | None


class KbGenJobRegistry:
    """进程内问题集生成 Job 注册表；Job 在独立 daemon 线程中执行。"""

    def __init__(
        self,
        *,
        store: PlatformStore,
        environ: Mapping[str, str] | None = None,
        complete: CompletionFn | None = None,
    ) -> None:
        """绑定问题集生成依赖，初始化进程内 Job 注册表。

        Args:
            store: 平台资源路径配置。
            environ: 用于配置环境变量插值的映射。
            complete: 可注入的同步文本补全函数。
        """
        self._store = store
        self._environ = environ
        self._complete = complete
        self._lock = threading.Lock()
        self._jobs: dict[str, KbGenJobView] = {}

    def start(self, request: PlatformKbGenerateRequest) -> KbGenJobView:
        """校验源知识库与裁判配置并启动生成线程。

        Raises:
            ServiceError: 源知识库不存在，或缺少 EVAL_JUDGE_LLM_* 配置。
        """
        resolved = tuple(
            (source, *self._resolve_source(source)) for source in request.sources
        )
        complete = self._complete or _litellm_complete(self._resolve_llm_config())
        job_id = f"gen-{uuid.uuid4().hex[:12]}"
        view = KbGenJobView(
            job_id=job_id,
            status=KbGenJobStatus.PENDING,
            created_at=time.time(),
            finished_at=None,
            sources=request.sources,
            count=request.count,
            guidance=request.guidance,
            results=(),
            error=None,
        )
        with self._lock:
            self._jobs[job_id] = view
        thread = threading.Thread(
            target=self._thread_main,
            args=(job_id, resolved, request.guidance, request.count, complete),
            name=f"kb-gen-{job_id}",
            daemon=True,
        )
        thread.start()
        return view

    def get(self, job_id: str) -> KbGenJobView:
        """返回 Job 快照；不存在抛 KeyError。"""
        with self._lock:
            view = self._jobs.get(job_id)
        if view is None:
            raise KeyError(job_id)
        return view

    def _resolve_source(self, source: str) -> tuple[str, Path]:
        """解析 ``[scope/]name`` 源并定位知识库目录，返回 (库名, 目录)。"""
        scope, _, name = source.partition("/")
        if not name:
            scope, name = "global", scope
        return name, existing_kb_dir(self._store, scope, name)

    def _resolve_llm_config(self) -> JudgeLlmConfig:
        """从环境变量解析生成用 LLM 配置；缺失时抛 INVALID 只报变量名。"""
        environ = self._environ if self._environ is not None else os.environ
        try:
            return JudgeLlmConfig(
                model=environ.get("EVAL_JUDGE_LLM_MODEL") or "",
                api_key=environ.get("EVAL_JUDGE_LLM_API_KEY") or "",
                base_url=environ.get("EVAL_JUDGE_LLM_BASE_URL") or "",
            )
        except ValidationError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_gen_llm_missing",
                message=(
                    "缺少生成模型配置：请配置环境变量 "
                    "EVAL_JUDGE_LLM_MODEL/API_KEY/BASE_URL"
                ),
            ) from error

    def _publish(self, view: KbGenJobView) -> None:
        with self._lock:
            self._jobs[view.job_id] = view

    def _thread_main(
        self,
        job_id: str,
        resolved: tuple[tuple[str, str, Path], ...],
        guidance: str,
        count: int,
        complete: CompletionFn,
    ) -> None:
        self._publish(replace(self.get(job_id), status=KbGenJobStatus.RUNNING))
        set_name = new_set_name()
        results: list[PlatformKbGenKbResult] = []
        counts = _split_count(count, len(resolved))
        for (source, kb_name, kb_dir), kb_count in zip(resolved, counts, strict=True):
            results.append(
                self._generate_for_kb(
                    source=source,
                    kb_name=kb_name,
                    kb_dir=kb_dir,
                    count=kb_count,
                    guidance=guidance,
                    set_name=set_name,
                    complete=complete,
                )
            )
        all_failed = all(result.status == "failed" for result in results)
        self._publish(
            replace(
                self.get(job_id),
                status=KbGenJobStatus.FAILED
                if all_failed
                else KbGenJobStatus.SUCCEEDED,
                finished_at=time.time(),
                results=tuple(results),
                error="全部源知识库生成失败" if all_failed else None,
            )
        )

    def _generate_for_kb(
        self,
        *,
        source: str,
        kb_name: str,
        kb_dir: Path,
        count: int,
        guidance: str,
        set_name: str,
        complete: CompletionFn,
    ) -> PlatformKbGenKbResult:
        """生成单个知识库的问题集并落盘；失败收敛为该库的结果条目。"""
        try:
            documents, _ = load_documents(kb_dir / KB_DOCUMENTS_FILENAME)
            if not documents:
                raise ValueError("知识库没有文档，无法生成问题")
            report = generate_question_set(
                kb_name=kb_name,
                description=_kb_description(kb_dir),
                documents=documents,
                count=count,
                guidance=guidance,
                complete=complete,
            )
            save_question_set(
                kb_dir,
                QuestionSet(
                    name=set_name,
                    created_at=datetime.now(UTC).isoformat(),
                    source="generated",
                    guidance=guidance or None,
                    items=report.items,
                ),
            )
            return PlatformKbGenKbResult(
                source=source,
                status="succeeded",
                question_set=set_name,
                generated=report.generated,
                kept=len(report.items),
                type_counts=report.type_counts,
                shortfall=report.shortfall,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - 单库失败不炸整个 Job
            log_unexpected_exception(
                _LOGGER,
                "kb_generate_failed",
                error,
                operation="platform.kb_generate",
                source=source,
            )
            return PlatformKbGenKbResult(
                source=source,
                status="failed",
                question_set=None,
                generated=0,
                kept=0,
                type_counts={},
                shortfall={},
                error=f"{type(error).__name__}: {error}",
            )


def _split_count(count: int, parts: int) -> tuple[int, ...]:
    """把总条数尽量均匀分配到各源知识库，余数分配给排在前面的库。"""
    base, remainder = divmod(count, parts)
    return tuple(base + (1 if index < remainder else 0) for index in range(parts))


def _kb_description(kb_dir: Path) -> str:
    """尽力读取 kb.yaml 的 description，供生成 Prompt 使用。"""
    try:
        raw = yaml.safe_load(
            (kb_dir / KB_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError):
        return ""
    if isinstance(raw, Mapping):
        description = raw.get("description")
        if isinstance(description, str):
            return description
    return ""


def _litellm_complete(config: JudgeLlmConfig) -> CompletionFn:
    """构造基于 litellm 同步调用的生成补全；失败重试一次后抛错。"""

    def complete(prompt: str) -> str:
        last_error: Exception | None = None
        for _ in range(_LLM_MAX_ATTEMPTS):
            try:
                # api_key 只进入 litellm 调用参数，不写入任何日志。
                response = litellm.completion(
                    model=config.model,
                    api_key=config.api_key,
                    base_url=config.base_url,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=config.timeout_seconds,
                )
                content = response.choices[0].message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("生成输出为空")
                return content
            except Exception as error:  # noqa: BLE001 - 重试一次后抛给调用方
                last_error = error
        assert last_error is not None
        raise last_error

    return complete


# ---------------------------------------------------------------------------
# 生成管线（Prompt 组装、解析、校验、去重、类型分布与补生成）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenerationReport:
    """单个知识库一次生成的产出与如实统计。"""

    items: tuple[QuestionSetItem, ...]
    generated: int
    type_counts: dict[str, int]
    dropped_duplicates: int
    dropped_invalid: int
    shortfall: dict[str, int]


_GUIDANCE_BLOCK = "额外指导要求：\n{guidance}\n\n"

_SINGLE_DOC_PROMPT = """你是评测数据构造专家。{guidance_block}根据以下知识库文档，
生成 1 条单文档问答评测问题。

要求：
- 问题能由该文档直接回答，语气贴近真实客户提问，口语化；
- 参考答案基于文档内容，简洁列出关键事实。

文档 ID：{doc_id}
文档标题：{title}
文档内容：{content}

只输出 JSON，不要输出其他内容：
{{"question": "...", "reference_answer": "..."}}"""

_MULTI_DOC_PROMPT = """你是评测数据构造专家。{guidance_block}根据以下知识库文档目录，
生成 {n} 条需要结合多篇文档才能完整回答的组合问题。

要求：
- 每条问题的 expected_doc_ids 必须引用目录中至少 2 个真实存在的文档 ID；
- 参考答案基于所引用文档的内容；
- 语气贴近真实客户提问，口语化。

文档目录（ID | 标题）：
{catalog}

只输出 JSON 数组，不要输出其他内容：
[{{"question": "...", "expected_doc_ids": ["id1", "id2"],
  "reference_answer": "..."}}]"""

_BOUNDARY_PROMPT = """你是评测数据构造专家。{guidance_block}根据以下知识库文档目录，
生成 {n} 条边界混淆问题：问题看似指向某篇相似文档，但正确答案只在一篇文档中。

要求：
- 每条问题给出正确答案所在的唯一文档 ID（expected_doc_ids 恰好 1 个）；
- forbidden 列出容易误答的错误表述关键词（如混淆车型/项目的错误价格或政策）；
- 参考答案基于正确文档的内容；
- 语气贴近真实客户提问，口语化。

文档目录（ID | 标题）：
{catalog}

只输出 JSON 数组，不要输出其他内容：
[{{"question": "...", "expected_doc_ids": ["id"], "forbidden": ["..."],
  "reference_answer": "..."}}]"""

_OUT_OF_SCOPE_PROMPT = """你是评测数据构造专家。{guidance_block}以下是知识库
「{kb_name}」（{description}）的文档目录。生成 {n} 条客户可能会问、
但该知识库无法回答的库外问题（用于验证拒答行为）。

要求：
- 问题必须与目录中任何文档都无关，但仍在合理的客服对话语境内；
- 语气贴近真实客户提问，口语化。

文档目录（ID | 标题）：
{catalog}

只输出 JSON 数组，不要输出其他内容：
[{{"question": "..."}}]"""

_SUPPLEMENT_PROMPT = """你是评测数据构造专家。{guidance_block}根据以下知识库
「{kb_name}」文档目录，再生成 {n} 条「{type_label}」类型的评测问题，
且不得与已有问题重复。

已有问题：
{existing}

文档目录（ID | 标题）：
{catalog}

只输出 JSON 数组，不要输出其他内容：
[{{"question": "...", "expected_doc_ids": ["id"], "reference_answer": "...",
  "forbidden": []}}]"""

_TYPE_LABELS = {
    "single_doc": "单文档问答",
    "multi_doc": "多文档组合",
    "boundary": "边界混淆",
    "out_of_scope": "库外拒答",
}


def type_targets(count: int) -> dict[CaseType, int]:
    """按默认分布拆分各类型目标数量；count 足够时每个类型保底 1 条。

    先给四种类型各保底 1 条，剩余条数按 50/25/15/10 比例分配，
    取整余数归 single_doc；count 小于类型数时全部归 single_doc。
    """
    case_types = tuple(_DEFAULT_DISTRIBUTION)
    if count < len(case_types):
        return {
            case_type: (count if case_type == "single_doc" else 0)
            for case_type in case_types
        }
    targets = dict.fromkeys(case_types, 1)
    remaining = count - len(case_types)
    for case_type in case_types:
        targets[case_type] += int(remaining * _DEFAULT_DISTRIBUTION[case_type])
    targets["single_doc"] += count - sum(targets.values())
    return targets


def generate_question_set(
    *,
    kb_name: str,
    description: str,
    documents: Sequence[KnowledgeDocument],
    count: int,
    guidance: str,
    complete: CompletionFn,
) -> GenerationReport:
    """按类型分布逐类生成候选问题，校验、去重并对不足类型补生成一次。

    单个 LLM 调用失败只跳过对应批次；补生成仍不足的类型在 shortfall
    中如实报告。

    Args:
        kb_name: 知识库名称。
        description: 资源或业务内容的描述。
        documents: 候选知识库文档。
        count: 期望生成的问题数量。
        guidance: 附加的问题生成要求。
        complete: 可注入的同步文本补全函数。

    Returns:
        候选问题、生成计数与不足类型统计。
    """
    targets = type_targets(count)
    catalog = _format_catalog(documents)
    known_ids = {document.doc_id for document in documents}
    guidance_block = _GUIDANCE_BLOCK.format(guidance=guidance) if guidance else ""
    raw_items: list[QuestionSetItem] = []
    raw_items.extend(
        _generate_single_doc(
            documents,
            target=targets["single_doc"],
            guidance_block=guidance_block,
            complete=complete,
        )
    )
    batch_types: tuple[tuple[CaseType, str], ...] = (
        ("multi_doc", _MULTI_DOC_PROMPT),
        ("boundary", _BOUNDARY_PROMPT),
        ("out_of_scope", _OUT_OF_SCOPE_PROMPT),
    )
    for case_type, prompt_template in batch_types:
        target = targets[case_type]
        if target <= 0:
            continue
        batch = _complete_batch(
            prompt_template.format(
                guidance_block=guidance_block,
                n=target,
                catalog=catalog,
                kb_name=kb_name,
                description=description or "（无描述）",
            ),
            complete=complete,
        )
        raw_items.extend(
            _parse_batch_items(batch, case_type=case_type, known_doc_ids=known_ids)
        )
    generated = len(raw_items)
    items, dropped_duplicates = dedup_items(raw_items)
    items, dropped_invalid = _drop_invalid(items, known_doc_ids=known_ids)

    type_counts = _count_by_type(items)
    shortfall: dict[CaseType, int] = {
        case_type: targets[case_type] - type_counts.get(case_type, 0)
        for case_type in targets
        if targets[case_type] - type_counts.get(case_type, 0) > 0
    }
    for case_type, missing in shortfall.items():
        batch = _complete_batch(
            _SUPPLEMENT_PROMPT.format(
                guidance_block=guidance_block,
                kb_name=kb_name,
                n=missing,
                type_label=_TYPE_LABELS[case_type],
                existing="\n".join(f"- {item.question}" for item in items) or "（无）",
                catalog=catalog,
            ),
            complete=complete,
        )
        extra, _ = _drop_invalid(
            _parse_batch_items(batch, case_type=case_type, known_doc_ids=known_ids),
            known_doc_ids=known_ids,
        )
        generated += len(extra)
        merged, _ = dedup_items([*items, *extra])
        items = merged

    type_counts = _count_by_type(items)
    final_shortfall: dict[str, int] = {
        case_type: targets[case_type] - type_counts.get(case_type, 0)
        for case_type in targets
        if targets[case_type] - type_counts.get(case_type, 0) > 0
    }
    return GenerationReport(
        items=tuple(items),
        generated=generated,
        type_counts=type_counts,
        dropped_duplicates=dropped_duplicates,
        dropped_invalid=dropped_invalid,
        shortfall=final_shortfall,
    )


def _generate_single_doc(
    documents: Sequence[KnowledgeDocument],
    *,
    target: int,
    guidance_block: str,
    complete: CompletionFn,
) -> list[QuestionSetItem]:
    """逐文档生成单文档问答；文档不足时循环取样，单篇失败跳过。"""
    items: list[QuestionSetItem] = []
    if target <= 0:
        return items
    for index in range(target):
        document = documents[index % len(documents)]
        batch = _complete_batch(
            _SINGLE_DOC_PROMPT.format(
                guidance_block=guidance_block,
                doc_id=document.doc_id,
                title=document.title,
                content=document.content,
            ),
            complete=complete,
        )
        items.extend(
            _parse_batch_items(
                batch,
                case_type="single_doc",
                known_doc_ids={document.doc_id},
                fallback_doc_id=document.doc_id,
            )
        )
    return items


def _complete_batch(prompt: str, *, complete: CompletionFn) -> list[object]:
    """调用 LLM 并解析 JSON 对象/数组；失败时记录并返回空批次。"""
    try:
        payload = _extract_json_payload(complete(prompt))
    except Exception as error:  # noqa: BLE001 - 单批失败不炸整个生成
        log_unexpected_exception(
            _LOGGER,
            "kb_generate_batch_failed",
            error,
            operation="platform.kb_generate",
        )
        return []
    return payload if isinstance(payload, list) else [payload]


def _parse_batch_items(
    batch: Sequence[object],
    *,
    case_type: CaseType,
    known_doc_ids: set[str],
    fallback_doc_id: str | None = None,
) -> list[QuestionSetItem]:
    """把 LLM 输出解析为候选条目；expected_doc_ids 过滤到库内真实文档。"""
    items: list[QuestionSetItem] = []
    for entry in batch:
        if not isinstance(entry, Mapping):
            continue
        question = entry.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        raw_ids = entry.get("expected_doc_ids")
        expected = tuple(
            sorted(
                {
                    doc_id
                    for doc_id in (
                        raw_ids
                        if isinstance(raw_ids, list)
                        else ([fallback_doc_id] if fallback_doc_id else [])
                    )
                    if isinstance(doc_id, str) and doc_id in known_doc_ids
                }
            )
        )
        raw_forbidden = entry.get("forbidden")
        forbidden = tuple(
            word
            for word in (raw_forbidden if isinstance(raw_forbidden, list) else [])
            if isinstance(word, str) and word.strip()
        )
        raw_answer = entry.get("reference_answer")
        items.append(
            QuestionSetItem(
                question=question.strip(),
                expected_doc_ids=expected,
                reference_answer=(
                    raw_answer.strip()
                    if isinstance(raw_answer, str) and raw_answer.strip()
                    else None
                ),
                case_type=case_type,
                forbidden=forbidden,
            )
        )
    return items


def _drop_invalid(
    items: Sequence[QuestionSetItem],
    *,
    known_doc_ids: set[str],
) -> tuple[list[QuestionSetItem], int]:
    """丢弃既无有效期望文档又无参考答案的非库外条目。"""
    kept: list[QuestionSetItem] = []
    dropped = 0
    for item in items:
        if (
            item.case_type != "out_of_scope"
            and not item.expected_doc_ids
            and not item.reference_answer
        ):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def dedup_items(
    items: Sequence[QuestionSetItem],
    *,
    threshold: float = _DEDUP_THRESHOLD,
) -> tuple[list[QuestionSetItem], int]:
    """按问题文本去重：完全相同或 token Jaccard 相似度 ≥ threshold 丢弃。

    Args:
        items: 待处理的候选问题序列。
        threshold: 问题文本的 Jaccard 相似度去重阈值。

    Returns:
        保留原顺序的去重结果，以及被丢弃的条目数量。
    """
    kept: list[QuestionSetItem] = []
    seen: list[frozenset[str]] = []
    dropped = 0
    for item in items:
        tokens = frozenset(tokenize(item.question))
        if not tokens or any(
            _jaccard(tokens, previous) >= threshold for previous in seen
        ):
            dropped += 1
            continue
        kept.append(item)
        seen.append(tokens)
    return kept, dropped


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _count_by_type(items: Sequence[QuestionSetItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.case_type] = counts.get(item.case_type, 0) + 1
    return counts


def _format_catalog(documents: Sequence[KnowledgeDocument]) -> str:
    """生成 Prompt 用的文档目录（ID | 标题），限制条数控制 token。"""
    return "\n".join(
        f"{document.doc_id} | {document.title}"
        for document in documents[:_CATALOG_DOC_LIMIT]
    )


def _extract_json_payload(text: str) -> object:
    """从生成输出中提取首个 JSON 数组或对象并解析。"""
    starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
    if not starts:
        raise ValueError("生成输出中不存在 JSON")
    start = min(starts)
    closer = "]" if text[start] == "[" else "}"
    end = text.rfind(closer)
    if end <= start:
        raise ValueError("生成输出 JSON 不完整")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------------------
# 沉淀为评测集
# ---------------------------------------------------------------------------


def _case_id(scope: str, kb_name: str, set_name: str, index: int) -> str:
    """生成无歧义的 EvalCase id：可读前缀 + 结构化字段的 sha256 截断摘要。

    scope/kb_name/set_name 都允许含连字符，直接拼接存在歧义（如
    ``sales-a/pricing`` 与 ``sales/a-pricing`` 生成同一前缀）。摘要以
    JSON 数组编码四个字段后取 sha256 前 12 位十六进制，JSON 的字符串
    引用规则保证编码无歧义，且跨进程稳定（不使用内置 hash()）。
    """
    payload = json.dumps([scope, kb_name, set_name, index], ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{scope}-{kb_name}-{set_name}-{index:03d}-{digest}"


def to_eval_cases(
    *,
    scope: str,
    kb_name: str,
    set_name: str,
    items: Sequence[tuple[int, QuestionSetItem]],
) -> list[EvalCase]:
    """将已采纳的候选问题转换为具有稳定来源标识的评测用例。

    用例 ID 格式为
    ``<scope>-<kb-name>-<set-name>-<原始下标>-<sha256 摘要前 12 位>``。

    items 中每个条目必须携带其在原始问题集中的下标（而非 accepted 过滤后
    的位置），保证分批采纳、多次沉淀时 id 稳定；scope/kb/set/下标共同经
    sha256 摘要编码进 id，避免名称含连字符时不同来源碰撞（见 _case_id）。

    映射规则：question→input；expected_doc_ids→expected.gold_doc_ids；
    reference_answer 按句拆分进 expected.key_points（原句保留在 meta）；
    case_type=out_of_scope→expected.must_refuse=true；boundary 携带的
    forbidden 词→expected.forbidden。

    Args:
        scope: 资源范围：global 或 Agent 标识。
        kb_name: 知识库名称。
        set_name: 问题集名称。
        items: 原始下标与已采纳候选问题的二元组序列。

    Returns:
        从候选问题转换的评测用例，保持来源标识稳定。
    """
    cases: list[EvalCase] = []
    for index, item in items:
        meta: dict[str, JsonValue] = {
            "source": "question_set",
            "question_set": set_name,
        }
        if item.reference_answer:
            meta["reference_answer"] = item.reference_answer
        cases.append(
            EvalCase(
                id=_case_id(scope, kb_name, set_name, index),
                input=item.question,
                expected=Expected(
                    key_points=split_sentences(item.reference_answer or ""),
                    forbidden=list(item.forbidden),
                    must_refuse=item.case_type == "out_of_scope",
                    gold_doc_ids=list(item.expected_doc_ids),
                ),
                tags=[kb_name, item.case_type],
                meta=meta,
            )
        )
    return cases


def split_sentences(text: str) -> list[str]:
    """按中英文句读把参考答案拆为关键要点列表。"""
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def append_eval_dataset(
    tasks_root: Path,
    task_id: str,
    dataset: str,
    cases: Sequence[EvalCase],
) -> tuple[int, int, int]:
    """把 EvalCase 追加进 eval_tasks/<task>/datasets/<dataset>.json。

    已存在的同 id case 跳过；单次输入内部的重复 id 只保留首条，其余计为
    跳过。返回 (追加条数, 跳过条数, 数据集总条数)。

    Args:
        tasks_root: 任务目录根路径。
        task_id: 任务标识。
        dataset: 不含扩展名的目标评测数据集名称。
        cases: 待追加的评测用例。

    Returns:
        追加条数、去重跳过条数与写入后的总条数。

    Raises:
        ServiceError: 任务不存在（NOT_FOUND）、数据集名不合法或已有数据集
            结构无效（INVALID）。
    """
    task_dir = _eval_task_dir(tasks_root, task_id)
    if not _ID_RE.fullmatch(dataset):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="eval_dataset_name_invalid",
            message=f"数据集名不合法：{dataset}",
        )
    datasets_dir = task_dir / "datasets"
    path = datasets_dir / f"{dataset}.json"
    existing: list[EvalCase] = []
    if path.exists():
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="eval_dataset_unreadable",
                message=f"数据集不可读：{path.name}",
            ) from error
        if not isinstance(payload, list):
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="eval_dataset_invalid",
                message=f"数据集必须是 JSON 数组：{path.name}",
            )
        try:
            existing = [EvalCase.model_validate(item) for item in payload]
        except ValidationError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="eval_dataset_invalid",
                message=(
                    f"数据集结构无效：{path.name}（{error.error_count()} 个错误）"
                ),
            ) from error
    existing_ids = {case.id for case in existing}
    appended: list[EvalCase] = []
    for case in cases:
        if case.id in existing_ids:
            continue
        existing_ids.add(case.id)
        appended.append(case)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    merged = [*existing, *appended]
    path.write_text(
        json.dumps(
            [case.model_dump(mode="json") for case in merged],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(appended), len(cases) - len(appended), len(merged)


def _eval_task_dir(tasks_root: Path, task_id: str) -> Path:
    """校验并定位评测任务目录；不存在抛 NOT_FOUND。"""
    if not _ID_RE.fullmatch(task_id):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="eval_task_id_invalid",
            message=f"评测任务 ID 不合法：{task_id}",
        )
    task_dir = tasks_root / task_id
    if not task_dir.is_dir() or not (task_dir / "config.yaml").is_file():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="eval_task_not_found",
            message=f"评测任务不存在：{task_id}",
        )
    return task_dir


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def register_kb_generation_routes(
    router: APIRouter,
    store: PlatformStore,
    registry: KbGenJobRegistry,
) -> None:
    """注册问题集生成 Job 与审核/沉淀路由。

    Args:
        router: 接收新路由的 FastAPI 路由器。
        store: 平台资源路径配置。
        registry: 问题集生成任务注册表。
    """
    _register_generate_routes(router, registry)
    _register_review_routes(router, store)


def _register_generate_routes(router: APIRouter, registry: KbGenJobRegistry) -> None:
    """注册生成 Job 启动与轮询路由。"""

    @router.post(
        "/knowledge-bases/generate",
        response_model=PlatformKbGenerateResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def generate_questions(
        request: PlatformKbGenerateRequest,
    ) -> PlatformKbGenerateResponse:
        """启动问题集生成 Job；候选全部以 pending 写入各源库的问题集。"""
        view = registry.start(request)
        return PlatformKbGenerateResponse(job_id=view.job_id, status=view.status.value)

    @router.get("/kb-gen-jobs/{job_id}", response_model=PlatformKbGenJobResponse)
    def get_generation_job(job_id: str) -> PlatformKbGenJobResponse:
        """轮询生成 Job 状态与逐库结果摘要。"""
        try:
            view = registry.get(job_id)
        except KeyError as error:
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="kb_gen_job_not_found",
                message=f"生成 Job 不存在：{job_id}",
            ) from error
        return PlatformKbGenJobResponse(
            job_id=view.job_id,
            status=view.status.value,
            created_at=view.created_at,
            finished_at=view.finished_at,
            sources=view.sources,
            count=view.count,
            results=view.results,
            error=view.error,
        )


def _register_review_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册问题集列表、读取、审核与沉淀路由。"""

    @router.get(
        "/knowledge-bases/{scope}/{name}/question_sets",
        response_model=PlatformKbQuestionSetListResponse,
    )
    def list_sets(scope: str, name: str) -> PlatformKbQuestionSetListResponse:
        """按库列出问题集及 pending/accepted/discarded 计数。"""
        kb_dir = existing_kb_dir(store, scope, name)
        return PlatformKbQuestionSetListResponse(
            question_sets=list_question_sets(kb_dir)
        )

    @router.get(
        "/knowledge-bases/{scope}/{name}/question_sets/{set_name}",
        response_model=QuestionSetView,
    )
    def read_set(scope: str, name: str, set_name: str) -> QuestionSetView:
        """读取单个问题集的完整内容。"""
        kb_dir = existing_kb_dir(store, scope, name)
        return QuestionSetView(question_set=load_question_set(kb_dir, set_name))

    @router.patch(
        "/knowledge-bases/{scope}/{name}/question_sets/{set_name}/items/{index}",
        response_model=PlatformKbQuestionItemResponse,
    )
    def patch_item(
        scope: str,
        name: str,
        set_name: str,
        index: int,
        request: PlatformKbQuestionItemPatch,
    ) -> PlatformKbQuestionItemResponse:
        """审核单条候选：采纳/丢弃/编辑；编辑内容自动置 edited=True。"""
        kb_dir = existing_kb_dir(store, scope, name)
        item = patch_question_set_item(
            kb_dir,
            set_name,
            index,
            status=_parse_status(request.status),
            question=request.question,
            expected_doc_ids=request.expected_doc_ids,
            reference_answer=request.reference_answer,
            known_doc_ids=_known_doc_ids(kb_dir),
        )
        return PlatformKbQuestionItemResponse(index=index, item=item)

    @router.post(
        "/knowledge-bases/{scope}/{name}/question_sets/{set_name}/accept-all",
        response_model=QuestionSetSummary,
    )
    def accept_all(scope: str, name: str, set_name: str) -> QuestionSetSummary:
        """把全部 pending 条目置为 accepted，返回更新后的计数摘要。"""
        kb_dir = existing_kb_dir(store, scope, name)
        return summarize_question_set(accept_all_items(kb_dir, set_name))

    @router.post(
        "/knowledge-bases/{scope}/{name}/question_sets/{set_name}/to-eval-dataset",
        response_model=PlatformKbToEvalDatasetResponse,
    )
    def convert_to_eval_dataset(
        scope: str,
        name: str,
        set_name: str,
        request: PlatformKbToEvalDatasetRequest,
    ) -> PlatformKbToEvalDatasetResponse:
        """把 accepted 条目转为 EvalCase 追加进 eval_tasks 数据集。"""
        kb_dir = existing_kb_dir(store, scope, name)
        question_set = load_question_set(kb_dir, set_name)
        accepted = [
            (index, item)
            for index, item in enumerate(question_set.items)
            if item.status == "accepted"
        ]
        if not accepted:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_question_set_no_accepted",
                message=f"问题集 {set_name} 没有已采纳条目",
            )
        appended, skipped, total = append_eval_dataset(
            store.project_root / "eval_tasks",
            request.task_id,
            request.dataset,
            to_eval_cases(scope=scope, kb_name=name, set_name=set_name, items=accepted),
        )
        return PlatformKbToEvalDatasetResponse(
            task_id=request.task_id,
            dataset=request.dataset,
            appended=appended,
            skipped=skipped,
            total_cases=total,
        )


def _parse_status(value: str | None) -> QuestionStatus | None:
    """校验审核状态取值；非法值抛 INVALID。"""
    if value is None:
        return None
    if value not in ("pending", "accepted", "discarded"):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_question_status_invalid",
            message=f"审核状态不合法：{value}",
        )
    return cast(QuestionStatus, value)


def _known_doc_ids(kb_dir: Path) -> set[str] | None:
    """尽力加载库内文档 ID 集合；文档文件缺失/无效时不做校验。"""
    try:
        documents, _ = load_documents(kb_dir / KB_DOCUMENTS_FILENAME)
    except (OSError, ValueError):
        return None
    return {document.doc_id for document in documents}


__all__ = [
    "GenerationReport",
    "KbGenJobRegistry",
    "KbGenJobStatus",
    "KbGenJobView",
    "PlatformKbGenerateRequest",
    "append_eval_dataset",
    "dedup_items",
    "generate_question_set",
    "register_kb_generation_routes",
    "split_sentences",
    "to_eval_cases",
    "type_targets",
]
