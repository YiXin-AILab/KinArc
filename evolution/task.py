"""Evolution Task 配置、入口加载与 Dataset 读取。"""

import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType

from agentsystem.common.models import FrozenModel, JsonValue, StrictModel
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import NonEmptyName
from pydantic import Field, field_validator

from evolution.evaluator import Evaluator
from evolution.executor import Executor
from evolution.models import DatasetRole, RollbackChangelogSource
from evolution.workspace import WorkspaceAccess


class TaskConfig(StrictModel):
    """用户声明的单一 Evolution Task 配置。"""

    objective: NonEmptyName
    initial_target: Path
    executor: Path = Path("executor.py")
    evaluator: Path = Path("evaluator.py")
    workspace: WorkspaceAccess
    datasets: dict[DatasetRole, Path] = Field(
        default_factory=lambda: {
            role: Path("datasets") / f"{role.value}.json" for role in DatasetRole
        }
    )
    max_iterations: int = Field(default=5, ge=0)
    improver_timeout_seconds: float = Field(default=1200, gt=0)
    max_retries: int = Field(default=3, ge=0, strict=True)
    max_case_concurrency: int = Field(default=1, ge=1, strict=True)
    single_case_min_cases: int = Field(default=2, ge=2, strict=True)
    retry_delay_seconds: float = Field(default=2, ge=0, allow_inf_nan=False)
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    rollback_enabled: bool = Field(default=True, strict=True)
    exploration_patience: int = Field(default=3, ge=1, strict=True)
    rollback_changelog_source: RollbackChangelogSource = "best"

    @field_validator("datasets")
    @classmethod
    def validate_dataset_roles(
        cls, value: dict[DatasetRole, Path]
    ) -> dict[DatasetRole, Path]:
        """显式映射必须完整声明三个 Role。"""
        if set(value) != set(DatasetRole):
            raise ValueError("datasets 必须包含 train、validation、test")
        return value


@dataclass(frozen=True, slots=True)
class ResolvedTaskConfig:
    """相对路径已按 Task 根目录解析的配置。"""

    root: Path
    datasets: dict[DatasetRole, Path]
    objective: str
    initial_target: Path
    executor: Path
    evaluator: Path
    workspace: WorkspaceAccess
    max_iterations: int
    improver_timeout_seconds: float
    max_retries: int
    max_case_concurrency: int
    single_case_min_cases: int
    retry_delay_seconds: float
    settings: dict[str, JsonValue]
    rollback_enabled: bool = True
    exploration_patience: int = 3
    rollback_changelog_source: RollbackChangelogSource = "best"


def load_task_config(
    task_dir: str | Path,
    *,
    environ: Mapping[str, str] = os.environ,
) -> ResolvedTaskConfig:
    """加载任务 YAML，插值环境变量并解析入口与数据集路径。

    Args:
        task_dir: 包含 config.yaml 的任务目录。
        environ: 用于配置插值的环境变量映射。

    Returns:
        路径已解析、配置已校验的任务配置。

    Raises:
        ValueError: 配置无效，或入口、数据集路径不符合边界约束。
        OSError: 任务目录或配置文件不可读取。
    """
    root = Path(task_dir).resolve(strict=True)
    config = load_yaml_config(root / "config.yaml", TaskConfig, environ=environ)
    return ResolvedTaskConfig(
        root=root,
        objective=config.objective,
        initial_target=(root / config.initial_target).resolve(strict=True),
        executor=_resolve_entry(root, config.executor, "executor"),
        evaluator=_resolve_entry(root, config.evaluator, "evaluator"),
        datasets=resolve_datasets(root, config.datasets),
        workspace=config.workspace,
        max_iterations=config.max_iterations,
        improver_timeout_seconds=config.improver_timeout_seconds,
        max_retries=config.max_retries,
        max_case_concurrency=config.max_case_concurrency,
        single_case_min_cases=config.single_case_min_cases,
        retry_delay_seconds=config.retry_delay_seconds,
        settings=dict(config.settings),
        rollback_enabled=config.rollback_enabled,
        exploration_patience=config.exploration_patience,
        rollback_changelog_source=config.rollback_changelog_source,
    )


