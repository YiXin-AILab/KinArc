"""server.platform.evaluation.store 磁盘事实源测试。"""

import json
from pathlib import Path

import pytest
from agentsystem.common.errors import ServiceError
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


def test_list_tasks_summarizes_valid_and_broken(tmp_path: Path) -> None:
    _write_task(tmp_path)
    broken = tmp_path / "eval_tasks" / "broken"
    broken.mkdir(parents=True)
    (broken / "config.yaml").write_text("agent_id: [", encoding="utf-8")
    store = EvaluationStore(tasks_root=tmp_path / "eval_tasks")
    summaries = {item["task_id"]: item for item in store.list_tasks()}
    assert summaries["task_one"]["valid"] is True
    assert summaries["task_one"]["case_count"] == 1
    assert summaries["task_one"]["agent_id"] == "fake-agent"
    assert summaries["broken"]["valid"] is False
    assert summaries["broken"]["error"]


def test_task_dir_validates_id_and_existence(tmp_path: Path) -> None:
    _write_task(tmp_path)
    store = EvaluationStore(tasks_root=tmp_path / "eval_tasks")
    assert store.task_dir("task_one").name == "task_one"
    with pytest.raises(ServiceError) as missing:
        store.task_dir("nope")
    assert missing.value.kind == "not_found"
    with pytest.raises(ServiceError) as invalid:
        store.task_dir("../escape")
    assert invalid.value.kind == "invalid"


def test_run_meta_lifecycle_roundtrip(tmp_path: Path) -> None:
    _write_task(tmp_path)
    store = EvaluationStore(tasks_root=tmp_path / "eval_tasks")
    run_dir = store.create_run_dir("task_one", "run-abc123")
    meta = store.read_meta(run_dir)
    assert meta["status"] == "pending"
    assert meta["completed_cases"] == 0
    meta["status"] = "succeeded"
    meta["final_score"] = 0.75
    store.write_meta(run_dir, meta)
    task_id, found = store.find_run("run-abc123")
    assert task_id == "task_one"
    assert store.read_meta(found)["final_score"] == 0.75
    with pytest.raises(ServiceError) as not_found:
        store.find_run("run-missing")
    assert not_found.value.kind == "not_found"
    with pytest.raises(ServiceError):
        store.find_run("bad id!")


def test_read_report_missing_and_invalid(tmp_path: Path) -> None:
    _write_task(tmp_path)
    store = EvaluationStore(tasks_root=tmp_path / "eval_tasks")
    store.create_run_dir("task_one", "run-noreport")
    with pytest.raises(ServiceError) as missing:
        store.read_report("run-noreport")
    assert missing.value.code == "evaluation_report_not_found"
    run_dir = store.task_dir("task_one") / "runs" / "run-noreport"
    (run_dir / "report.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ServiceError) as invalid:
        store.read_report("run-noreport")
    assert invalid.value.code == "evaluation_report_unreadable"
