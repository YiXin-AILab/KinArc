"""受信 Task 入口加载测试。"""

import json
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest
from evolution.models import DatasetRole
from evolution.task import load_task, load_task_config

_EXECUTOR = """
from evolution.models import ExecutionResult

class FakeExecutor:
    def __init__(self, task_dir, settings):
        self.task_dir = task_dir
        self.settings = settings

    def execute(self, target_dir, dataset_path, dataset_role, *,
                max_case_concurrency=1, max_retries=3,
                retry_delay_seconds=2, cancel_event=None):
        return ExecutionResult(cases=())

def create_executor(task_dir, settings):
    return FakeExecutor(task_dir, settings)
"""

_EVALUATOR = """
from evolution.models import EvaluationResult

class FakeEvaluator:
    def evaluate(self, execution, dataset_path):
        return EvaluationResult(total_score=0.0, cases=())

def create_evaluator(task_dir, settings):
    del task_dir, settings
    return FakeEvaluator()
"""


class _LoadedExecutor(Protocol):
    task_dir: Path
    settings: dict[str, object]


def _write_task(root: Path, *, entries: tuple[str, str] | None = None) -> None:
    root.mkdir(parents=True)
    (root / "target").mkdir()
    entry_yaml = ""
    if entries is None:
        (root / "executor.py").write_text(_EXECUTOR, encoding="utf-8")
        (root / "evaluator.py").write_text(_EVALUATOR, encoding="utf-8")
    else:
        entry_yaml = f"executor: {entries[0]}\nevaluator: {entries[1]}\n"
    (root / "config.yaml").write_text(
        entry_yaml
        + """objective: improve
initial_target: target
workspace: {read_only: [], inaccessible: []}
settings:
  model: ${MODEL}
""",
        encoding="utf-8",
    )
    datasets = root / "datasets"
    datasets.mkdir()
    for role in DatasetRole:
        (datasets / f"{role.value}.json").write_text(
            json.dumps([{"id": role.value}]), encoding="utf-8"
        )


def test_load_task_uses_derived_data_and_shared_ports(tmp_path: Path) -> None:
    """派生 Task 只复用入口实现，配置与 Dataset 均属于自身。"""
    tasks = tmp_path / "tasks"
    implementation = tasks / "base"
    implementation.mkdir(parents=True)
    (implementation / "executor.py").write_text(_EXECUTOR, encoding="utf-8")
    (implementation / "evaluator.py").write_text(_EVALUATOR, encoding="utf-8")
    derived = tasks / "derived"
    _write_task(
        derived,
        entries=("../base/executor.py", "../base/evaluator.py"),
    )

    bundle = load_task(load_task_config(derived, environ={"MODEL": "openai/derived"}))
    executor = cast(_LoadedExecutor, bundle.executor)

    assert bundle.task_dir == derived
    assert executor.task_dir == derived
    assert executor.settings == {"model": "openai/derived"}
    assert bundle.datasets[DatasetRole.TRAIN] == derived / "datasets/train.json"
    assert len(bundle.digest) == 64


def test_task_reuses_sibling_datasets_and_tracks_content(tmp_path: Path) -> None:
    """共享数据集按来源路径加载，其内容变更仍影响 Task 身份。"""
    base = tmp_path / "tasks" / "base"
    derived = tmp_path / "tasks" / "derived"
    _write_task(base)
    _write_task(derived)
    config_path = derived / "config.yaml"
    config_path.write_text(
        config_path.read_text()
        + "datasets:\n"
        + "".join(
            f"  {role.value}: ../base/datasets/{role.value}.json\n"
            for role in DatasetRole
        )
    )
    config = load_task_config(derived, environ={"MODEL": "test"})
    task = load_task(config)
    assert task.datasets == {
        role: base / "datasets" / f"{role.value}.json" for role in DatasetRole
    }
    (base / "datasets/train.json").write_text('[{"id": "changed"}]')
    assert load_task(config).digest != task.digest


