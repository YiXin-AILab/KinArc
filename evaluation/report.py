"""评测报告对比：逐 Case 与逐维度 delta 及改善/回归计数。"""

from enum import StrEnum

from agentsystem.common.models import StrictModel
from pydantic import Field

from evaluation.models import EvalReport

_EPSILON = 1e-9


class DeltaStatus(StrEnum):
    """单个指标的对比结论。"""

    IMPROVED = "improved"
    REGRESSED = "regressed"
    UNCHANGED = "unchanged"


class CaseDelta(StrictModel):
    """同一 Case 在两次 Run 间的分数变化。"""

    case_id: str = Field(min_length=1)
    baseline_score: float = Field(ge=0, le=1)
    current_score: float = Field(ge=0, le=1)
    delta: float
    status: DeltaStatus


class ComparisonReport(StrictModel):
    """两次评测 Run 的对比报告。"""

    baseline_run_id: str = Field(min_length=1)
    current_run_id: str = Field(min_length=1)
    case_deltas: list[CaseDelta] = Field(default_factory=list)
    dim_deltas: dict[str, float] = Field(default_factory=dict)
    improved_count: int = Field(ge=0)
    regressed_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    final_score_delta: float


def compare_reports(baseline: EvalReport, current: EvalReport) -> ComparisonReport:
    """比较两次运行的评分，仅对共有用例和共有维度计算差值。

    Args:
        baseline: 基线运行的评测报告。
        current: 当前运行的评测报告。

    Returns:
        当前分数减去基线分数的差值、共有用例的变化计数及整体总分差值。
    """
    baseline_scores = {case.case_id: case.final_score for case in baseline.cases}
    deltas: list[CaseDelta] = []
    for case in current.cases:
        if case.case_id not in baseline_scores:
            continue
        baseline_score = baseline_scores[case.case_id]
        delta = case.final_score - baseline_score
        deltas.append(
            CaseDelta(
                case_id=case.case_id,
                baseline_score=baseline_score,
                current_score=case.final_score,
                delta=delta,
                status=_delta_status(delta),
            )
        )
    dim_deltas = {
        dimension: current_average - baseline.dim_averages[dimension]
        for dimension, current_average in current.dim_averages.items()
        if dimension in baseline.dim_averages
    }
    return ComparisonReport(
        baseline_run_id=baseline.run_id,
        current_run_id=current.run_id,
        case_deltas=deltas,
        dim_deltas=dim_deltas,
        improved_count=sum(1 for item in deltas if item.status is DeltaStatus.IMPROVED),
        regressed_count=sum(
            1 for item in deltas if item.status is DeltaStatus.REGRESSED
        ),
        unchanged_count=sum(
            1 for item in deltas if item.status is DeltaStatus.UNCHANGED
        ),
        final_score_delta=current.final_score - baseline.final_score,
    )


def _delta_status(delta: float) -> DeltaStatus:
    if delta > _EPSILON:
        return DeltaStatus.IMPROVED
    if delta < -_EPSILON:
        return DeltaStatus.REGRESSED
    return DeltaStatus.UNCHANGED


__all__ = [
    "CaseDelta",
    "ComparisonReport",
    "DeltaStatus",
    "compare_reports",
]
