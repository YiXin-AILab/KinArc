"""知识库问题集的磁盘事实源：模型、读写与审核状态操作。

问题集位于 ``knowledge/<kb>/question_sets/<set-name>.json``（或应用级库
的同名相对路径），是批测（batch-search 的 question_set 输入源）与
「沉淀为评测集」的共享存储。全部条目先进入人工审核队列（pending），
采纳/编辑/丢弃后由批测与沉淀消费；status != discarded 的条目参与批测，
仅 accepted 条目可沉淀为 EvalCase。
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import StrictModel
from pydantic import Field, ValidationError

QUESTION_SETS_DIRNAME = "question_sets"

_SET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

type CaseType = Literal["single_doc", "multi_doc", "boundary", "out_of_scope"]
type QuestionStatus = Literal["pending", "accepted", "discarded"]


class QuestionSetItem(StrictModel):
    """问题集中的一条候选问题及其审核状态。"""

    question: str = Field(min_length=1)
    expected_doc_ids: tuple[str, ...] = ()
    reference_answer: str | None = None
    case_type: CaseType = "single_doc"
    status: QuestionStatus = "pending"
    edited: bool = False
    forbidden: tuple[str, ...] = ()


class QuestionSet(StrictModel):
    """一个命名问题集文件的全部内容。"""

    name: str = Field(min_length=1, max_length=64)
    created_at: str = Field(min_length=1)
    source: Literal["generated", "manual"]
    guidance: str | None = None
    items: tuple[QuestionSetItem, ...] = ()


class QuestionSetSummary(StrictModel):
    """问题集列表摘要：按审核状态计数。"""

    name: str
    created_at: str | None
    source: str | None
    total: int
    pending: int
    accepted: int
    discarded: int
    valid: bool
    error: str | None


class QuestionSetView(StrictModel):
    """问题集完整内容响应。"""

    question_set: QuestionSet


def new_set_name() -> str:
    """按当前时间生成稳定可读的问题集名称。"""
    return datetime.now(UTC).strftime("gen-%Y%m%d-%H%M%S")


def list_question_sets(kb_dir: Path) -> tuple[QuestionSetSummary, ...]:
    """列出知识库全部问题集摘要；损坏文件以 valid=False 列出。"""
    root = kb_dir / QUESTION_SETS_DIRNAME
    if not root.is_dir():
        return ()
    summaries: list[QuestionSetSummary] = []
    for path in sorted(root.glob("*.json")):
        name = path.stem
        try:
            question_set = load_question_set(kb_dir, name)
        except ServiceError as error:
            summaries.append(
                QuestionSetSummary(
                    name=name,
                    created_at=None,
                    source=None,
                    total=0,
                    pending=0,
                    accepted=0,
                    discarded=0,
                    valid=False,
                    error=error.message,
                )
            )
            continue
        summaries.append(summarize_question_set(question_set))
    return tuple(summaries)


def load_question_set(kb_dir: Path, set_name: str) -> QuestionSet:
    """读取并校验单个问题集；不存在抛 NOT_FOUND，结构无效抛 INVALID。

    Args:
        kb_dir: 知识库目录。
        set_name: 问题集名称。

    Returns:
        已校验的问题集。
    """
    path = _set_path(kb_dir, set_name)
    if not path.is_file():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="kb_question_set_not_found",
            message=f"问题集不存在：{set_name}",
        )
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_question_set_unreadable",
            message=f"问题集文件不可读：{path.name}",
        ) from error
    try:
        question_set = QuestionSet.model_validate(payload)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_question_set_invalid",
            message=f"问题集结构无效：{path.name}（{error.error_count()} 个错误）",
        ) from error
    if question_set.name != set_name:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_question_set_name_mismatch",
            message=f"问题集 name（{question_set.name}）与文件名（{set_name}）不一致",
        )
    return question_set


def save_question_set(kb_dir: Path, question_set: QuestionSet) -> Path:
    """把问题集写回磁盘；文件名即 name，已存在则覆盖。

    Args:
        kb_dir: 知识库目录。
        question_set: 完整问题集内容。

    Returns:
        已写入的问题集文件路径。
    """
    path = _set_path(kb_dir, question_set.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        question_set.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def patch_question_set_item(
    kb_dir: Path,
    set_name: str,
    index: int,
    *,
    status: QuestionStatus | None = None,
    question: str | None = None,
    expected_doc_ids: tuple[str, ...] | None = None,
    reference_answer: str | None = None,
    known_doc_ids: set[str] | None = None,
) -> QuestionSetItem:
    """更新单条候选的审核状态或内容；内容被编辑时 edited=True。

    Args:
        kb_dir: 知识库目录。
        set_name: 问题集名称。
        index: 目标问题在问题集中的下标。
        status: 新的审核状态；为 None 时保持原值。
        question: 用户问题正文。
        expected_doc_ids: 期望命中的文档标识。
        reference_answer: 参考答案正文。
        known_doc_ids: 提供时校验 expected_doc_ids 必须全部存在于库中。

    Returns:
        已更新并保存的候选问题。

    Raises:
        ServiceError: 下标越界（NOT_FOUND）或 expected_doc_ids 含未知文档
            （INVALID）。
    """
    question_set = load_question_set(kb_dir, set_name)
    items = list(question_set.items)
    if not 0 <= index < len(items):
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="kb_question_item_not_found",
            message=f"问题集条目不存在：{set_name}[{index}]",
        )
    item = items[index]
    edited = item.edited
    if expected_doc_ids is not None and known_doc_ids is not None:
        unknown = sorted(set(expected_doc_ids) - known_doc_ids)
        if unknown:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="kb_question_doc_unknown",
                message=f"expected_doc_ids 包含库中不存在的文档：{', '.join(unknown)}",
            )
    updates: dict[str, object] = {}
    if status is not None:
        updates["status"] = status
    for field, value in (
        ("question", question),
        ("expected_doc_ids", expected_doc_ids),
        ("reference_answer", reference_answer),
    ):
        if value is not None:
            updates[field] = value
            edited = True
    updates["edited"] = edited
    items[index] = item.model_copy(update=updates)
    save_question_set(kb_dir, question_set.model_copy(update={"items": tuple(items)}))
    return items[index]


def accept_all_items(kb_dir: Path, set_name: str) -> QuestionSet:
    """把全部 pending 条目置为 accepted，返回并落盘更新后的问题集。

    Args:
        kb_dir: 知识库目录。
        set_name: 问题集名称。

    Returns:
        已更新并保存的完整问题集。
    """
    question_set = load_question_set(kb_dir, set_name)
    items = tuple(
        item.model_copy(update={"status": "accepted"})
        if item.status == "pending"
        else item
        for item in question_set.items
    )
    updated = question_set.model_copy(update={"items": items})
    save_question_set(kb_dir, updated)
    return updated


def summarize_question_set(question_set: QuestionSet) -> QuestionSetSummary:
    """按审核状态计数构造问题集摘要。"""
    counts = {"pending": 0, "accepted": 0, "discarded": 0}
    for item in question_set.items:
        counts[item.status] += 1
    return QuestionSetSummary(
        name=question_set.name,
        created_at=question_set.created_at,
        source=question_set.source,
        total=len(question_set.items),
        pending=counts["pending"],
        accepted=counts["accepted"],
        discarded=counts["discarded"],
        valid=True,
        error=None,
    )


def _set_path(kb_dir: Path, set_name: str) -> Path:
    if not _SET_NAME_RE.fullmatch(set_name):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="kb_question_set_name_invalid",
            message=f"问题集名称不合法：{set_name}",
        )
    return kb_dir / QUESTION_SETS_DIRNAME / f"{set_name}.json"


__all__ = [
    "QUESTION_SETS_DIRNAME",
    "CaseType",
    "QuestionSet",
    "QuestionSetItem",
    "QuestionSetSummary",
    "QuestionSetView",
    "QuestionStatus",
    "accept_all_items",
    "list_question_sets",
    "load_question_set",
    "new_set_name",
    "patch_question_set_item",
    "save_question_set",
    "summarize_question_set",
]
