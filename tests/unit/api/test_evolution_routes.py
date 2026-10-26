"""Evolution 自进化任务 HTTP 路由（/v1/evolution）合约测试。"""

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.errors import register_error_handlers
from server.platform.evolution.routes import EvolutionDeps, create_evolution_router
from server.platform.evolution.runner import EvolutionRunRegistry, RunStatus
from server.platform.evolution.store import EvolutionStore
from tests.unit.api.evolution_helpers import (
    FakeImprover,
    fake_improver_factory,
    write_runnable_task,
)


def _client(root: Path, improver: FakeImprover | None = None) -> TestClient:
    store = EvolutionStore(
        project_root=root,
        tasks_root=root / "evolution_tasks",
    )
    registry = EvolutionRunRegistry(
        project_root=root,
        store=store,
        improver_factory=fake_improver_factory(improver or FakeImprover()),
    )
    app = FastAPI()
    app.include_router(
        create_evolution_router(EvolutionDeps(store=store, registry=registry))
    )
    register_error_handlers(app)
    client = TestClient(app)
    client.registry = registry  # type: ignore[attr-defined]
    return client


def _wait_run_terminal(
    client: TestClient,
    run_id: str,
    timeout: float = 15,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload: dict[str, object] = client.get(f"/v1/evolution/runs/{run_id}").json()[
            "run"
        ]
        if payload["status"] in ("completed", "failed", "stopped"):
            return payload
        time.sleep(0.1)
    raise AssertionError(f"Run 未在 {timeout}s 内结束：{run_id}")


def test_list_tasks_and_templates(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    client = _client(tmp_path)
    tasks = client.get("/v1/evolution/tasks").json()["tasks"]
    assert [item["task_id"] for item in tasks] == ["tpl"]
    assert tasks[0]["valid"] is True
    assert tasks[0]["active_run_id"] is None
    templates = client.get("/v1/evolution/templates").json()["templates"]
    assert [item["task_id"] for item in templates] == ["tpl"]
    assert templates[0]["dataset_case_counts"]["train"] == 2


def test_create_task_and_conflict(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    client = _client(tmp_path)
    response = client.post(
        "/v1/evolution/tasks",
        json={
            "template_id": "tpl",
            "task_id": "clone_one",
            "objective": "New objective.",
            "max_iterations": 2,
        },
    )
    assert response.status_code == 201
    assert response.json()["task"]["task_id"] == "clone_one"
    detail = client.get("/v1/evolution/tasks/clone_one").json()
    assert detail["objective"] == "New objective."
    assert detail["max_iterations"] == 2
    assert detail["candidates"] == []
    conflict = client.post(
        "/v1/evolution/tasks",
        json={"template_id": "tpl", "task_id": "clone_one"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "evolution_task_exists"


def test_task_not_found_and_invalid_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/v1/evolution/tasks/missing").status_code == 404
    response = client.post(
        "/v1/evolution/tasks",
        json={"template_id": "tpl", "task_id": "Bad-Id"},
    )
    assert response.status_code == 422


def test_run_lifecycle_and_sse_replay(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    client = _client(tmp_path)
    started = client.post("/v1/evolution/tasks/tpl/runs")
    assert started.status_code == 201
    run_id = started.json()["run"]["run_id"]
    final = _wait_run_terminal(client, run_id)
    assert final["status"] == "completed"
    assert final["stop_reason"] == "iteration_limit"
    assert final["best_candidate_id"] is not None
    response = client.get(f"/v1/evolution/runs/{run_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: progress" in response.text
    assert '"event":"run_started"' in response.text
    assert '"event":"run_completed"' in response.text
    detail = client.get("/v1/evolution/tasks/tpl").json()
    assert len(detail["candidates"]) >= 2
    seed = detail["candidates"][0]
    assert seed["train"]["total_score"] == 1.0
    stop = client.post(f"/v1/evolution/runs/{run_id}/stop")
    assert stop.status_code == 409


def test_run_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/v1/evolution/runs/run-missing").status_code == 404
    assert client.get("/v1/evolution/runs/run-missing/events").status_code == 404


def test_start_run_conflict_while_active(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    client = _client(tmp_path, FakeImprover(delay_seconds=30))
    first = client.post("/v1/evolution/tasks/tpl/runs")
    assert first.status_code == 201
    run_id = first.json()["run"]["run_id"]
    registry: EvolutionRunRegistry = client.registry  # type: ignore[attr-defined]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if registry.get(run_id).status is RunStatus.RUNNING:
            break
        time.sleep(0.05)
    conflict = client.post("/v1/evolution/tasks/tpl/runs")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "evolution_run_active"
    client.post(f"/v1/evolution/runs/{run_id}/stop")
    final = _wait_run_terminal(client, run_id)
    assert final["status"] == "stopped"


def test_candidate_changelog_endpoint(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    client = _client(tmp_path)
    started = client.post("/v1/evolution/tasks/tpl/runs")
    run_id = started.json()["run"]["run_id"]
    _wait_run_terminal(client, run_id)
    detail = client.get("/v1/evolution/tasks/tpl").json()
    candidate_id = detail["candidates"][-1]["candidate_id"]
    assert "changelog_markdown" not in detail["candidates"][-1]
    response = client.get(
        f"/v1/evolution/tasks/tpl/candidates/{candidate_id}/changelog"
    )
    assert response.status_code == 200
    assert "迭代版本" in response.json()["changelog_markdown"]
    missing = client.get(
        f"/v1/evolution/tasks/tpl/candidates/{candidate_id}9/changelog"
    )
    assert missing.status_code == 400
