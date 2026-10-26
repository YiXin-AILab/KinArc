"""evaluation.runner 端到端测试（fake executor + fake judge）。"""

import json
from pathlib import Path
from threading import Event

import pytest

from evaluation.models import (
    CaseOutcome,
    DimScore,
    EvalCase,
    EvalReport,
    ToolCallRecord,
)
from evaluation.runner import load_dataset, run_evaluation

_CONFIG = """agent_id: fake-agent
server_base_url: http://127.0.0.1:9
dataset: main
judge_llm:
  model: openai/test-model
  api_key: test-key
  base_url: http://127.0.0.1:9/v1
max_case_concurrency: 2
judge_concurrency: 2
"""


def _write_task(
    root: Path,
    cases: list[dict[str, object]],
    *,
    config: str = _CONFIG,
) -> Path:
    task_dir = root / "eval_tasks" / "task_one"
    (task_dir / "datasets").mkdir(parents=True)
    (task_dir / "config.yaml").write_text(config, encoding="utf-8")
    (task_dir / "datasets" / "main.json").write_text(
        json.dumps(cases, ensure_ascii=False),
        encoding="utf-8",
    )
    return task_dir


class FakeExecutor:
    """按 case id 返回预置 Outcome 的执行器。"""

    def __init__(self, outcomes: dict[str, CaseOutcome]) -> None:
        self._outcomes = outcomes

    def execute(self, case: EvalCase, stop_event: Event) -> CaseOutcome:
        del stop_event
        return self._outcomes[case.id]


class FakeJudge:
    """返回固定维度分的裁判。"""

    def __init__(self, score: float = 0.8) -> None:
        self._score = score

    async def score_cases(
        self,
        items: list[tuple[EvalCase, CaseOutcome]],
    ) -> list[list[DimScore]]:
        return [
            [DimScore(dimension="faithfulness", score=self._score, weight=1.0)]
            for _ in items
        ]


def test_run_evaluation_persists_report(tmp_path: Path) -> None:
    task_dir = _write_task(
        tmp_path,
        [
            {
                "id": "hit",
                "input": "价格？",
                "expected": {"gold_doc_ids": ["d1"], "required_tools": ["kb_search"]},
            },
            {
                "id": "miss",
                "input": "价格？",
                "expected": {"gold_doc_ids": ["d2"]},
            },
        ],
    )
    executor = FakeExecutor(
        {
            "hit": CaseOutcome(
                case_id="hit",
                reply="1100 元",
                tool_calls=[ToolCallRecord(name="kb_search")],
                retrieved_doc_ids=["d1"],
                latency_seconds=1.0,
                total_tokens=100,
            ),
            "miss": CaseOutcome(case_id="miss", reply="不知道", retrieved_doc_ids=[]),
        }
    )
    report = run_evaluation(
        task_dir,
        run_id="run-test0001",
        executor=executor,
        judge=FakeJudge(0.8),
    )
    assert isinstance(report, EvalReport)
    assert report.case_count == 2
    assert report.failed_count == 1
    by_id = {case.case_id: case for case in report.cases}
    assert by_id["hit"].failed is False
    assert by_id["hit"].final_score == pytest.approx(0.8)
    assert by_id["miss"].failed is True
    assert by_id["miss"].final_score == 0.0
    assert report.final_score == pytest.approx(0.4)
    assert report.dim_averages == {"faithfulness": pytest.approx(0.8)}
    run_dir = task_dir / "runs" / "run-test0001"
    persisted = EvalReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )
    assert persisted == report
    outcomes = json.loads((run_dir / "outcomes.json").read_text(encoding="utf-8"))
    assert [item["case_id"] for item in outcomes] == ["hit", "miss"]
    assert "test-key" not in (run_dir / "report.json").read_text(encoding="utf-8")
    assert "test-key" not in (run_dir / "outcomes.json").read_text(encoding="utf-8")


