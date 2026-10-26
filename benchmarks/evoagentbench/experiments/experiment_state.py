"""实验初始化与可检测篡改的冻结记录。"""

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from agentsystem.common.models import StrictModel
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import AgentConfig
from evolution.candidates import tree_digest
from evolution.task import TaskConfig
from pydantic import Field

from benchmarks.evoagentbench.common.identity import file_digest, source_digest
from benchmarks.evoagentbench.experiments.arms import (
    copy_assets,
    freeze_arms,
    verify_arms,
)


class ExperimentConfig(StrictModel):
    """可迁移且不含密钥的实验选项，工具配置保存在任务文件中。"""

    preset: Literal["pilot", "formal"] = "pilot"
    seed: int = 42
    validation_count: int = Field(default=36, gt=0)
    task_dir: str = "task"
    baseline_config: str = "baseline.yaml"


def read_config(root: Path) -> ExperimentConfig:
    """读取并校验实验的可迁移配置。

    Args:
        root: 包含 experiment.json 的实验目录。

    Returns:
        通过严格校验的实验配置。
    """
    return ExperimentConfig.model_validate_json((root / "experiment.json").read_text())


def write_json(path: Path, value: object) -> None:
    """原子发布证据，避免读取到只写入一部分的 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def experiment_lock(root: Path) -> Iterator[None]:
    """防止两个 CLI 调用同时学习或评测同一实验。"""
    with (root / ".experiment.lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another command owns this experiment") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def initialize(
    root: Path, template: Path, baseline: Path, config: ExperimentConfig
) -> None:
    """创建隔离的学习资产和配置，不复制旧数据集与结果。"""
    if root.exists():
        raise FileExistsError("Experiment directory exists; refusing to overwrite")
    raw = yaml.safe_load((template / "config.yaml").read_text())
    source = (template / raw["initial_target"]).resolve(strict=True)
    baseline_bytes = baseline.read_bytes()
    runtime_source = (template / raw["settings"]["runtime_config"]).resolve(strict=True)
    runtime_raw = yaml.safe_load(runtime_source.read_text())
    root.mkdir(parents=True)
    copy_assets(source, root / "initial_target")
    task = root / config.task_dir
    task.mkdir()
    for field, default in (("executor", "executor.py"), ("evaluator", "evaluator.py")):
        (task / default).write_bytes((template / raw.get(field, default)).read_bytes())
        raw[field] = default
    raw["initial_target"] = "../initial_target"
    raw["max_retries"] = 0
    # 正式实验使用模板的搜索策略，仅试运行限制预算。
    if config.preset == "pilot":
        raw["max_iterations"] = 2
    raw["settings"]["runtime_config"] = "../runtime.yaml"
    raw["settings"]["artifact_dir"] = "../evidence"
    raw["settings"].update(
        {
            "train_trials": 1,
            "validation_trials": 1,
            "test_trials": 1,
        }
    )
    # 保留显式依赖路径；${ENV} 路径在准备阶段解析。
    for name in (
        "upstream_repo",
        "upstream_python",
        "domain_config",
        "dependency_manifest",
        "hf_cache_dir",
    ):
        value = raw["settings"].get(name)
        if isinstance(value, str) and "${" not in value:
            raw["settings"][name] = str((template / value).absolute())
    (task / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    (root / "runtime.yaml").write_text(yaml.safe_dump(runtime_raw, sort_keys=False))
    (root / config.baseline_config).write_bytes(baseline_bytes)
    write_json(root / "experiment.json", config.model_dump())


def protocol_inputs(root: Path) -> dict[str, Any]:
    """分别计算对比条件与各实验组目标来源的摘要。"""
    config = read_config(root)
    task = root / config.task_dir
    paths = [
        root / "experiment.json",
        root / "runtime.yaml",
        root / config.baseline_config,
        task / "config.yaml",
    ]
    paths.extend(
        task / "datasets" / f"{role}.json" for role in ("train", "validation", "test")
    )
    agent = load_yaml_config(root / "initial_target/config/agent.yaml", AgentConfig)
    improver_assets = Path(__file__).resolve().parents[3] / "evolution/agent"
    improver = load_yaml_config(improver_assets / "config/agent.yaml", AgentConfig)
    from benchmarks.evoagentbench.runners.official_baseline import (
        validate_baseline_model,
    )

    return {
        "files": {str(p.relative_to(root)): file_digest(p) for p in paths},
        "source": source_digest(),
        "public_model": validate_baseline_model(root / config.baseline_config, agent),
        "grading_dependencies": dependency_identity(task),
        "initial_target_sha256": tree_digest(root / "initial_target"),
        "improver_assets_sha256": tree_digest(improver_assets),
        "improver_config_sha256": hashlib.sha256(
            improver.model_dump_json().encode()
        ).hexdigest(),
        "agent_config_sha256": hashlib.sha256(
            agent.model_dump_json().encode()
        ).hexdigest(),
    }


def dependency_identity(task: Path) -> str:
    """绑定实际使用的公共评分器、数据缓存、源码和执行镜像。"""
    raw = load_yaml_config(task / "config.yaml", TaskConfig)
    from benchmarks.evoagentbench.domains.domain_settings import (
        experiment_identity,
        settings_for,
    )

    return experiment_identity(
        settings_for(task, raw.settings), task / "datasets/test.json"
    )


def freeze(root: Path) -> None:
    """在测试评测前，仅选择引擎确定的验证集最优候选。"""
    config = read_config(root)
    result = json.loads((root / "optimization.json").read_text())
    inputs = protocol_inputs(root)
    learning = json.loads((root / "learning_receipt.json").read_text())
    if learning["inputs"] != inputs or learning["optimization_sha256"] != file_digest(
        root / "optimization.json"
    ):
        raise ValueError("Learning result does not match current experiment conditions")
    candidates = root / config.task_dir / ".evolution/candidates"
    seed_id, best_id = result["seed_candidate_id"], result["best_candidate_id"]
    for value in (seed_id, best_id):
        if Path(value).name != value:
            raise ValueError("Invalid candidate identifier")
    if (root / "arms").exists():
        verify_arms(root / "arms")
        manifest = json.loads((root / "arms/manifest.json").read_text())
        if manifest["seed_sha256"] != tree_digest(
            candidates / seed_id / "target"
        ) or manifest["best_sha256"] != tree_digest(candidates / best_id / "target"):
            raise ValueError("Existing frozen arms belong to different candidates")
    else:
        freeze_arms(
            candidates / seed_id / "target",
            candidates / best_id / "target",
            root / "arms",
        )
    write_json(
        root / "freeze.json",
        {
            "comparison_key": hashlib.sha256(
                json.dumps(inputs, sort_keys=True).encode()
            ).hexdigest(),
            "inputs": inputs,
            "seed_candidate_id": seed_id,
            "best_candidate_id": best_id,
            "arms": verify_arms(root / "arms"),
            "baseline_sha256": file_digest(root / config.baseline_config),
        },
    )


def verify_frozen(root: Path) -> dict[str, Any]:
    """复用缓存结果前拒绝数据、配置、模型或源码的变化。"""
    frozen: dict[str, Any] = json.loads((root / "freeze.json").read_text())
    if protocol_inputs(root) != frozen["inputs"]:
        raise ValueError("Experiment conditions changed after freeze")
    if verify_arms(root / "arms") != frozen["arms"]:
        raise ValueError("Frozen arm manifest changed")
    return frozen
