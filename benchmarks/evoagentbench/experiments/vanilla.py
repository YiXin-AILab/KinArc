"""在官方训练集上运行配对的单次基线试验，不启动进化引擎。"""

import argparse
import hashlib
import json
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentsystem.common.logging import log_unexpected_exception
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import AgentConfig
from dotenv import dotenv_values, load_dotenv
from evolution.candidates import tree_digest
from evolution.models import DatasetRole, EvaluationResult, ExecutionResult
from evolution.task import TaskConfig

from benchmarks.evoagentbench.common.identity import (
    file_digest,
    source_digest,
)
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains import domain_adapter, domain_settings
from benchmarks.evoagentbench.experiments.experiment_state import (
    experiment_lock,
    write_json,
)
from benchmarks.evoagentbench.runners.official_baseline import (
    preflight_baseline,
    validate_baseline_model,
)

LOGGER = logging.getLogger(__name__)
DOMAINS = (
    "knowledge_work",
    "information_retrieval",
)


def load_profile(path: Path) -> None:
    """覆盖共享默认配置，并要求当前配置提供非空模型密钥。"""
    load_dotenv(Path.cwd() / ".env", override=False)
    profile = dotenv_values(path)
    for key in ("EAB_MODEL", "EAB_BASE_MODEL", "EAB_API_KEY", "EAB_BASE_URL"):
        if not profile.get(key):
            raise ValueError(f"Model profile requires a nonempty {key}")
    load_dotenv(path, override=True)


def settings_for(root: Path) -> domain_settings.DomainSettings:
    """加载已有领域任务的 Runtime 配置与依赖设置。"""
    task = root / "task"
    config = load_yaml_config(task / "config.yaml", TaskConfig)
    return domain_settings.settings_for(task, config.settings)


def official_train(settings: domain_settings.DomainSettings) -> Dataset:
    """导出官方训练集的全部任务 ID，不额外划分验证集。"""
    domain = settings.domain
    split = settings.upstream_repo / "benchmark/data/splits" / f"{domain}.json"
    raw = json.loads(split.read_text())
    ids = raw["train"]
    if not ids or len(set(ids)) != len(ids) or set(ids) & set(raw["test"]):
        raise ValueError("Official train IDs must be nonempty, unique and disjoint")
    exported = domain_adapter.upstream(settings, "prepare", ids)
    rows = exported["cases"]
    if not isinstance(rows, list):
        raise ValueError("Expected exported public cases")
    cases = tuple(Case.model_validate(row) for row in rows)
    if [case.id for case in cases] != ids:
        raise ValueError("Exported case membership/order differs from official train")
    return Dataset(
        role="train",
        upstream_revision=settings.upstream_revision,
        source_sha256=file_digest(split),
        cases=cases,
    )


def conditions(root: Path, dataset: Path) -> dict[str, Any]:
    """将恢复条件绑定到模型、源码、资产、数据集和实际领域依赖。"""
    settings = settings_for(root)
    agent = load_yaml_config(root / "initial_target/config/agent.yaml", AgentConfig)
    provenance = validate_baseline_model(root / "baseline.yaml", agent)
    protocol = domain_settings.experiment_identity(settings, dataset)
    # 对解析后的配置计算摘要，不保存端点或凭证。
    model_hash = hashlib.sha256(agent.model_dump_json().encode()).hexdigest()
    return {
        "mode": "vanilla",
        "role": "official_train",
        "trials": 1,
        "source": source_digest(),
        "protocol": protocol,
        "target": tree_digest(root / "initial_target"),
        "model": provenance,
        "model_config_sha256": model_hash,
        "baseline_sha256": file_digest(root / "baseline.yaml"),
    }


def prepare(root: Path) -> None:
    """在依赖准备完成后初始化，并校验各组的预算一致。"""
    settings = settings_for(root)
    if settings.train_trials != 1:
        raise ValueError("Vanilla experiment requires exactly one train trial")
    preflight_baseline(settings, root / "baseline.yaml")
    dataset = official_train(settings)
    path = root / "train.json"
    if path.exists():
        if Dataset.model_validate_json(path.read_text()) != dataset:
            raise ValueError("Prepared dataset changed")
    else:
        write_json(path, dataset.model_dump(mode="json"))
    identity = conditions(root, path)
    manifest = root / "vanilla.json"
    if manifest.exists() and json.loads(manifest.read_text()) != identity:
        raise ValueError("Vanilla conditions changed; use a new run directory")
    write_json(manifest, identity)


def run_case(root: Path, dataset: Path, arm: str, output: Path) -> None:
    """在评分前保存执行结果，使评分重试无需再次调用模型。"""
    settings = settings_for(root).model_copy(
        update={"artifact_dir": output / "evidence"}
    )
    role = DatasetRole.TRAIN
    if arm == "base":
        settings = settings.model_copy(
            update={
                "runner": "official_baseline",
                "official_agent_config": root / "baseline.yaml",
            }
        )
    executor = domain_adapter.DomainExecutor(settings)
    evaluator = domain_adapter.DomainEvaluator(settings)
    execution_path = output / "execution.json"
    if execution_path.exists():
        execution = ExecutionResult.model_validate_json(execution_path.read_text())
    else:
        execution = executor.execute(
            root / "initial_target", dataset, role, max_retries=0
        )
        write_json(execution_path, execution.model_dump(mode="json"))
    result = evaluator.evaluate(execution, dataset)
    write_json(output / "evaluation.json", result.model_dump(mode="json"))
    write_json(
        output / "receipt.json",
        {
            "execution_sha256": file_digest(execution_path),
            "evaluation_sha256": file_digest(output / "evaluation.json"),
        },
    )