def test_task_digest_changes_when_entry_sibling_changes(tmp_path: Path) -> None:
    """入口同目录的辅助 Python 实现属于 Task 身份。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    helper = task / "adapter.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    config = load_task_config(task, environ={"MODEL": "test"})

    original = load_task(config).digest
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    assert load_task(config).digest != original


def test_task_digest_covers_full_implementation_task(tmp_path: Path) -> None:
    """嵌套入口仍须覆盖其所属实现 Task 的全部 Python 源码。"""
    tasks = tmp_path / "tasks"
    implementation = tasks / "base"
    ports = implementation / "ports"
    ports.mkdir(parents=True)
    (ports / "executor.py").write_text(_EXECUTOR, encoding="utf-8")
    (ports / "evaluator.py").write_text(_EVALUATOR, encoding="utf-8")
    helper = implementation / "models.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    task = tasks / "derived"
    _write_task(
        task,
        entries=("../base/ports/executor.py", "../base/ports/evaluator.py"),
    )
    config = load_task_config(task, environ={"MODEL": "test"})
    original = load_task(config).digest

    helper.write_text("VALUE = 2\n", encoding="utf-8")

    assert load_task(config).digest != original


@pytest.mark.parametrize(
    "relative_path",
    [
        "config.yaml",
        "datasets/train.json",
        "datasets/validation.json",
        "datasets/test.json",
    ],
)
def test_task_digest_includes_config_and_datasets(
    tmp_path: Path,
    relative_path: str,
) -> None:
    """Task 自有配置和三类 Dataset 均属于其身份。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    config = load_task_config(task, environ={"MODEL": "test"})
    original = load_task(config).digest
    changed = task / relative_path

    changed.write_text(changed.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert load_task(config).digest != original


def test_load_task_supports_future_annotations_dataclass_entry(
    tmp_path: Path,
) -> None:
    """动态模块必须支持 Python 3.13 的常见 dataclass 定义。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    (task / "executor.py").write_text(
        """from __future__ import annotations
from dataclasses import dataclass
from evolution.models import ExecutionResult

@dataclass
class FakeExecutor:
    label: str = "executor"

    def execute(self, target_dir, dataset_path, dataset_role, *,
                max_case_concurrency=1, max_retries=3,
                retry_delay_seconds=2, cancel_event=None):
        return ExecutionResult(cases=())

def create_executor(task_dir, settings):
    del task_dir, settings
    return FakeExecutor()
""",
        encoding="utf-8",
    )

    bundle = load_task(load_task_config(task, environ={"MODEL": "test"}))

    assert type(bundle.executor).__name__ == "FakeExecutor"


@pytest.mark.parametrize(
    "ignored",
    [
        "dependencies/external/module.py",
        ".evolution/cache.py",
        "__pycache__/cache.py",
    ],
)
def test_task_digest_ignores_runtime_and_dependency_sources(
    tmp_path: Path,
    ignored: str,
) -> None:
    """外部依赖和运行产物不得污染 Task 摘要。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    ignored_path = task / ignored
    ignored_path.parent.mkdir(parents=True, exist_ok=True)
    ignored_path.write_text("VALUE = 1\n", encoding="utf-8")
    config = load_task_config(task, environ={"MODEL": "test"})
    original = load_task(config).digest

    ignored_path.write_text("VALUE = 2\n", encoding="utf-8")

    assert load_task(config).digest == original


@pytest.mark.parametrize("missing", ["executor.py", "evaluator.py"])
def test_load_task_rejects_missing_entry(tmp_path: Path, missing: str) -> None:
    """缺失入口时必须在执行前 fail-close。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    (task / missing).unlink()

    with pytest.raises(ValueError, match=missing.split(".")[0]):
        load_task_config(task, environ={"MODEL": "test"})


def test_load_task_rejects_missing_factory(tmp_path: Path) -> None:
    """入口必须暴露约定 Factory。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    (task / "executor.py").write_text("VALUE = 1\n", encoding="utf-8")

    before = {name for name in sys.modules if name.startswith("_evolution_")}
    with pytest.raises(ValueError, match="create_executor"):
        load_task(load_task_config(task, environ={"MODEL": "test"}))

    assert {name for name in sys.modules if name.startswith("_evolution_")} == before


def test_load_task_rejects_factory_result_without_protocol(tmp_path: Path) -> None:
    """Factory 返回任意对象不得延迟到运行时才失败。"""
    task = tmp_path / "tasks" / "task"
    _write_task(task)
    (task / "executor.py").write_text(
        "def create_executor(task_dir, settings):\n    return object()\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="Executor"):
        load_task(load_task_config(task, environ={"MODEL": "test"}))
