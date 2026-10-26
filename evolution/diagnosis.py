"""Train 评测证据的确定性诊断简报与单 case 失败簇问题库。

在 Improver 启动前把一次 Train Evaluation 预处理为失败聚类、退步清单和
回归保护名单。简报只做规则层粗聚类，不含 LLM 因果归因；簇归属与
「已恢复摩擦」的最终判定由 Improver 读取轨迹后完成。
简报只含分数与归一化签名，不内嵌反馈、轨迹或标准答案；
Case 级证据仍由 EvidenceTools 按需读取。
问题库由框架按相同确定性规则跨轮积累单 case 失败签名，
达到阈值前对应簇延后处理，不呈现在可处理简报中。
"""

import re
from enum import StrEnum
from typing import Literal

from agentsystem.common.models import FrozenModel
from pydantic import Field

from evolution.models import CaseEvaluation, EvaluationResult

_PASS_THRESHOLD = 0.999
_SIGNATURE_MAX_LENGTH = 60
_SIGNATURE_MIN_LENGTH = 4
_UNCLASSIFIED = "unclassified_failure"
# 归一化后只剩这些标量残片的签名不携带失败语义（常见于把结构化
# JSON 反馈当文本处理），一律按无法归类处理。
_STOP_FRAGMENT_TOKENS = frozenset({"null", "true", "false", "none"})