def cached_result(output: Path, case_id: str) -> EvaluationResult | None:
    """仅接纳证据摘要和 case ID 均匹配的已完成试次。"""
    receipt = output / "receipt.json"
    if not receipt.exists():
        return None
    value = json.loads(receipt.read_text())
    if value != {
        "execution_sha256": file_digest(output / "execution.json"),
        "evaluation_sha256": file_digest(output / "evaluation.json"),
    }:
        raise ValueError("Cached trial evidence changed")
    result = EvaluationResult.model_validate_json(
        (output / "evaluation.json").read_text()
    )
    if [case.id for case in result.cases] != [case_id]:
        raise ValueError("Cached trial case ID changed")
    return result


def report(root: Path) -> dict[str, Any]:
    """区分基础设施导致的未完成案例与实际测得的 Agent 失败。"""
    dataset = Dataset.model_validate_json((root / "train.json").read_text())
    rows = []
    for index, case in enumerate(dataset.cases):
        row: dict[str, Any] = {"case_id": case.id}
        for arm in ("base", "yx"):
            output = root / "results" / arm / str(index)
            result = cached_result(output, case.id)
            row[arm] = (
                {"status": "completed", "score": result.total_score}
                if result
                else {
                    "status": "error"
                    if (output / "error.json").exists()
                    else "pending",
                    "score": None,
                }
            )
        rows.append(row)
    summary: dict[str, Any] = {"expected_per_arm": len(rows), "rows": rows, "arms": {}}
    for arm in ("base", "yx"):
        scores = [
            row[arm]["score"] for row in rows if row[arm]["status"] == "completed"
        ]
        summary["arms"][arm] = {
            "completed": len(scores),
            "errors": sum(row[arm]["status"] == "error" for row in rows),
            "pass_at_1": sum(scores) / len(rows) if len(scores) == len(rows) else None,
            "observed_mean": sum(scores) / len(scores) if scores else None,
        }
    write_json(root / "summary.json", summary)
    return summary


def execute_job(root: Path, path: Path, arm: str, output: Path, case_id: str) -> None:
    """负责一次完整的模型、工具和评分生命周期及其错误记录。"""
    try:
        run_case(root, path, arm, output)
    except Exception as error:
        identifier = log_unexpected_exception(
            LOGGER,
            "vanilla_case_failed",
            error,
            operation="vanilla.run_case",
            arm=arm,
            case_id=case_id,
        )
        write_json(
            output / "error.json",
            {"type": type(error).__name__, "error_id": identifier},
        )


def dispatch(
    root: Path, jobs: list[tuple[Path, str, Path, str]], concurrency: int
) -> None:
    """限制两组试验的并发执行数量，仅由协调器汇总报告。

    中断时停止派发，等待已有有限超时约束的
    worker 完成清理后再释放实验锁，不遗留容器。
    """
    remaining = iter(jobs)
    pending: set[Future[None]] = set()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        try:
            while True:
                while len(pending) < concurrency:
                    job = next(remaining, None)
                    if job is None:
                        break
                    path, arm, output, case_id = job
                    pending.add(
                        pool.submit(
                            execute_job,
                            root,
                            path,
                            arm,
                            output,
                            case_id,
                        )
                    )
                if not pending:
                    break
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                report(root)
        finally:
            # 不保留排队积压，最多只需等待 concurrency 个 worker 完成清理。
            for future in pending:
                future.cancel()


def run(
    root: Path, arms: list[str], limit: int | None = None, *, concurrency: int = 2
) -> None:
    """单个案例发生基础设施错误后继续处理其余案例，恢复时只执行未完成项。"""
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if not arms or len(set(arms)) != len(arms) or set(arms) - {"base", "yx"}:
        raise ValueError("Choose distinct base/yx arms")
    identity = json.loads((root / "vanilla.json").read_text())
    if conditions(root, root / "train.json") != identity:
        raise ValueError("Vanilla experiment conditions changed")
    dataset = Dataset.model_validate_json((root / "train.json").read_text())
    jobs: list[tuple[Path, str, Path, str]] = []
    for index, case in enumerate(dataset.cases[:limit]):
        path = root / "cases" / f"{index}.json"
        single = dataset.model_copy(update={"cases": (case,)})
        if path.exists() and Dataset.model_validate_json(path.read_text()) != single:
            raise ValueError("Per-case dataset changed")
        write_json(path, single.model_dump(mode="json"))
        for arm in arms:
            output = root / "results" / arm / str(index)
            if cached_result(output, case.id):
                continue
            jobs.append((path, arm, output, case.id))
    write_json(
        root / "dispatches" / f"{uuid4().hex}.json",
        {
            "concurrency": concurrency,
            "arms": arms,
            "limit": limit,
            "jobs": [
                {"arm": arm, "case_id": identifier} for _, arm, _, identifier in jobs
            ],
        },
    )
    dispatch(root, jobs, concurrency)
    report(root)
    if conditions(root, root / "train.json") != identity:
        raise ValueError("Conditions changed during experiment")


def main() -> None:
    """使用已初始化的任务资产，不调用冻结、进化或测试阶段。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--arms", default="base,yx")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    load_profile(args.env_file)
    root = args.root.resolve()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    with experiment_lock(root):
        if args.command == "prepare":
            prepare(root)
        elif args.command == "report":
            report(root)
        else:
            run(root, args.arms.split(","), args.limit, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
