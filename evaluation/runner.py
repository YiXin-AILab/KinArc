"""评测主流程：加载任务、执行 Case、L1 规则、L2 Judge、落盘报告。"""

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

from agentsystem.config.loader import load_yaml_config
from evolution.execution import run_cases
from evolution.models import CaseExecution, ExecutionMetadata

from evaluation.executor import HttpAgentExecutor
from evaluation.judge import LlmJudge
from evaluation.models import (
    CaseOutcome,
    CaseScore,
    DimScore,
    EvalCase,
    EvalReport,
    EvalTaskConfig,
    Expected,
)
from evaluation.rules import run_rules, select_rules

_LOGGER = logging.getLogger(__name__)


class CaseExecutor(Protocol):
    """评测执行器协议；默认实现为 HttpAgentExecutor。"""

    def execute(self, case: EvalCase, stop_event: Event) -> CaseOutcome:
        """执行单条 Case 并返回事实产出。

        Args:
            case: 待执行的评测用例。
            stop_event: 执行期间的停止信号。

        Returns:
            单条用例的执行事实。
        """
        ...


class Judge(Protocol):
    """维度裁判协议；默认实现为 LlmJudge。"""

    async def score_cases(
        self,
        items: list[tuple[EvalCase, CaseOutcome]],
    ) -> list[list[DimScore]]:
        """按输入顺序返回每条 Case 的维度评分。"""
        ...


type CaseCompletedCallback = Callable[[str, CaseOutcome], None]


def load_dataset(task_dir: Path, dataset: str) -> list[EvalCase]:
    """加载 datasets/<name>.json 并逐条校验为 EvalCase。

    Args:
        task_dir: 评测任务根目录。
        dataset: 不含扩展名的数据集名称。

    Returns:
        按文件顺序排列的评测用例。

    Raises:
        ValueError: 文件不可读、JSON 无效或顶层不是数组。
        ValidationError: 用例字段不符合 EvalCase 模型。
    """
    path = task_dir / "datasets" / f"{dataset}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"数据集不可读：{path.name}") from error
    if not isinstance(raw, list):
        raise ValueError(f"数据集必须是 JSON 数组：{path.name}")
    return [EvalCase.model_validate(item) for item in raw]


def run_evaluation(
    task_dir: str | Path,
    *,
    run_id: str | None = None,
    environ: Mapping[str, str] = os.environ,
    executor: CaseExecutor | None = None,
    judge: Judge | None = None,
    on_case_completed: CaseCompletedCallback | None = None,
) -> EvalReport:
    """加载并运行评测任务，将执行事实和评分报告写入运行目录。

    Args:
        task_dir: 包含 config.yaml 与 datasets 的任务目录。
        run_id: 本次运行标识；省略时生成新标识。
        environ: 配置加载时用于环境变量插值的映射。
        executor: 可注入的用例执行器，默认通过 HTTP 调用 Runtime。
        judge: 可注入的维度裁判，默认使用配置中的 LLM。
        on_case_completed: 单条执行结束后的回调，在用例执行线程内调用。

    Returns:
        评分报告；同时写入 runs/<run_id>/outcomes.json 和 report.json。

    Raises:
        ValueError: 任务配置、数据集或评分选项无效。
        OSError: 报告目录或文件无法写入。
    """
    directory = Path(task_dir).resolve()
    task_id = directory.name
    resolved_run_id = run_id or f"run-{uuid4().hex[:12]}"
    config = load_yaml_config(
        directory / "config.yaml", EvalTaskConfig, environ=environ
    )
    cases = load_dataset(directory, config.dataset)
    resolved_executor = executor or HttpAgentExecutor(
        base_url=config.server_base_url,
        agent_id=config.agent_id,
        app_id="evaluation",
        user_id="evaluation",
    )
    resolved_judge = judge or LlmJudge(
        config.judge_llm,
        concurrency=config.judge_concurrency,
    )
    outcomes = _execute_cases(
        cases,
        executor=resolved_executor,
        max_case_concurrency=config.max_case_concurrency,
        on_case_completed=on_case_completed,
    )
    scores = _score_cases(cases, outcomes, resolved_judge)
    report = _build_report(
        run_id=resolved_run_id,
        task_id=task_id,
        agent_id=config.agent_id,
        dataset=config.dataset,
        scores=scores,
    )
    _persist(directory, resolved_run_id, outcomes, report)
    _LOGGER.info(
        "evaluation_completed task=%s run=%s cases=%s failed=%s final_score=%.4f",
        task_id,
        resolved_run_id,
        report.case_count,
        report.failed_count,
        report.final_score,
    )
    return report


