"""evaluation.models 严格模型校验测试。"""

import pytest
from pydantic import ValidationError

from evaluation.models import (
    CaseOutcome,
    CaseScore,
    DimScore,
    EvalCase,
    EvalReport,
    Expected,
    RuleResult,
    ToolCallRecord,
)


def test_expected_defaults_all_empty() -> None:
    expected = Expected()
    assert expected.key_points == []
    assert expected.must_refuse is False
    assert expected.max_latency_seconds is None
    assert expected.max_total_tokens is None


def test_eval_case_minimal_and_defaults() -> None:
    case = EvalCase.model_validate({"id": "c1", "input": "你好"})
    assert case.expected is None
    assert case.rules is None
    assert case.dims is None
    assert case.tags == []
    assert case.meta == {}


def test_eval_case_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        EvalCase.model_validate({"id": "c1", "input": "x", "bogus": 1})


def test_eval_case_rejects_empty_id() -> None:
    with pytest.raises(ValidationError):
        EvalCase.model_validate({"id": "", "input": "x"})


def test_case_outcome_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        CaseOutcome(case_id="c1", total_tokens=-1)


def test_dim_score_range_enforced() -> None:
    DimScore(dimension="faithfulness", score=0.5, weight=1.0)
    DimScore(dimension="faithfulness", score=None, error="x")
    with pytest.raises(ValidationError):
        DimScore(dimension="faithfulness", score=1.5)
    with pytest.raises(ValidationError):
        DimScore(dimension="faithfulness", score=-0.1)


def test_case_score_and_report_roundtrip() -> None:
    score = CaseScore(
        case_id="c1",
        rules=[RuleResult(rule="forbidden_words", passed=True)],
        dims=[DimScore(dimension="faithfulness", score=0.8, weight=1.0)],
        final_score=0.8,
    )
    report = EvalReport(
        run_id="run-1",
        task_id="t1",
        agent_id="a1",
        dataset="smoke",
        case_count=1,
        failed_count=0,
        dim_averages={"faithfulness": 0.8},
        final_score=0.8,
        cases=[score],
    )
    restored = EvalReport.model_validate_json(report.model_dump_json())
    assert restored == report


def test_tool_call_record_arguments_typed() -> None:
    record = ToolCallRecord(name="kb_search", arguments={"query": "保养"})
    assert record.arguments["query"] == "保养"
