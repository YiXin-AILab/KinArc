"""Evolution Task 配置测试。"""

from pathlib import Path

import pytest
from evolution.task import TaskConfig, load_task_config
from evolution.workspace import WorkspaceAccess
from pydantic import ValidationError


def _write_task_config(
    root: Path,
    *,
    entries: tuple[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / "target").mkdir()
    (root / "executor.py").touch()
    (root / "evaluator.py").touch()
    (root / "datasets").mkdir()
    for role in ("train", "validation", "test"):
        (root / "datasets" / f"{role}.json").write_text("[]")
    entry_config = ""
    if entries is not None:
        entry_config = f"executor: {entries[0]}\nevaluator: {entries[1]}\n"
    (root / "config.yaml").write_text(
        entry_config
        + """objective: improve
initial_target: target
workspace: {read_only: [], inaccessible: []}
settings:
  model: ${MODEL}
""",
        encoding="utf-8",
    )
    return root


def test_load_task_config_uses_default_entries(tmp_path: Path) -> None:
    """未配置入口时使用当前 Task 的固定文件。"""
    task = _write_task_config(tmp_path / "tasks" / "task")

    loaded = load_task_config(task, environ={"MODEL": "openai/test"})

    assert loaded.root == task
    assert loaded.initial_target == task / "target"
    assert loaded.executor == task / "executor.py"
    assert loaded.evaluator == task / "evaluator.py"
    assert loaded.improver_timeout_seconds == 1200
    assert loaded.max_retries == 3
    assert loaded.max_case_concurrency == 1
    assert loaded.single_case_min_cases == 2
    assert loaded.retry_delay_seconds == 2
    assert loaded.settings == {"model": "openai/test"}
    assert loaded.rollback_enabled is True
    assert loaded.exploration_patience == 3
    assert loaded.rollback_changelog_source == "best"
    assert {role.value: path.name for role, path in loaded.datasets.items()} == {
        role: f"{role}.json" for role in ("train", "validation", "test")
    }


@pytest.mark.parametrize(
    "field,value",
    [("exploration_patience", value) for value in (0, -1, True, 2.0, "2")]
    + [("rollback_enabled", value) for value in (0, 1, "true")]
    + [("rollback_changelog_source", "unknown")],
)
def test_rollback_configuration_rejects_invalid_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(
            {
                "objective": "test",
                "initial_target": "target",
                "workspace": {},
                "rollback_enabled": False,
                field: value,
            }
        )


def test_rollback_configuration_is_resolved(tmp_path: Path) -> None:
    task = _write_task_config(tmp_path / "task")
    path = task / "config.yaml"
    path.write_text(
        path.read_text() + "rollback_enabled: false\nexploration_patience: 2\n"
        "rollback_changelog_source: latest\n"
    )
    config = load_task_config(task, environ={"MODEL": "test"})
    assert config.rollback_enabled is False
    assert config.exploration_patience == 2
    assert config.rollback_changelog_source == "latest"


@pytest.mark.parametrize(
    "path",
    ["missing.csv", "target", "../../outside.csv", "linked.csv", "/tmp/absolute.csv"],
)
def test_dataset_path_is_existing_local_file(tmp_path: Path, path: str) -> None:
    task = _write_task_config(tmp_path / "tasks" / "task")
    (tmp_path / "outside.csv").write_text("data")
    (task / "linked.csv").symlink_to(tmp_path / "outside.csv")
    config = task / "config.yaml"
    config.write_text(
        config.read_text()
        + f"datasets: {{train: {path}, validation: datasets/validation.json, "
        + "test: datasets/test.json}\n"
    )
    with pytest.raises(ValueError, match="Dataset"):
        load_task_config(task, environ={"MODEL": "test"})


@pytest.mark.parametrize(
    "mapping",
    [{"train": "a.csv"}, {"train": "a", "validation": "b", "test": "c", "other": "d"}],
)
def test_dataset_mapping_requires_exact_roles(mapping: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(
            {
                "objective": "test",
                "initial_target": "target",
                "workspace": {},
                "datasets": mapping,
            }
        )


def test_custom_csv_dataset_paths(tmp_path: Path) -> None:
    task = _write_task_config(tmp_path / "task")
    for role in ("train", "validation", "test"):
        (task / f"{role}.csv").write_text("case_key,value\na,1\n")
    config = task / "config.yaml"
    config.write_text(
        config.read_text()
        + "datasets: {train: train.csv, validation: validation.csv, test: test.csv}\n"
    )
    loaded = load_task_config(task, environ={"MODEL": "test"})
    assert all(path.suffix == ".csv" for path in loaded.datasets.values())


@pytest.mark.parametrize("value", ["3", "1", "0", "-2", "true", '"2"', "1.5"])
def test_load_task_config_validates_single_case_min_cases(
    tmp_path: Path, value: str
) -> None:
    """单 case 簇延后阈值只接受不小于 2 的整数，默认值为 2。"""
    task = _write_task_config(tmp_path / "task")
    config_path = task / "config.yaml"
    config_path.write_text(
        config_path.read_text() + f"single_case_min_cases: {value}\n",
        encoding="utf-8",
    )
    if value == "3":
        loaded = load_task_config(task, environ={"MODEL": "test"})
        assert loaded.single_case_min_cases == 3
    else:
        with pytest.raises(ValidationError, match="single_case_min_cases"):
            load_task_config(task, environ={"MODEL": "test"})


@pytest.mark.parametrize("value", ["2", "0", "-1", "true", '"2"', "1.5"])
def test_load_task_config_validates_case_concurrency(
    tmp_path: Path, value: str
) -> None:
    """并发额度只接受正整数，不能通过 YAML 加载隐式转换。"""
    task = _write_task_config(tmp_path / "task")
    config_path = task / "config.yaml"
    config_path.write_text(
        config_path.read_text() + f"max_case_concurrency: {value}\n",
        encoding="utf-8",
    )
    if value == "2":
        loaded = load_task_config(task, environ={"MODEL": "test"})
        assert loaded.max_case_concurrency == 2
    else:
        with pytest.raises(ValidationError, match="max_case_concurrency"):
            load_task_config(task, environ={"MODEL": "test"})


def test_load_task_config_resolves_peer_entries(tmp_path: Path) -> None:
    """显式入口可复用同一 Tasks 根目录下的相邻实现。"""
    tasks = tmp_path / "tasks"
    peer = tasks / "peer"
    peer.mkdir(parents=True)
    (peer / "executor.py").touch()
    (peer / "evaluator.py").touch()
    task = _write_task_config(
        tasks / "task",
        entries=("../peer/executor.py", "../peer/evaluator.py"),
    )

    loaded = load_task_config(task, environ={"MODEL": "openai/test"})

    assert loaded.executor == peer / "executor.py"
    assert loaded.evaluator == peer / "evaluator.py"


@pytest.mark.parametrize("field", ["executor", "evaluator"])
def test_task_config_rejects_entry_outside_tasks_root(
    tmp_path: Path,
    field: str,
) -> None:
    """Task 入口不得逃逸共享 Tasks 根目录。"""
    entries = {"executor": "executor.py", "evaluator": "evaluator.py"}
    entries[field] = "../../outside.py"
    task = _write_task_config(
        tmp_path / "tasks" / "task",
        entries=(entries["executor"], entries["evaluator"]),
    )
    (tmp_path / "outside.py").touch()

    with pytest.raises(ValueError, match=field):
        load_task_config(task, environ={"MODEL": "openai/test"})


@pytest.mark.parametrize("field", ["executor", "evaluator"])
def test_task_config_rejects_absolute_entry(tmp_path: Path, field: str) -> None:
    """显式入口必须是相对于当前 Task 的可迁移路径。"""
    entries = {"executor": "executor.py", "evaluator": "evaluator.py"}
    entries[field] = str(tmp_path / "tasks" / "peer.py")
    task = _write_task_config(
        tmp_path / "tasks" / "task",
        entries=(entries["executor"], entries["evaluator"]),
    )
    (tmp_path / "tasks" / "peer.py").touch()

    with pytest.raises(ValueError, match=f"{field}.*相对路径"):
        load_task_config(task, environ={"MODEL": "openai/test"})


@pytest.mark.parametrize(
    "protected_path",
    [
        "",
        "/prompt",
        "../prompt",
        "./data",
        "data/.",
        "data//",
        "prompt/../data",
        "prompt\\file.md",
    ],
)
def test_config_rejects_unsafe_protected_path(protected_path: str) -> None:
    """保护边界不得包含空值、绝对路径或路径逃逸。"""
    with pytest.raises(ValidationError, match="read_only"):
        TaskConfig(
            objective="improve",
            initial_target=Path("target"),
            workspace=WorkspaceAccess(read_only=(protected_path,)),
        )


def test_config_rejects_duplicate_read_only() -> None:
    """重复边界会造成含糊且没有额外能力。"""
    with pytest.raises(ValidationError, match="重复"):
        TaskConfig(
            objective="improve",
            initial_target=Path("target"),
            workspace=WorkspaceAccess(read_only=("data/", "data/")),
        )


def test_config_allows_empty_read_only() -> None:
    """空保护列表表示整个 Target 都可以变更。"""
    config = TaskConfig(
        objective="improve",
        initial_target=Path("target"),
        workspace=WorkspaceAccess(),
    )

    assert config.workspace.read_only == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_retries", -1), ("retry_delay_seconds", -0.1)],
)
def test_config_rejects_negative_retry_settings(field: str, value: object) -> None:
    """重试次数和等待时间都不能为负数。"""
    with pytest.raises(ValidationError, match=field):
        TaskConfig.model_validate(
            {
                "objective": "improve",
                "initial_target": "target",
                "workspace": {"read_only": [], "inaccessible": []},
                field: value,
            }
        )


@pytest.mark.parametrize(
    "field", ["scenario", "task", "mutable_paths", "protected_paths"]
)
def test_config_rejects_removed_fields(field: str) -> None:
    """旧字段不得通过兼容层继续存在。"""
    value: object = [] if field == "mutable_paths" else "legacy"
    with pytest.raises(ValidationError, match=field):
        TaskConfig.model_validate(
            {
                "objective": "improve",
                "initial_target": "target",
                "workspace": {"read_only": [], "inaccessible": []},
                field: value,
            }
        )