def _execute_cases(
    cases: list[EvalCase],
    *,
    executor: CaseExecutor,
    max_case_concurrency: int,
    on_case_completed: CaseCompletedCallback | None,
) -> list[CaseOutcome]:
    def execute_once(case: EvalCase, stop_event: Event) -> CaseExecution:
        outcome = executor.execute(case, stop_event)
        if on_case_completed is not None:
            on_case_completed(case.id, outcome)
        return CaseExecution(
            id=case.id,
            result=outcome.reply or outcome.error or "",
            tracelog=outcome.model_dump(mode="json"),
            metadata=ExecutionMetadata(
                total_tokens=outcome.total_tokens or 0,
                duration_seconds=outcome.latency_seconds,
            ),
            retryable=False,
        )

    result = run_cases(
        cases,
        case_id=lambda case: case.id,
        execute_once=execute_once,
        max_case_concurrency=max_case_concurrency,
        max_retries=0,
        retry_delay_seconds=0.0,
    )
    return [
        CaseOutcome.model_validate(execution.tracelog) for execution in result.cases
    ]


def _score_cases(
    cases: list[EvalCase],
    outcomes: list[CaseOutcome],
    judge: Judge,
) -> list[CaseScore]:
    rule_results = [
        run_rules(
            select_rules(case.rules, case.expected),
            outcome,
            case.expected or Expected(),
        )
        for case, outcome in zip(cases, outcomes, strict=True)
    ]
    # 执行失败的 Case 不进入裁判评分，维度列表为空。
    judged_pairs = [
        (index, case, outcome)
        for index, (case, outcome) in enumerate(zip(cases, outcomes, strict=True))
        if outcome.error is None
    ]
    judged_dims = (
        asyncio.run(
            judge.score_cases([(case, outcome) for _, case, outcome in judged_pairs])
        )
        if judged_pairs
        else []
    )
    dim_map = {
        index: dims
        for (index, _, _), dims in zip(judged_pairs, judged_dims, strict=True)
    }
    dim_results: list[list[DimScore]] = [
        dim_map.get(index, []) for index in range(len(cases))
    ]
    scores: list[CaseScore] = []
    for case, outcome, rules, dims in zip(
        cases, outcomes, rule_results, dim_results, strict=True
    ):
        failed = outcome.error is not None or any(not item.passed for item in rules)
        scores.append(
            CaseScore(
                case_id=case.id,
                rules=rules,
                dims=dims,
                final_score=0.0 if failed else _weighted_average(dims),
                failed=failed,
            )
        )
    return scores


def _weighted_average(dims: list[DimScore]) -> float:
    scored = [dim for dim in dims if dim.score is not None]
    if not scored:
        return 1.0
    total_weight = sum(dim.weight for dim in scored)
    return sum(dim.score * dim.weight for dim in scored if dim.score is not None) / (
        total_weight
    )


def _build_report(
    *,
    run_id: str,
    task_id: str,
    agent_id: str,
    dataset: str,
    scores: list[CaseScore],
) -> EvalReport:
    dim_scores: dict[str, list[float]] = {}
    for score in scores:
        for dim in score.dims:
            if dim.score is not None:
                dim_scores.setdefault(dim.dimension, []).append(dim.score)
    return EvalReport(
        run_id=run_id,
        task_id=task_id,
        agent_id=agent_id,
        dataset=dataset,
        case_count=len(scores),
        failed_count=sum(1 for score in scores if score.failed),
        dim_averages={
            name: sum(values) / len(values)
            for name, values in sorted(dim_scores.items())
        },
        final_score=(
            sum(score.final_score for score in scores) / len(scores) if scores else 0.0
        ),
        cases=scores,
    )


def _persist(
    task_dir: Path,
    run_id: str,
    outcomes: list[CaseOutcome],
    report: EvalReport,
) -> None:
    run_dir = task_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "outcomes.json").write_text(
        json.dumps(
            [outcome.model_dump(mode="json") for outcome in outcomes],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "report.json").write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )


__all__ = [
    "CaseCompletedCallback",
    "CaseExecutor",
    "Judge",
    "load_dataset",
    "run_evaluation",
]
