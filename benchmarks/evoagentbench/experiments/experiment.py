"""用于准备、学习、四组冻结评测和报告生成的 CLI。"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import AgentConfig, RuntimeConfig
from agentsystem.redis.client import close_redis_client, create_redis_client
from dotenv import load_dotenv
from evolution.cli import _execute_command
from evolution.models import DatasetRole, ExecutionResult
from evolution.task import TaskConfig, load_task, load_task_config

from benchmarks.evoagentbench.common.identity import file_digest
from benchmarks.evoagentbench.domains.domain_settings import settings_for
from benchmarks.evoagentbench.experiments.experiment_state import (
    ExperimentConfig,
    experiment_lock,
    freeze,
    initialize,
    protocol_inputs,
    read_config,
    verify_frozen,
    write_json,
)
from benchmarks.evoagentbench.reporting.experiment_report import (
    ARMS,
    build_report,
    validate_result,
)
from benchmarks.evoagentbench.runners.official_baseline import (
    preflight_baseline,
    validate_baseline_model,
)


def prepare_experiment(root: Path) -> None:
    """按实验固定的划分策略导出公开数据集。

    Args:
        root: 已初始化且包含模板派生配置的实验目录。

    Raises:
        subprocess.CalledProcessError: 领域导出 worker 执行失败。
        subprocess.TimeoutExpired: 数据集准备超过三十分钟时限。
    """
    config = read_config(root)
    task = root / config.task_dir
    raw = load_yaml_config(task / "config.yaml", TaskConfig)
    # 显式 ID 清单使各领域的试运行和正式划分策略保持一致。
    from benchmarks.evoagentbench.data.prepare_dataset import pilot_ids, split_ids
    from benchmarks.evoagentbench.domains.domain_settings import (
        settings_for as domain_settings,
    )

    settings = domain_settings(task, raw.settings)
    split_path = (
        settings.upstream_repo / "benchmark/data/splits" / f"{settings.domain}.json"
    )
    original = json.loads(split_path.read_text())
    splitter = pilot_ids if config.preset == "pilot" else split_ids
    ids = splitter(original, config.validation_count, config.seed)
    ids_path = root / "domain_ids.json"
    write_json(ids_path, ids)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.evoagentbench.data.domain_prepare",
            str(task),
            "--ids",
            str(ids_path),
        ],
        check=True,
        timeout=1800,
    )
    write_json(
        task / "datasets/split_manifest.json",
        {
            "roles": ids,
            "preset": config.preset,
            "seed": config.seed,
            "test_source": "official_train"
            if config.preset == "pilot"
            else "official_test",
            "source_sha256": file_digest(split_path),
        },
    )


async def _check_redis(runtime: RuntimeConfig) -> None:
    client = await create_redis_client(runtime.redis)
    await close_redis_client(client)


def preflight(root: Path) -> dict[str, Any]:
    """检查实际本地依赖，不发起付费模型请求。"""
    config = read_config(root)
    task = root / config.task_dir
    raw = load_yaml_config(task / "config.yaml", TaskConfig)
    agent = load_yaml_config(root / "initial_target/config/agent.yaml", AgentConfig)
    runtime = load_yaml_config(root / "runtime.yaml", RuntimeConfig)
    model = validate_baseline_model(root / config.baseline_config, agent)
    asyncio.run(_check_redis(runtime))
    settings = settings_for(task, raw.settings)
    preflight_baseline(settings, root / config.baseline_config)
    load_task(load_task_config(task))
    result = {
        "status": "dependencies_ready",
        "inputs": protocol_inputs(root),
        "model": model,
        "trace_enabled": runtime.trace.enabled,
        "limits": [
            "No paid model call",
            "Official full task environment must pass its first real smoke trial",
        ],
    }
    write_json(root / "preflight.json", result)
    return result


def evolve(root: Path) -> None:
    """运行现有进化引擎，记录其按验证集选出的候选。"""
    if (root / "arms").exists() or (root / "results").exists():
        raise ValueError(
            "Cannot continue learning after freeze/test; use a new experiment"
        )
    config = read_config(root)
    checked = json.loads((root / "preflight.json").read_text())
    if checked["inputs"] != protocol_inputs(root):
        raise ValueError("Conditions changed since preflight")
    started = root / "learning_started.json"
    if started.exists():
        if json.loads(started.read_text())["inputs"] != checked["inputs"]:
            raise ValueError("Cannot resume learning under changed conditions")
    else:
        write_json(started, {"inputs": checked["inputs"]})
    result = asyncio.run(
        _execute_command(
            argparse.Namespace(
                command="run",
                task_dir=root / config.task_dir,
                runtime_config=root / "runtime.yaml",
            ),
            Path.cwd(),
            os.environ,
        )
    )
    if checked["inputs"] != protocol_inputs(root):
        raise ValueError("Experiment conditions changed during learning")
    write_json(root / "optimization.json", result.model_dump(mode="json"))
    write_json(
        root / "learning_receipt.json",
        {
            "inputs": checked["inputs"],
            "optimization_sha256": file_digest(root / "optimization.json"),
        },
    )


def _baseline_execution(
    root: Path,
    dataset: Path,
    role: DatasetRole = DatasetRole.TEST,
) -> tuple[ExecutionResult, Any]:
    config = read_config(root)
    task_root = root / config.task_dir
    raw = load_yaml_config(task_root / "config.yaml", TaskConfig)
    from benchmarks.evoagentbench.domains.domain_adapter import (
        DomainEvaluator,
        DomainExecutor,
    )
    from benchmarks.evoagentbench.domains.domain_settings import (
        settings_for as domain_settings,
    )

    domain = domain_settings(task_root, raw.settings).model_copy(
        update={
            "runner": "official_baseline",
            "official_agent_config": root / config.baseline_config,
        }
    )
    execution = DomainExecutor(domain).execute(
        root / "arms/yx" if role == DatasetRole.TEST else root / "initial_target",
        dataset,
        role,
        max_retries=0,
        max_case_concurrency=raw.max_case_concurrency,
    )
    return execution, DomainEvaluator(domain)


def smoke(root: Path) -> None:
    """通过 base 和 YX 组执行两道训练题，会发起真实模型请求。"""
    from benchmarks.evoagentbench.data.models import Dataset

    checked = json.loads((root / "preflight.json").read_text())
    if checked["inputs"] != protocol_inputs(root):
        raise ValueError("Conditions changed since preflight")
    config = read_config(root)
    resolved = load_task_config(root / config.task_dir)
    task = load_task(resolved)
    source = Dataset.model_validate_json(task.datasets[DatasetRole.TRAIN].read_text())
    path = root / "smoke/dataset.json"
    selected = source.model_copy(update={"cases": source.cases[:2]})
    if path.exists() and Dataset.model_validate_json(path.read_text()) != selected:
        raise ValueError("Smoke dataset differs from current training sample")
    if not path.exists():
        write_json(path, selected.model_dump(mode="json"))
    for arm in ("yx", "base"):
        if arm == "base":
            execution, evaluator = _baseline_execution(root, path, DatasetRole.TRAIN)
        else:
            execution = task.executor.execute(
                root / "initial_target", path, DatasetRole.TRAIN, max_retries=0
            )
            evaluator = task.evaluator
        write_json(
            root / "smoke" / arm / "execution.json", execution.model_dump(mode="json")
        )
        result = evaluator.evaluate(execution, path)
        write_json(
            root / "smoke" / arm / "evaluation.json", result.model_dump(mode="json")
        )


def status(root: Path) -> dict[str, Any]:
    """索引试次身份和状态，完整执行记录保留在证据目录中。"""
    config = read_config(root)
    rows = []
    archives = ("evidence/trials", "results/base/raw/trials", "smoke/base/raw/trials")
    paths = sorted(
        path for archive in archives for path in (root / archive).glob("*/*.json")
    )
    for path in paths:
        value = json.loads(path.read_text())
        rows.append(
            {
                "identity": value["identity"],
                "status": value["status"],
                "session_id": value.get("session_id"),
                "evidence_file": str(path.relative_to(root)),
            }
        )
    output = {
        "preset": config.preset,
        "frozen": (root / "freeze.json").exists(),
        "completed_arms": [
            a for a in ARMS if (root / "results" / a / "receipt.json").exists()
        ],
        "trials": rows,
    }
    write_json(root / "trace-index.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output


def evaluate(root: Path, arms: list[str]) -> None:
    """评测冻结实验组，仅复用身份与记录一致的证据。

    在评分前保存完整执行结果，使评分失败后能够恢复，
    无需再次调用模型；完成校验后才发布结果回执。

    Args:
        root: 包含数据集和实验组快照的冻结实验目录。
        arms: 从 base、yx、prompt、evolved 中选择的不重复组名。

    Raises:
        ValueError: 组别选择、冻结条件或缓存证据无效。
    """
    frozen = verify_frozen(root)
    config = read_config(root)
    resolved = load_task_config(root / config.task_dir)
    task = load_task(resolved)
    dataset = task.datasets[DatasetRole.TEST]
    if len(set(arms)) != len(arms) or any(a not in ARMS for a in arms):
        raise ValueError("Specify distinct known arms")
    for arm in arms:
        output = root / "results" / arm
        receipt_path = output / "receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if (
                receipt["comparison_key"] != frozen["comparison_key"]
                or receipt["evaluation_sha256"]
                != file_digest(output / "evaluation.json")
                or receipt.get("arm") != arm
                or receipt.get("target")
                != frozen["arms"].get(arm, frozen["baseline_sha256"])
            ):
                raise ValueError("Cached evaluation identity changed")
            continue
        execution_path = output / "execution.json"
        # 仅在单独保存的身份记录匹配时复用完整执行结果。
        marker = output / "execution_identity.json"
        execution_key = {
            "comparison_key": frozen["comparison_key"],
            "arm": arm,
            "target": frozen["arms"].get(arm, frozen["baseline_sha256"]),
        }
        evaluator = task.evaluator
        if execution_path.exists() and marker.exists():
            if json.loads(marker.read_text()) != {
                **execution_key,
                "execution_sha256": file_digest(execution_path),
            }:
                raise ValueError("Saved execution does not match frozen experiment")
            execution = ExecutionResult.model_validate_json(execution_path.read_text())
            if arm == "base" and "domain" in resolved.settings:
                from benchmarks.evoagentbench.domains.domain_adapter import (
                    DomainEvaluator,
                )
                from benchmarks.evoagentbench.domains.domain_settings import (
                    settings_for as domain_settings,
                )

                evaluator = DomainEvaluator(
                    domain_settings(resolved.root, resolved.settings).model_copy(
                        update={
                            "runner": "official_baseline",
                            "official_agent_config": root / config.baseline_config,
                        }
                    )
                )
        elif arm == "base":
            execution, evaluator = _baseline_execution(root, dataset)
        else:
            execution = task.executor.execute(
                root / "arms" / arm,
                dataset,
                DatasetRole.TEST,
                max_retries=0,
                max_case_concurrency=resolved.max_case_concurrency,
            )
        write_json(execution_path, execution.model_dump(mode="json"))
        write_json(
            marker, {**execution_key, "execution_sha256": file_digest(execution_path)}
        )
        result = evaluator.evaluate(execution, dataset)
        validate_result(root, arm, result, execution_key["target"])
        verify_frozen(root)
        write_json(output / "evaluation.json", result.model_dump(mode="json"))
        write_json(
            receipt_path,
            {
                **execution_key,
                "evaluation_sha256": file_digest(output / "evaluation.json"),
            },
        )


def main() -> None:
    """派发实验 CLI，对修改实验状态的命令使用独占锁。"""
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "init",
            "prepare",
            "preflight",
            "smoke",
            "evolve",
            "freeze",
            "evaluate",
            "report",
            "status",
        ),
    )
    parser.add_argument("experiment_dir", type=Path)
    parser.add_argument("--task-template", type=Path)
    parser.add_argument("--baseline-config", type=Path)
    parser.add_argument("--preset", choices=("pilot", "formal"), default="pilot")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-count", type=int, default=36)
    parser.add_argument("--arms", default=",".join(ARMS))
    args = parser.parse_args()
    root = args.experiment_dir.resolve()
    if args.command == "init":
        if args.task_template is None or args.baseline_config is None:
            parser.error("init requires --task-template and --baseline-config")
        initialize(
            root,
            args.task_template.resolve(),
            args.baseline_config.resolve(),
            ExperimentConfig(
                preset=args.preset,
                seed=args.seed,
                validation_count=args.validation_count,
            ),
        )
    elif args.command == "status":
        status(root)
    else:
        with experiment_lock(root):
            if args.command == "evaluate":
                evaluate(root, args.arms.split(","))
            else:
                {
                    "prepare": prepare_experiment,
                    "preflight": preflight,
                    "smoke": smoke,
                    "evolve": evolve,
                    "freeze": freeze,
                    "report": build_report,
                    "status": status,
                }[args.command](root)
    print(
        json.dumps(
            {
                "command": args.command,
                "experiment_dir": str(root),
                "status": "completed",
            }
        )
    )


if __name__ == "__main__":
    main()
