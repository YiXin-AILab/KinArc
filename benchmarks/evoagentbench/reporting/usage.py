"""报告已知 token 数，不将不完整统计显示为精确用量。"""

from collections.abc import Iterable
from typing import TypedDict

from evolution.models import CaseExecution, EvaluationResult, ExecutionResult


class TokenSummary(TypedDict):
    """任一独立执行缺少用量统计时，不提供精确总量。"""

    tokens: int | None
    reported_tokens: int
    incomplete_token_trials: int


def summarize_tokens(result: EvaluationResult | ExecutionResult) -> TokenSummary:
    """检查每个试次，包括历史快照和重试记录。"""
    executions: list[CaseExecution] = []
    for case in result.cases:
        if isinstance(case, CaseExecution):
            executions.append(case)
        elif isinstance(case.tracelog, dict):
            executions.append(CaseExecution.model_validate(case.tracelog["execution"]))
        else:
            raise ValueError("Missing execution trace for token accounting")
    return _summarize(executions)


def _summarize(executions: Iterable[CaseExecution]) -> TokenSummary:
    reported = incomplete = 0
    for case in executions:
        for trial in case.trials or case.attempts or (case,):
            reported += trial.metadata.total_tokens
            trace = trial.tracelog
            accounting = "exact"
            if isinstance(trace, dict):
                marker = trace.get("token_accounting")
                snapshot = trace.get("snapshot")
                if isinstance(marker, str):
                    accounting = marker
                elif isinstance(snapshot, dict):
                    usage = snapshot.get("usage")
                    accounting = (
                        str(usage.get("accounting", "missing"))
                        if isinstance(usage, dict)
                        else "missing"
                    )
            incomplete += accounting not in {"exact", "total_only"}
    return {
        "tokens": reported if not incomplete else None,
        "reported_tokens": reported,
        "incomplete_token_trials": incomplete,
    }
