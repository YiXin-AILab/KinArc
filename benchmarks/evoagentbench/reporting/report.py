"""按任务配对的可靠性与迁移效果报告。"""

import argparse
import json
import math
import random
from pathlib import Path

from evolution.models import CaseExecution, EvaluationResult

from benchmarks.evoagentbench.reporting.usage import summarize_tokens


def identities(result: EvaluationResult) -> tuple[str, str]:
    """拒绝跨预算、数据或模型配置的比较，以及混合快照。"""
    protocols: set[str] = set()
    configs: set[str] = set()
    targets: set[str] = set()
    for case in result.cases:
        if not isinstance(case.tracelog, dict):
            raise ValueError("Missing execution identity")
        execution = CaseExecution.model_validate(case.tracelog.get("execution"))
        traces = [t.tracelog for t in execution.trials] or [execution.tracelog]
        for trace in traces:
            if not isinstance(trace, dict):
                raise ValueError("Missing trial identity")
            for key, destination in (
                ("protocol", protocols),
                ("agent_config_sha256", configs),
                ("target_sha256", targets),
            ):
                value = trace.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"Missing trial identity: {key}")
                destination.add(value)
    if any(len(values) != 1 for values in (protocols, configs, targets)):
        raise ValueError("Mixed protocol, model configuration or candidate snapshots")
    return protocols.pop(), configs.pop()


def scores_of(result: EvaluationResult) -> dict[str, list[float]]:
    """拒绝不完整或非二值观测，不通过丢弃任务规避校验。"""
    output = {}
    for case in result.cases:
        trace = case.tracelog
        values = trace.get("scores") if isinstance(trace, dict) else None
        if not isinstance(values, list) or not values:
            raise ValueError("Missing per-trial scores")
        if any(type(v) not in (int, float) or v not in (0, 1) for v in values):
            raise ValueError("Reliability requires binary per-trial scores")
        output[case.id] = [float(v) for v in values if isinstance(v, (int, float))]
    if not output:
        raise ValueError("Empty evaluation")
    return output


def metrics(scores: dict[str, list[float]]) -> dict[str, float]:
    """计算有限样本下无偏的 pass@k 与 pass^k，并按任务取平均。"""
    counts = {len(v) for v in scores.values()}
    if not scores or len(counts) != 1:
        raise ValueError("All expected tasks must have the same trial count")
    n = counts.pop()
    result = {}
    for k in range(1, n + 1):
        denominator = math.comb(n, k)
        result[f"pass@{k}"] = sum(
            1 - math.comb(n - int(sum(v)), k) / denominator for v in scores.values()
        ) / len(scores)
        result[f"pass^{k}"] = sum(
            math.comb(int(sum(v)), k) / denominator for v in scores.values()
        ) / len(scores)
    return result


def compare(
    before: EvaluationResult,
    after: EvaluationResult,
    *,
    bootstrap_samples: int = 10000,
    seed: int = 42,
) -> dict[str, object]:
    """按任务 ID 配对，统计退化案例与任务 bootstrap 不确定性。"""
    left, right = scores_of(before), scores_of(after)
    if identities(before) != identities(after):
        raise ValueError("Baseline and evolved protocol/model identities differ")
    if set(left) != set(right) or any(len(left[k]) != len(right[k]) for k in left):
        raise ValueError(
            "Paired evaluations must cover identical tasks and trial counts"
        )
    if bootstrap_samples < 100:
        raise ValueError("At least 100 bootstrap samples required")
    deltas = [
        sum(right[k]) / len(right[k]) - sum(left[k]) / len(left[k])
        for k in sorted(left)
    ]
    rng = random.Random(seed)
    samples = sorted(
        sum(rng.choices(deltas, k=len(deltas))) / len(deltas)
        for _ in range(bootstrap_samples)
    )
    return {
        "tasks": len(left),
        "baseline": metrics(left),
        "evolved": metrics(right),
        "delta_pass@1": sum(deltas) / len(deltas),
        "paired_task_bootstrap_95": [
            samples[int(0.025 * bootstrap_samples)],
            samples[min(bootstrap_samples - 1, int(0.975 * bootstrap_samples))],
        ],
        "improved": sum(d > 0 for d in deltas),
        "regressed": sum(d < 0 for d in deltas),
        "unchanged": sum(d == 0 for d in deltas),
        "baseline_tokens": summarize_tokens(before)["tokens"],
        "baseline_token_accounting": summarize_tokens(before),
        "evolved_tokens": summarize_tokens(after)["tokens"],
        "evolved_token_accounting": summarize_tokens(after),
        "baseline_seconds": sum(c.metadata.duration_seconds for c in before.cases),
        "evolved_seconds": sum(c.metadata.duration_seconds for c in after.cases),
    }


def main() -> None:
    """比较两个不可变的评测 JSON 文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("evolved", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            compare(
                EvaluationResult.model_validate_json(args.baseline.read_text()),
                EvaluationResult.model_validate_json(args.evolved.read_text()),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
