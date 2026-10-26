"""通用评测 HTTP 路由（/v1/evaluation）合约测试。"""

import json
import time
from collections.abc import Mapping
from pathlib import Path

from evaluation.models import EvalReport
from evaluation.runner import CaseCompletedCallback
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.errors import register_error_handlers
from server.platform.evaluation.routes import EvaluationDeps, create_evaluation_router
from server.platform.evaluation.runner import EvaluationRunRegistry
from server.platform.evaluation.store import EvaluationStore

_CONFIG = """agent_id: fake-agent
dataset: main
judge_llm:
  model: openai/test-model
  api_key: test-key
  base_url: http://127.0.0.1:9/v1
"""


def _write_task(root: Path, task_id: str = "task_one") -> Path:
    task_dir = root / "eval_tasks" / task_id
    (task_dir / "datasets").mkdir(parents=True)
    (task_dir / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (task_dir / "datasets" / "main.json").write_text(
        json.dumps([{"id": "c1", "input": "你好"}]),
        encoding="utf-8",
    )
    return task_dir


def _fake_run_fn(
    task_dir: Path,
    *,
    run_id: str,
    environ: Mapping[str, str],
    on_case_completed: CaseCompletedCallback | None = None,
) -> EvalReport:
    del environ
    from evaluation.models import CaseOutcome

    if on_case_completed is not None:
        on_case_completed("c1", CaseOutcome(case_id="c1", reply="ok"))
    report = EvalReport(
        run_id=run_id,
        task_id=Path(task_dir).name,
        agent_id="fake-agent",
        dataset="main",
        case_count=1,
        failed_count=0,
        dim_averages={"faithfulness": 1.0},
        final_score=1.0,
    )
    (Path(task_dir) / "runs" / run_id / "report.json").write_text(
        report.model_dump_json(),
        encoding="utf-8",
    )
    return report


def _client(root: Path) -> TestClient:
    store = EvaluationStore(tasks_root=root / "eval_tasks")
    registry = EvaluationRunRegistry(store=store, run_fn=_fake_run_fn)
    app = FastAPI()
    app.include_router(
        create_evaluation_router(EvaluationDeps(store=store, registry=registry))
    )
    register_error_handlers(app)
    return TestClient(app)


def _wait_terminal(
    client: TestClient,
    run_id: str,
    timeout: float = 10,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload: dict[str, object] = client.get(f"/v1/evaluation/runs/{run_id}").json()[
            "run"
        ]
        if payload["status"] in ("succeeded", "failed"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"Run 未在 {timeout}s 内结束：{run_id}")


def test_list_and_read_tasks(tmp_path: Path) -> None:
    _write_task(tmp_path)
    client = _client(tmp_path)
    tasks = client.get("/v1/evaluation/tasks").json()["tasks"]
    assert [task["task_id"] for task in tasks] == ["task_one"]
    assert tasks[0]["valid"] is True
    assert tasks[0]["case_count"] == 1
    detail = client.get("/v1/evaluation/tasks/task_one").json()
    assert detail["task"]["agent_id"] == "fake-agent"
    assert detail["runs"] == []
    assert client.get("/v1/evaluation/tasks/missing").status_code == 404
    assert client.get("/v1/evaluation/tasks/Bad-Id").status_code == 400


def test_run_lifecycle_and_report(tmp_path: Path) -> None:
    _write_task(tmp_path)
    client = _client(tmp_path)
    started = client.post("/v1/evaluation/tasks/task_one/runs")
    assert started.status_code == 201
    run_id = started.json()["run"]["run_id"]
    final = _wait_terminal(client, run_id)
    assert final["status"] == "succeeded"
    assert final["final_score"] == 1.0
    assert final["completed_cases"] == 1
    report = client.get(f"/v1/evaluation/runs/{run_id}/report")
    assert report.status_code == 200
    assert report.json()["run_id"] == run_id
    assert report.json()["case_count"] == 1
    detail = client.get("/v1/evaluation/tasks/task_one").json()
    assert [run["run_id"] for run in detail["runs"]] == [run_id]


def test_run_not_found_and_report_gating(tmp_path: Path) -> None:
    _write_task(tmp_path)
    client = _client(tmp_path)
    assert client.get("/v1/evaluation/runs/run-missing").status_code == 404
    assert client.get("/v1/evaluation/runs/run-missing/report").status_code == 404
    assert client.post("/v1/evaluation/tasks/missing/runs").status_code == 404


def test_compare_runs(tmp_path: Path) -> None:
    _write_task(tmp_path)
    client = _client(tmp_path)
    first = client.post("/v1/evaluation/tasks/task_one/runs").json()["run"]["run_id"]
    _wait_terminal(client, first)
    second = client.post("/v1/evaluation/tasks/task_one/runs").json()["run"]["run_id"]
    _wait_terminal(client, second)
    comparison = client.get(
        f"/v1/evaluation/runs/{second}/compare",
        params={"baseline": first},
    )
    assert comparison.status_code == 200
    payload = comparison.json()
    assert payload["baseline_run_id"] == first
    assert payload["current_run_id"] == second
    assert payload["final_score_delta"] == 0.0
    missing = client.get(
        f"/v1/evaluation/runs/{second}/compare",
        params={"baseline": "run-missing"},
    )
    assert missing.status_code == 404
