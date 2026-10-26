"""生成四组对比结果，并保留原有两组对比的身份校验。"""

import html
import json
import random
from pathlib import Path
from statistics import mean, median
from typing import Any

from agentsystem.config.loader import load_yaml_config
from evolution.models import CaseExecution, EvaluationResult
from evolution.task import TaskConfig

from benchmarks.evoagentbench.common.identity import file_digest
from benchmarks.evoagentbench.experiments.experiment_state import (
    read_config,
    verify_frozen,
    write_json,
)
from benchmarks.evoagentbench.reporting.report import identities, metrics, scores_of
from benchmarks.evoagentbench.reporting.usage import summarize_tokens

ARMS = ("base", "yx", "prompt", "evolved")


def paired(
    left: dict[str, list[float]], right: dict[str, list[float]]
) -> dict[str, object]:
    """按任务而非试次重采样，估计固定学习产物的不确定性。"""
    if set(left) != set(right) or any(len(left[k]) != len(right[k]) for k in left):
        raise ValueError("Task/trial coverage differs")
    deltas = [mean(right[k]) - mean(left[k]) for k in sorted(left)]
    rng = random.Random(42)
    samples = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(10000))
    return {
        "delta_pass@1": mean(deltas),
        "paired_task_bootstrap_95": [samples[250], samples[9750]],
        "improved": sum(v > 0 for v in deltas),
        "regressed": sum(v < 0 for v in deltas),
        "unchanged": sum(v == 0 for v in deltas),
    }


def build_report(root: Path) -> dict[str, object]:
    """校验全部四个冻结实验组，并发布 JSON 和 HTML 对比报告。

    Args:
        root: 包含冻结记录和全部实验组回执的实验目录。

    Returns:
        分数、配对任务差值、用量摘要和已知学习成本。

    Raises:
        ValueError: 冻结条件、来源身份或结果覆盖范围不一致。
    """
    frozen = verify_frozen(root)
    results: dict[str, EvaluationResult] = {}
    for arm in ARMS:
        path = root / "results" / arm / "evaluation.json"
        receipt = json.loads(path.with_name("receipt.json").read_text())
        if (
            receipt["comparison_key"] != frozen["comparison_key"]
            or receipt["evaluation_sha256"] != file_digest(path)
            or receipt.get("arm") != arm
            or receipt.get("target")
            != frozen["arms"].get(arm, frozen["baseline_sha256"])
        ):
            raise ValueError(f"Changed or mismatched {arm} evidence")
        results[arm] = EvaluationResult.model_validate_json(path.read_text())
        validate_result(
            root, arm, results[arm], frozen["arms"].get(arm, frozen["baseline_sha256"])
        )
    # B、C、D 组必须使用相同的 Runtime、配置和协议；A 组使用不同框架。
    if len({identities(results[a]) for a in ARMS[1:]}) != 1:
        raise ValueError("YX ablations differ in model or execution protocol")
    scores = {a: scores_of(r) for a, r in results.items()}
    columns = {
        a: {
            **metrics(scores[a]),
            "tasks": len(scores[a]),
            "trials": sum(len(v) for v in scores[a].values()),
            **summarize_tokens(r),
            "seconds": sum(c.metadata.duration_seconds for c in r.cases),
            **execution_summary(r),
        }
        for a, r in results.items()
    }
    contrasts = {
        f"{after}-{before}": paired(scores[before], scores[after])
        for before, after in (
            ("base", "yx"),
            ("yx", "prompt"),
            ("yx", "evolved"),
            ("prompt", "evolved"),
        )
    }
    outcome: dict[str, object] = {
        "comparison_key": frozen["comparison_key"],
        "columns": columns,
        "contrasts": contrasts,
        "primary_contrast": "evolved-yx",
        "per_task": {
            key: {a: scores[a][key] for a in ARMS} for key in sorted(scores["yx"])
        },
        "limitations": [
            "Single evolution run; task bootstrap is not evolution-seed uncertainty",
            "base-yx includes native harness/tool differences",
            "evolved-prompt includes interactions between learned assets",
        ],
        "learning_cost": learning_cost(root),
    }
    write_json(root / "report.json", outcome)
    headers = "".join(f"<th>{a}</th>" for a in ARMS)
    numeric_keys = [
        key for key, value in columns["yx"].items() if isinstance(value, (int, float))
    ]
    rows = "".join(
        f"<tr><th>{html.escape(key)}</th>"
        + "".join(f"<td>{columns[a][key]:.4f}</td>" for a in ARMS)
        + "</tr>"
        for key in numeric_keys
    )
    links = " ".join(
        f'<a href="results/{a}/evaluation.json">{a} evidence</a>' for a in ARMS
    )
    body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>EvoAgentBench four-arm results</title><style>