def test_run_evaluation_error_outcome_fails_case(tmp_path: Path) -> None:
    task_dir = _write_task(
        tmp_path,
        [{"id": "boom", "input": "x", "expected": {"forbidden": ["坏词"]}}],
    )
    executor = FakeExecutor(
        {"boom": CaseOutcome(case_id="boom", error="TimeoutError: timeout")}
    )
    report = run_evaluation(
        task_dir, executor=executor, judge=FakeJudge(), run_id="run-test0002"
    )
    case = report.cases[0]
    assert case.failed is True
    assert case.final_score == 0.0
    assert case.dims == []
    assert all(not rule.passed for rule in case.rules)


def test_run_evaluation_error_outcome_without_rules_fails_case(
    tmp_path: Path,
) -> None:
    task_dir = _write_task(tmp_path, [{"id": "boom", "input": "x", "rules": []}])
    executor = FakeExecutor(
        {"boom": CaseOutcome(case_id="boom", error="TimeoutError: timeout")}
    )
    report = run_evaluation(
        task_dir, executor=executor, judge=FakeJudge(), run_id="run-test0005"
    )
    case = report.cases[0]
    assert case.failed is True
    assert case.final_score == 0.0
    assert case.rules == []
    assert case.dims == []
    assert report.failed_count == 1
    assert report.final_score == 0.0


def test_run_evaluation_error_outcome_with_rules_fails_case(
    tmp_path: Path,
) -> None:
    task_dir = _write_task(
        tmp_path,
        [{"id": "boom", "input": "x", "expected": {"required_tools": ["kb_search"]}}],
    )
    executor = FakeExecutor(
        {"boom": CaseOutcome(case_id="boom", error="ConnectionError: refused")}
    )
    report = run_evaluation(
        task_dir, executor=executor, judge=FakeJudge(), run_id="run-test0006"
    )
    case = report.cases[0]
    assert case.failed is True
    assert case.final_score == 0.0
    assert case.dims == []
    assert all(not rule.passed for rule in case.rules)


def test_judge_dim_failure_does_not_fail_case(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, [{"id": "c1", "input": "x"}])
    executor = FakeExecutor({"c1": CaseOutcome(case_id="c1", reply="ok")})

    class FailingDimJudge:
        async def score_cases(
            self,
            items: list[tuple[EvalCase, CaseOutcome]],
        ) -> list[list[DimScore]]:
            return [
                [DimScore(dimension="faithfulness", score=None, error="judge down")]
                for _ in items
            ]

    report = run_evaluation(
        task_dir, executor=executor, judge=FailingDimJudge(), run_id="run-test0007"
    )
    case = report.cases[0]
    assert case.failed is False
    assert case.final_score == 1.0
    assert case.dims[0].score is None
    assert report.dim_averages == {}


def test_run_evaluation_progress_callback(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, [{"id": "c1", "input": "x"}])
    completed: list[str] = []
    executor = FakeExecutor({"c1": CaseOutcome(case_id="c1", reply="ok")})
    run_evaluation(
        task_dir,
        executor=executor,
        judge=FakeJudge(),
        run_id="run-test0003",
        on_case_completed=lambda case_id, outcome: completed.append(case_id),
    )
    assert completed == ["c1"]


def test_load_dataset_rejects_missing_and_invalid(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, [{"id": "c1", "input": "x"}])
    with pytest.raises(ValueError, match="数据集不可读"):
        load_dataset(task_dir, "missing")
    (task_dir / "datasets" / "bad.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 数组"):
        load_dataset(task_dir, "bad")


def test_case_without_dims_scores_one_when_rules_pass(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, [{"id": "c1", "input": "x", "dims": []}])
    executor = FakeExecutor({"c1": CaseOutcome(case_id="c1", reply="ok")})

    class EmptyJudge:
        async def score_cases(
            self,
            items: list[tuple[EvalCase, CaseOutcome]],
        ) -> list[list[DimScore]]:
            return [[] for _ in items]

    report = run_evaluation(
        task_dir, executor=executor, judge=EmptyJudge(), run_id="run-test0004"
    )
    assert report.cases[0].final_score == 1.0
    assert report.final_score == 1.0