def _resolve_entry(root: Path, configured: Path, field: str) -> Path:
    """解析相对 Python 入口，要求文件存在且位于 Tasks 根目录内。"""
    if configured.is_absolute():
        raise ValueError(f"{field} 入口必须使用相对路径：{configured}")
    try:
        entry = (root / configured).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ValueError(f"{field} 入口不存在：{configured}") from error
    try:
        entry.relative_to(root.parent)
    except ValueError as error:
        raise ValueError(f"{field} 入口不得超出 Tasks 根目录：{configured}") from error
    if not entry.is_file() or entry.suffix != ".py":
        raise ValueError(f"{field} 入口必须是 Python 文件：{configured}")
    return entry


_IGNORED_SOURCE_PARTS = frozenset({".evolution", "dependencies", "__pycache__"})


@dataclass(frozen=True, slots=True)
class TaskBundle:
    """通用编排器所需的 Task 运行端口与身份。"""

    task_dir: Path
    executor: Executor
    evaluator: Evaluator
    datasets: Mapping[DatasetRole, Path]
    digest: str


def load_task(config: ResolvedTaskConfig) -> TaskBundle:
    """加载任务入口并创建 Executor 和 Evaluator。

    入口以配置、数据集及实现源码的摘要命名，加载失败时恢复原有模块映射。

    Args:
        config: 已解析路径并完成校验的任务配置。

    Returns:
        包含任务端口、数据集路径和内容摘要的 TaskBundle。

    Raises:
        ValueError: 入口无法加载或缺少约定的 Factory。
        TypeError: Factory 返回对象不满足任务协议。
    """
    datasets = config.datasets
    digest = _task_digest(config, datasets)
    executor_name = f"_evolution_{digest}_executor"
    evaluator_name = f"_evolution_{digest}_evaluator"
    previous_modules = {
        executor_name: sys.modules.get(executor_name),
        evaluator_name: sys.modules.get(evaluator_name),
    }
    try:
        executor_module = _load_module(config.executor, executor_name)
        evaluator_module = _load_module(config.evaluator, evaluator_name)
        executor = _create_port(
            executor_module, "create_executor", config.root, config.settings
        )
        evaluator = _create_port(
            evaluator_module, "create_evaluator", config.root, config.settings
        )
        if not isinstance(executor, Executor):
            raise TypeError("create_executor 必须返回 Executor")
        if not isinstance(evaluator, Evaluator):
            raise TypeError("create_evaluator 必须返回 Evaluator")
    except BaseException:
        _restore_modules(previous_modules)
        raise
    return TaskBundle(
        task_dir=config.root,
        executor=executor,
        evaluator=evaluator,
        datasets=MappingProxyType(dict(datasets)),
        digest=digest,
    )