body{{font:16px/1.6 sans-serif;max-width:1100px;margin:40px auto}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:10px}}pre{{white-space:pre-wrap}}</style>
<h1>EvoAgentBench 四组实验</h1><p>主要比较：自进化后 − YX-Agent。
以下数据来自完整冻结测试，缺失组不会生成报告。</p>
<table><tr><th>指标</th>{headers}</tr>{rows}</table><h2>配对差值</h2>
<pre>{html.escape(json.dumps(contrasts, ensure_ascii=False, indent=2))}</pre>
<h2>证据</h2><p>{links}</p><p>逐 trial 会话索引位于 evidence/；
模型用量来自执行记录；训练与验证见 task/.evolution/。
进化器会话用量不再采集，报告中以 null 标识。</p>
<p>单轮任务置信区间不代表多次进化的方差；
base 与 YX 的工具及 harness 差异也影响分数。</p></html>"""
    (root / "report.html").write_text(body)
    return outcome


def execution_summary(result: EvaluationResult) -> dict[str, float | None]:
    """汇总各独立执行试次的耗时与 token 用量。

    Args:
        result: 案例轨迹中包含原始执行记录的评测结果。

    Returns:
        耗时分位数、未成功执行次数，以及用量已知时的平均 token 数。

    Raises:
        ValueError: 案例缺少汇总所需的执行轨迹。
    """
    durations: list[float] = []
    terminated = 0
    for case in result.cases:
        if not isinstance(case.tracelog, dict):
            raise ValueError("Missing execution trace")
        execution = CaseExecution.model_validate(case.tracelog["execution"])
        attempts = execution.trials or (execution,)
        for attempt in attempts:
            durations.append(attempt.metadata.duration_seconds)
            trace = attempt.tracelog
            terminated += int(
                not isinstance(trace, dict) or trace.get("status") != "succeeded"
            )
    durations.sort()
    tokens = summarize_tokens(result)["tokens"]
    return {
        "mean_tokens_per_trial": tokens / len(durations)
        if tokens is not None
        else None,
        "duration_p50_seconds": median(durations),
        "duration_p95_seconds": durations[
            min(len(durations) - 1, int(len(durations) * 0.95))
        ],
        "non_successful_executions": float(terminated),
    }


def learning_cost(root: Path) -> dict[str, Any]:
    """对可用学习证据只统计一次，未知金额成本保留为 null。"""
    task = root / read_config(root).task_dir
    execution_tokens = 0
    incomplete_token_trials = 0
    execution_seconds = 0.0
    batches = 0
    for role in ("train", "validation"):
        for path in (task / ".evolution/candidates").glob(f"*/{role}/execution.json"):
            from evolution.models import ExecutionResult

            execution = ExecutionResult.model_validate_json(path.read_text())
            usage = summarize_tokens(execution)
            execution_tokens += usage["reported_tokens"]
            incomplete_token_trials += usage["incomplete_token_trials"]
            execution_seconds += sum(
                c.metadata.duration_seconds for c in execution.cases
            )
            batches += 1
    return {
        "training_validation_tokens": execution_tokens
        if not incomplete_token_trials
        else None,
        "training_validation_reported_tokens": execution_tokens,
        "training_validation_incomplete_token_trials": incomplete_token_trials,
        "training_validation_seconds": execution_seconds,
        "recorded_batches": batches,
        "improver_sessions": None,
        "improver_sessions_without_usage": None,
        "improver_known_tokens": None,
        "improver_accounting_status": "not_collected",
        "monetary_cost": None,
        "cost_status": "provider price accounting unavailable",
        "note": (
            "Completed batches only; incomplete attempts remain in evidence/. "
            "C shares D learning cost."
        ),
    }


def validate_result(
    root: Path, arm: str, result: EvaluationResult, target: str
) -> None:
    """要求完整的任务覆盖，并校验每个试次的冻结目标来源。"""
    task = root / read_config(root).task_dir
    frozen = json.loads((root / "freeze.json").read_text())
    if (
        arm != "base"
        and identities(result)[1] != frozen["inputs"]["agent_config_sha256"]
    ):
        raise ValueError(f"Wrong model configuration for {arm}")
    expected = json.loads((task / "datasets/test.json").read_text())
    required_trials = load_yaml_config(task / "config.yaml", TaskConfig).settings[
        "test_trials"
    ]
    if not isinstance(required_trials, int) or isinstance(required_trials, bool):
        raise ValueError("Invalid test trial count")
    if {c.id for c in result.cases} != {c["id"] for c in expected["cases"]}:
        raise ValueError(f"Incomplete {arm} task coverage")
    for case in result.cases:
        if not isinstance(case.tracelog, dict):
            raise ValueError("Missing evaluation trace")
        execution = CaseExecution.model_validate(case.tracelog.get("execution"))
        traces = [t.tracelog for t in execution.trials] or [execution.tracelog]
        trial_scores = case.tracelog.get("scores")
        if (
            not isinstance(trial_scores, list)
            or len(traces) != required_trials
            or len(trial_scores) != required_trials
        ):
            raise ValueError(f"Incomplete {arm} trial coverage")
        for trace in traces:
            if not isinstance(trace, dict) or trace.get("target_sha256") != target:
                raise ValueError(f"Wrong target provenance for {arm}")
