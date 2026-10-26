"""Evolution 任务磁盘存储（server.platform.evolution.store）单元测试。"""

import json
from pathlib import Path

import pytest
import yaml
from agentsystem.common.errors import ServiceError
from server.platform.evolution.store import EvolutionStore, TaskOverrides
from tests.unit.api.evolution_helpers import write_demo_agent, write_runnable_task


def _store(root: Path) -> EvolutionStore:
    return EvolutionStore(
        project_root=root,
        tasks_root=root / "evolution_tasks",
    )


def _write_candidate_artifacts(task_dir: Path) -> Path:
    candidate = task_dir / ".evolution" / "candidates" / "candidate-20260928T000000"
    (candidate / "train").mkdir(parents=True)
    (candidate / "validation").mkdir(parents=True)
    (candidate / "changelog.md").write_text(
        "## 评测版本：seed\n## 迭代版本：candidate-20260928T000000\n",
        encoding="utf-8",
    )
    execution = {
        "cases": [
            {
                "id": "train-case-1",
                "result": "ok",
                "tracelog": None,
                "metadata": {"total_tokens": 10, "duration_seconds": 0.5},
            }
        ]
    }
    evaluation = {
        "total_score": 0.5,
        "cases": [
            {
                "id": "train-case-1",
                "result": "ok",
                "tracelog": None,
                "metadata": {"total_tokens": 4, "duration_seconds": 0.1},
                "expected": None,
                "score": 1.0,
                "feedback": "ok",
            }
        ],
    }
    (candidate / "train" / "execution.json").write_text(
        json.dumps(execution), encoding="utf-8"
    )
    (candidate / "train" / "evaluation.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    (candidate / "validation" / "evaluation.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    return candidate


def test_list_tasks_empty_when_root_missing(tmp_path: Path) -> None:
    assert _store(tmp_path).list_tasks() == ()


def test_list_tasks_summarizes_valid_and_invalid(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    broken = tmp_path / "evolution_tasks" / "broken"
    broken.mkdir()
    (broken / "config.yaml").write_text("objective: 1\n", encoding="utf-8")
    tasks = {item.task_id: item for item in _store(tmp_path).list_tasks()}
    assert tasks["tpl"].valid is True
    assert tasks["tpl"].max_iterations == 1
    assert tasks["broken"].valid is False
    assert tasks["broken"].error is not None


def test_list_templates_requires_entries(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    write_runnable_task(tmp_path, "no_entries", with_entries=False)
    templates = _store(tmp_path).list_templates()
    assert [item.task_id for item in templates] == ["tpl"]
    assert templates[0].dataset_case_counts == {
        "train": 2,
        "validation": 2,
        "test": 2,
    }


def test_list_templates_resolves_custom_entry_paths(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    smoke = write_runnable_task(tmp_path, "smoke", with_entries=False)
    config = yaml.safe_load((smoke / "config.yaml").read_text(encoding="utf-8"))
    config["executor"] = "../tpl/executor.py"
    config["evaluator"] = "../tpl/evaluator.py"
    (smoke / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    templates = _store(tmp_path).list_templates()
    assert [item.task_id for item in templates] == ["smoke", "tpl"]
    summary = _store(tmp_path).create_task(
        template_id="smoke",
        task_id="smoke_clone",
        overrides=TaskOverrides(objective="Cloned smoke.", max_iterations=0),
    )
    assert summary.task_id == "smoke_clone"
    cloned = yaml.safe_load(
        (tmp_path / "evolution_tasks" / "smoke_clone" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert cloned["executor"] == "../tpl/executor.py"


def test_create_task_clones_template_with_overrides(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    write_demo_agent(tmp_path, "other_agent")
    summary = _store(tmp_path).create_task(
        template_id="tpl",
        task_id="clone_one",
        overrides=TaskOverrides(
            objective="New objective.",
            initial_target="application/other_agent",
            max_iterations=3,
            settings={"agent_id": "other-agent"},
        ),
    )
    assert summary.task_id == "clone_one"
    assert summary.max_iterations == 3
    clone_dir = tmp_path / "evolution_tasks" / "clone_one"
    assert (clone_dir / "executor.py").is_file()
    assert not (clone_dir / ".evolution").exists()
    config = yaml.safe_load((clone_dir / "config.yaml").read_text(encoding="utf-8"))
    assert config["objective"] == "New objective."
    assert config["initial_target"] == "../../application/other_agent"
    assert config["settings"]["agent_id"] == "other-agent"


def test_create_task_rejects_conflict_and_bad_target(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    store = _store(tmp_path)
    store.create_task(
        template_id="tpl",
        task_id="clone_one",
        overrides=TaskOverrides(objective="x"),
    )
    with pytest.raises(ServiceError, match="任务已存在"):
        store.create_task(
            template_id="tpl",
            task_id="clone_one",
            overrides=TaskOverrides(objective="x"),
        )
    with pytest.raises(ServiceError, match="initial_target 目录不存在"):
        store.create_task(
            template_id="tpl",
            task_id="clone_two",
            overrides=TaskOverrides(initial_target="application/missing"),
        )
    with pytest.raises(ServiceError, match="任务 ID 不合法"):
        store.create_task(
            template_id="tpl",
            task_id="../escape",
            overrides=TaskOverrides(objective="x"),
        )


def test_task_detail_rebuilds_candidates(tmp_path: Path) -> None:
    task_dir = write_runnable_task(tmp_path, "tpl")
    _write_candidate_artifacts(task_dir)
    detail = _store(tmp_path).task_detail("tpl")
    assert detail.task_id == "tpl"
    assert len(detail.candidates) == 1
    candidate = detail.candidates[0]
    assert candidate.candidate_id == "candidate-20260928T000000"
    assert candidate.is_rollback is False
    assert candidate.train is not None
    assert candidate.train.total_score == 0.5
    assert candidate.train.correct == 1
    assert candidate.train.total_tokens == 4
    assert candidate.train.cases[0].case_id == "train-case-1"
    assert candidate.validation is not None
    assert candidate.test is None


def test_changelog_read_lazily(tmp_path: Path) -> None:
    task_dir = write_runnable_task(tmp_path, "tpl")
    _write_candidate_artifacts(task_dir)
    store = _store(tmp_path)
    text = store.changelog("tpl", "candidate-20260928T000000")
    assert "迭代版本" in text
    with pytest.raises(ServiceError, match="候选 changelog 不存在"):
        store.changelog("tpl", "candidate-20260929T000000")
    with pytest.raises(ServiceError, match="候选 ID 不合法"):
        store.changelog("tpl", "../../etc")


def test_role_result_cache_hits_on_repeat(tmp_path: Path) -> None:
    task_dir = write_runnable_task(tmp_path, "tpl")
    _write_candidate_artifacts(task_dir)
    store = _store(tmp_path)
    first = store.task_detail("tpl")
    assert store._role_cache, "首次读取后应写入缓存"
    second = store.task_detail("tpl")
    assert first.candidates[0].train == second.candidates[0].train


def test_task_dir_rejects_unknown_and_invalid_ids(tmp_path: Path) -> None:
    write_runnable_task(tmp_path, "tpl")
    store = _store(tmp_path)
    with pytest.raises(ServiceError, match="任务不存在"):
        store.task_dir("missing")
    with pytest.raises(ServiceError, match="任务 ID 不合法"):
        store.task_dir("Bad-Id")