_FAILURE_HINT_RE = re.compile(
    r"fail|missing|error|timeout|assert|incorrect|expected|denied"
    r"|失败|错误|缺失|超时|未",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[\"'`][^\"'`]*[\"'`]")
_PATH_TOKEN_RE = re.compile(r"[\w.~-]*(?:/[\w.~-]+)+")
_DIGITS_RE = re.compile(r"\d+(?:\.\d+)?")
_NON_WORD_RE = re.compile(r"[^a-z一-鿿]+")
_UNDERSCORES_RE = re.compile(r"_+")
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


class CaseSeverity(StrEnum):
    """未通过 Case 的严重度分层。"""

    REGRESSION = "regression"
    TOTAL_FAILURE = "total_failure"
    PARTIAL = "partial"


_SEVERITY_RANK = {
    CaseSeverity.REGRESSION: 0,
    CaseSeverity.TOTAL_FAILURE: 1,
    CaseSeverity.PARTIAL: 2,
}


class DiagnosedCase(FrozenModel):
    """一个未通过 Case 的规则层诊断定位。"""

    id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    previous_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    severity: CaseSeverity
    signature: str = Field(min_length=1)


class FailureCluster(FrozenModel):
    """按归一化签名聚合的一组同因失败 Case。"""

    cluster_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    severity: CaseSeverity
    cases: tuple[DiagnosedCase, ...]


class RegressedCase(FrozenModel):
    """分数低于上轮记录的 Case，无论是否仍通过。"""

    id: str = Field(min_length=1)
    previous_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class DiagnosisBrief(FrozenModel):
    """一次 Train Evaluation 的确定性诊断简报。"""

    evidence_candidate_id: str = Field(min_length=1)
    baseline_candidate_id: str | None = Field(default=None, min_length=1)
    total_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    previous_total_score: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    clusters: tuple[FailureCluster, ...]
    regressed_cases: tuple[RegressedCase, ...]
    passing_case_ids: tuple[str, ...]


class ProblemLibraryEntry(FrozenModel):
    """问题库中一个失败签名的跨轮积累条目。"""

    signature: str = Field(min_length=1)
    severity: CaseSeverity
    case_ids: tuple[str, ...]
    first_seen_candidate_id: str = Field(min_length=1)
    last_seen_candidate_id: str = Field(min_length=1)
    status: Literal["accumulating", "ready", "resolved"]


class ProblemLibrary(FrozenModel):
    """按签名积累的实验级失败问题库。"""

    entries: tuple[ProblemLibraryEntry, ...] = ()


def build_diagnosis_brief(
    evidence_candidate_id: str,
    evaluation: EvaluationResult,
    *,
    baseline_candidate_id: str | None = None,
) -> DiagnosisBrief:
    """从 Train Evaluation 构建确定性诊断简报。

    相同输入必定产生相同简报（簇编号按优先级与簇大小稳定分配），
    因此简报无需持久化，可随时由 Evaluation 重算。
    baseline_candidate_id 仅在 Evaluation 未携带父子比较时作为展示基线。
    """
    diagnosed: list[DiagnosedCase] = []
    regressed: list[RegressedCase] = []
    passing: list[str] = []
    for case in evaluation.cases:
        if case.previous_score is not None and case.score < case.previous_score:
            regressed.append(
                RegressedCase(
                    id=case.id,
                    previous_score=case.previous_score,
                    score=case.score,
                )
            )
        if case.score >= _PASS_THRESHOLD:
            passing.append(case.id)
        else:
            diagnosed.append(_diagnose_case(case))
    comparison = evaluation.parent_comparison
    return DiagnosisBrief(
        evidence_candidate_id=evidence_candidate_id,
        baseline_candidate_id=(
            comparison.previous_candidate_id
            if comparison is not None
            else baseline_candidate_id
        ),
        total_score=evaluation.total_score,
        previous_total_score=(
            comparison.previous_total_score if comparison is not None else None
        ),
        clusters=_cluster_cases(diagnosed),
        regressed_cases=tuple(regressed),
        passing_case_ids=tuple(passing),
    )


def update_problem_library(
    library: ProblemLibrary,
    brief: DiagnosisBrief,
    *,
    min_cases: int = 2,
) -> ProblemLibrary:
    """按本轮简报幂等更新问题库。

    只有可延后的簇（单 case 且 severity 非 regression）会创建条目或推进
    累计计数；多 case 簇与 regression 簇不入库，仅刷新已有条目的
    severity 与 last_seen。unclassified 签名不入库：共享该签名不代表
    同一问题。本轮不再失败的签名置为 resolved（消失即视为已解决，
    不做清理）。累计不同 case 数达到 min_cases 的条目标记为
    ready，其余仍在失败集合中的条目为 accumulating。同一 case 反复失败
    不推进计数（集合语义）。
    """
    entries: dict[str, ProblemLibraryEntry] = {
        entry.signature: entry for entry in library.entries
    }
    failing = {
        cluster.signature
        for cluster in brief.clusters
        if cluster.signature != _UNCLASSIFIED
    }
    for signature in tuple(entries):
        if signature not in failing:
            entries[signature] = entries[signature].model_copy(
                update={"status": "resolved"}
            )
    for cluster in brief.clusters:
        # 无法归类的签名不入库：不同 Case 共享该签名不代表同一问题，
        # 互相推进计数会得到语义无意义的 ready 条目。
        if cluster.signature == _UNCLASSIFIED:
            continue
        deferrable = (
            len(cluster.cases) == 1 and cluster.severity is not CaseSeverity.REGRESSION
        )
        existing = entries.get(cluster.signature)
        if existing is None:
            if not deferrable:
                continue
            entry = ProblemLibraryEntry(
                signature=cluster.signature,
                severity=cluster.severity,
                case_ids=(cluster.cases[0].id,),
                first_seen_candidate_id=brief.evidence_candidate_id,
                last_seen_candidate_id=brief.evidence_candidate_id,
                status="accumulating",
            )
        else:
            case_ids = existing.case_ids
            if deferrable and cluster.cases[0].id not in case_ids:
                case_ids = case_ids + (cluster.cases[0].id,)
            entry = existing.model_copy(
                update={
                    "severity": cluster.severity,
                    "case_ids": case_ids,
                    "last_seen_candidate_id": brief.evidence_candidate_id,
                }
            )
        status: Literal["accumulating", "ready"] = (
            "ready" if len(entry.case_ids) >= min_cases else "accumulating"
        )
        entries[cluster.signature] = entry.model_copy(update={"status": status})
    return ProblemLibrary(entries=tuple(entries.values()))


def split_actionable_clusters(
    brief: DiagnosisBrief,
    library: ProblemLibrary,
    *,
    min_cases: int = 2,
) -> tuple[DiagnosisBrief, tuple[FailureCluster, ...]]:
    """把简报拆成可处理簇与本轮延后的单 case 簇。

    regression 簇与多 case 簇始终留在可处理侧；其余单 case 簇在问题库中
    该签名累计不同 case 数达到 min_cases 前延后，延后侧保留原 cluster_id。
    unclassified 簇始终留在可处理侧：它们需要 Improver 逐个分流，
    且共享签名在问题库中没有积累语义。
    """
    accumulated = {entry.signature: len(entry.case_ids) for entry in library.entries}
    actionable: list[FailureCluster] = []
    deferred: list[FailureCluster] = []
    for cluster in brief.clusters:
        if (
            len(cluster.cases) == 1
            and cluster.signature != _UNCLASSIFIED
            and cluster.severity is not CaseSeverity.REGRESSION
            and accumulated.get(cluster.signature, 0) < min_cases
        ):
            deferred.append(cluster)
        else:
            actionable.append(cluster)
    return brief.model_copy(update={"clusters": tuple(actionable)}), tuple(deferred)


def render_brief_markdown(
    brief: DiagnosisBrief,
    *,
    problem_library: ProblemLibrary | None = None,
    deferred_clusters: tuple[FailureCluster, ...] = (),
) -> str:
    """把诊断简报渲染为注入改进请求的固定小节文本。

    传入 problem_library 时，本轮出现且已成熟（ready）的簇在标题后
    附积累说明，提示该问题已经多轮观察。失败总数包含本轮暂缓簇，
    与问题库中的历史累计 Case 数区分。
    """
    baseline_id = brief.baseline_candidate_id or "无"
    baseline_score = (
        f"{brief.previous_total_score:.4f}"
        if brief.previous_total_score is not None
        else "—"
    )
    ready_counts = (
        {
            entry.signature: len(entry.case_ids)
            for entry in problem_library.entries
            if entry.status == "ready"
        }
        if problem_library is not None
        else {}
    )
    actionable_count = sum(len(cluster.cases) for cluster in brief.clusters)
    deferred_count = sum(len(cluster.cases) for cluster in deferred_clusters)
    failing = actionable_count + deferred_count
    parts = [
        "## 诊断概要",
        f"评测版本：{brief.evidence_candidate_id}  总分：{brief.total_score:.4f}",
        f"比较基线：{baseline_id}  总分：{baseline_score}",
        f"未通过 Case {failing} 个；"
        f"退步 Case {len(brief.regressed_cases)} 个；"
        f"当前通过 {len(brief.passing_case_ids)} 个（回归保护对象）。",
        f"本轮可处理：{actionable_count} 个 Case / {len(brief.clusters)} 个簇。",
        f"本轮暂缓：{deferred_count} 个 Case / {len(deferred_clusters)} 个簇。",
        "",
        "## 失败簇（按优先级：regression > total_failure > partial）",
    ]
    if not brief.clusters:
        parts.append(
            f"本轮无可处理失败簇，仍有 {deferred_count} 个失败 Case "
            "在问题库积累中，本轮暂缓处理。"
            if deferred_clusters
            else "无（全部通过）"
        )
    for cluster in brief.clusters:
        ready_note = (
            f"（问题库已积累 {ready_counts[cluster.signature]} 个 case）"
            if cluster.signature in ready_counts
            else ""
        )
        parts.append(
            f"### {cluster.cluster_id} · {cluster.severity.value} · "
            f"签名 `{cluster.signature}`{ready_note}"
        )
        for case in cluster.cases:
            parts.append(f"- {_case_score_change(case)}")
    parts.extend(["", "## 本轮退步 Case"])
    if brief.regressed_cases:
        for regressed_case in brief.regressed_cases:
            parts.append(
                f"- {regressed_case.id}：{regressed_case.previous_score:.4g} "
                f"→ {regressed_case.score:.4g}"
            )
    else:
        parts.append("无")
    parts.extend(["", "## 当前通过 Case（回归保护对象）"])
    parts.append("、".join(brief.passing_case_ids) if brief.passing_case_ids else "无")
    return "\n".join(parts)


def render_deferred_section(
    deferred: tuple[FailureCluster, ...],
    library: ProblemLibrary,
    *,
    min_cases: int = 2,
) -> str:
    """把本轮延后的单 case 簇渲染为问题库积累进度小节。"""
    by_signature = {entry.signature: entry for entry in library.entries}
    lines = ["## 问题库（单 case 簇积累中，本轮不处理）"]
    if not deferred:
        lines.append("无")
    for cluster in deferred:
        entry = by_signature.get(cluster.signature)
        count = len(entry.case_ids) if entry is not None else len(cluster.cases)
        last_seen = entry.last_seen_candidate_id if entry is not None else "未知版本"
        lines.append(
            f"- 签名 `{cluster.signature}` · {cluster.severity.value} · "
            f"已积累 {count}/{min_cases} 个 case（最近出现于 {last_seen}）"
        )
    return "\n".join(lines)


def _case_score_change(case: DiagnosedCase) -> str:
    """展示单题前后分数，显式标记缺失的历史评分。"""
    previous = (
        f"{case.previous_score:.4g}"
        if case.previous_score is not None
        else "无历史分数"
    )
    return f"{case.id}：{previous} → {case.score:.4g}"


def _diagnose_case(case: CaseEvaluation) -> DiagnosedCase:
    """提取失败级别与聚类签名，优先采用评分器提供的类别。"""
    return DiagnosedCase(
        id=case.id,
        score=case.score,
        previous_score=case.previous_score,
        severity=_severity_of(case),
        # Evaluator 提供的 Case 不变失败类别优先于 feedback 文本启发式。
        signature=case.failure_kind
        or _failure_signature(_select_failure_line(case.feedback)),
    )


def _severity_of(case: CaseEvaluation) -> CaseSeverity:
    """按通过转失败、零分和部分得分确定失败级别。"""
    if (
        case.previous_score is not None
        and case.previous_score >= _PASS_THRESHOLD
        and case.score < _PASS_THRESHOLD
    ):
        return CaseSeverity.REGRESSION
    if case.score == 0:
        return CaseSeverity.TOTAL_FAILURE
    return CaseSeverity.PARTIAL


def _cluster_cases(cases: list[DiagnosedCase]) -> tuple[FailureCluster, ...]:
    """按失败签名聚类并稳定排序；无法归类的 Case 各自独立。"""
    by_signature: dict[str, list[DiagnosedCase]] = {}
    for case in cases:
        # 无法归类的 Case 不互相合并：共享 unclassified 签名只表示
        # 「规则层无法归因」，捆成一个多 Case 簇会伪装成同因失败。
        key = (
            f"{_UNCLASSIFIED}:{case.id}"
            if case.signature == _UNCLASSIFIED
            else case.signature
        )
        by_signature.setdefault(key, []).append(case)
    groups = sorted(
        by_signature.items(),
        key=lambda item: (
            min(_SEVERITY_RANK[case.severity] for case in item[1]),
            -len(item[1]),
            item[0],
        ),
    )
    return tuple(
        FailureCluster(
            cluster_id=f"cluster_{index}",
            signature=group[0].signature,
            severity=min(
                (case.severity for case in group),
                key=_SEVERITY_RANK.__getitem__,
            ),
            cases=tuple(group),
        )
        for index, (_key, group) in enumerate(groups, start=1)
    )


def _select_failure_line(feedback: str) -> str:
    """优先选取带失败提示的反馈行，否则使用首个非空行。"""
    lines = [line.strip() for line in _LINE_SPLIT_RE.split(feedback) if line.strip()]
    if not lines:
        return ""
    for line in lines:
        if _FAILURE_HINT_RE.search(line):
            return line
    return lines[0]


def _failure_signature(line: str) -> str:
    """把一条失败反馈归一化为跨 Case 可聚类的签名。

    移除引号片段、路径、数字等 Case 特有内容，防止签名退化为单 Case 唯一。
    归一化结果过短或只剩标量残片时不携带失败语义，按无法归类处理。
    """
    if not line:
        return _UNCLASSIFIED
    text = line.lower()
    text = _QUOTED_RE.sub(" ", text)
    text = _PATH_TOKEN_RE.sub(" ", text)
    text = _DIGITS_RE.sub(" ", text)
    text = _NON_WORD_RE.sub("_", text)
    text = _UNDERSCORES_RE.sub("_", text.strip("_"))
    tokens = text.split("_")
    if len(text) < _SIGNATURE_MIN_LENGTH or all(
        token in _STOP_FRAGMENT_TOKENS for token in tokens
    ):
        return _UNCLASSIFIED
    return text[:_SIGNATURE_MAX_LENGTH].strip("_") or _UNCLASSIFIED


__all__ = [
    "CaseSeverity",
    "DiagnosedCase",
    "DiagnosisBrief",
    "FailureCluster",
    "ProblemLibrary",
    "ProblemLibraryEntry",
    "RegressedCase",
    "build_diagnosis_brief",
    "render_brief_markdown",
    "render_deferred_section",
    "split_actionable_clusters",
    "update_problem_library",
]
