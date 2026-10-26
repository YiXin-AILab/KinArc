"""evaluation.report 对比报告测试。"""

from evaluation.models import CaseScore, DimScore, EvalReport
from evaluation.report import DeltaStatus, compare_reports


def _report(
    run_id: str,
    scores: dict[str, float],
    dim_averages: dict[str, float],
) -> EvalReport:
    cases = [
        CaseScore(
            case_id=case_id,
            final_score=score,
            failed=score == 0.0,
        )
        for case_id, score in scores.items()
    ]
    final = sum(scores.values()) / len(scores) if scores else 0.0
    return EvalReport(
        run_id=run_id,
        task_id="t1",
        agent_id="a1",
        dataset="main",
        case_count=len(cases),
        failed_count=sum(1 for case in cases if case.failed),
        dim_averages=dim_averages,
        final_score=final,
        cases=cases,
    )


def test_compare_reports_counts_and_deltas() -> None:
    baseline = _report(
        "run-base",
        {"c1": 0.5, "c2": 0.8, "c3": 0.6},
        {"faithfulness": 0.6, "compliance": 0.9},
    )
    current = _report(
        "run-curr",
        {"c1": 0.9, "c2": 0.4, "c3": 0.6},
        {"faithfulness": 0.8, "compliance": 0.9},
    )
    comparison = compare_reports(baseline, current)
    assert comparison.baseline_run_id == "run-base"
    assert comparison.improved_count == 1
    assert comparison.regressed_count == 1
    assert comparison.unchanged_count == 1
    by_id = {delta.case_id: delta for delta in comparison.case_deltas}
    assert by_id["c1"].status is DeltaStatus.IMPROVED
    assert by_id["c1"].delta == 0.9 - 0.5
    assert by_id["c2"].status is DeltaStatus.REGRESSED
    assert by_id["c3"].status is DeltaStatus.UNCHANGED
    assert comparison.dim_deltas["faithfulness"] == 0.8 - 0.6
    assert comparison.dim_deltas["compliance"] == 0.0
    assert comparison.final_score_delta == (current.final_score - baseline.final_score)


def test_compare_reports_skips_cases_missing_in_baseline() -> None:
    baseline = _report("run-base", {"c1": 0.5}, {})
    current = _report("run-curr", {"c1": 0.5, "c2": 1.0}, {})
    comparison = compare_reports(baseline, current)
    assert [delta.case_id for delta in comparison.case_deltas] == ["c1"]


def test_dim_score_model_roundtrip_for_report() -> None:
    dim = DimScore(dimension="accuracy", score=0.3, weight=2.0, rationale="部分覆盖")
    assert dim.weight == 2.0