def _restore_modules(previous: Mapping[str, ModuleType | None]) -> None:
    """加载失败时恢复先前的动态模块映射，清除本次新增项。"""
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_module(path: Path, module_name: str) -> ModuleType:
    """按内容关联的模块名加载任务入口，并在失败时恢复模块状态。"""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 Task 模块：{path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(module_name) is module:
            if previous is None:
                del sys.modules[module_name]
            else:
                sys.modules[module_name] = previous
        raise
    return module


def _create_port(
    module: ModuleType,
    factory_name: str,
    task_dir: Path,
    settings: Mapping[str, JsonValue],
) -> object:
    """取得可调用的固定 Factory，并传入任务目录及专属配置。"""
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(f"Task 模块缺少 Factory：{factory_name}")
    return factory(task_dir, settings)


def _task_digest(
    config: ResolvedTaskConfig,
    datasets: Mapping[DatasetRole, Path],
) -> str:
    """汇总配置、数据集及共享实现源码，生成动态加载使用的内容摘要。"""
    files: dict[str, Path] = {"config.yaml": config.root / "config.yaml"}
    for role, path in datasets.items():
        files[f"datasets/{role.value}"] = path
    tasks_root = config.root.parent
    implementation_roots = sorted(
        {
            _implementation_root(tasks_root, config.executor),
            _implementation_root(tasks_root, config.evaluator),
        },
        key=lambda path: path.as_posix(),
    )
    for index, implementation_root in enumerate(implementation_roots):
        for path in sorted(implementation_root.rglob("*.py")):
            relative = path.relative_to(implementation_root)
            if path.is_file() and not (_IGNORED_SOURCE_PARTS & set(relative.parts)):
                files[f"entry/{index}/{relative.as_posix()}"] = path
    return _digest_files(files)


def _implementation_root(tasks_root: Path, entry: Path) -> Path:
    """确定入口所属的实现任务目录，供完整源码扫描。"""
    relative = entry.relative_to(tasks_root)
    if len(relative.parts) < 2:
        raise ValueError(f"Task 入口必须属于 Tasks 根目录下的任务目录：{entry}")
    return tasks_root / relative.parts[0]


def _digest_files(files: Mapping[str, Path]) -> str:
    """按稳定顺序将文件标签和内容写入同一个摘要。"""
    digest = hashlib.sha256()
    for label, path in sorted(files.items()):
        encoded_label = label.encode()
        digest.update(len(encoded_label).to_bytes(4, "big"))
        digest.update(encoded_label)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def resolve_datasets(
    root: Path, configured: Mapping[DatasetRole, Path]
) -> dict[DatasetRole, Path]:
    """解析三份本地文件，允许共享同级任务数据，禁止逃逸 Tasks 根目录。"""
    paths: dict[DatasetRole, Path] = {}
    for role in DatasetRole:
        relative = configured[role]
        if relative.is_absolute():
            raise ValueError(f"Dataset 必须使用相对路径：{role.value}")
        try:
            path = (root / relative).resolve(strict=True)
            path.relative_to(root.parent)
        except (OSError, ValueError) as error:
            raise ValueError(
                f"Dataset 必须是 Tasks 根目录内已存在文件：{role.value}"
            ) from error
        if not path.is_file():
            raise ValueError(f"Dataset 必须是文件：{role.value}")
        paths[role] = path
    return paths


class DatasetCase(FrozenModel):
    """保留原始 JSON 对象的可选 Case 包装，不是调度器必需类型。"""

    id: str = Field(min_length=1)
    content: dict[str, JsonValue]

    @field_validator("id")
    @classmethod
    def validate_non_blank_id(cls, value: str) -> str:
        """拒绝空白 ID，同时保留合法原始值。"""
        if not value.strip():
            raise ValueError("Case id 不能为空")
        return value


def load_json_cases(path: str | Path) -> tuple[DatasetCase, ...]:
    """读取使用 JSON 数组表示的数据集。

    Args:
        path: UTF-8 JSON 数据集文件路径。

    Returns:
        保留输入顺序及每条原始字段的 Case 元组；空数组返回空元组。

    Raises:
        ValueError: JSON 无效、顶层不是数组，或 Case ID 缺失、为空、重复。
        OSError: 数据集文件不可读取。
    """
    dataset_path = Path(path)
    raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Dataset 必须是 JSON 数组：{dataset_path}")
    cases: list[DatasetCase] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Dataset Case 必须是对象：index={index}")
        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Dataset Case id 必须是非空字符串：index={index}")
        if case_id in seen:
            raise ValueError(f"Dataset Case id 重复：{case_id}")
        seen.add(case_id)
        cases.append(DatasetCase(id=case_id, content=item))
    return tuple(cases)


__all__ = [
    "ResolvedTaskConfig",
    "TaskBundle",
    "TaskConfig",
    "load_task",
    "load_task_config",
]
