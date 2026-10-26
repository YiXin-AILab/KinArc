"""为依赖官方运行环境的 EvoAgentBench 领域实现 Evolution 执行与评分协议。"""

import hashlib
import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

from agentsystem.common.models import JsonValue
from evolution.candidates import tree_digest
from evolution.execution import run_cases
from evolution.models import (
    CaseAttempt,
    CaseEvaluation,
    CaseExecution,
    DatasetRole,
    EvaluationResult,
    ExecutionMetadata,
    ExecutionResult,
)

from benchmarks.evoagentbench.common.process import invoke
from benchmarks.evoagentbench.common.trace_store import TrialStore
from benchmarks.evoagentbench.data.models import Case, Dataset
from benchmarks.evoagentbench.domains.domain_settings import (
    DomainSettings,
    experiment_identity,
)
from benchmarks.evoagentbench.domains.office_feedback import summarize_office_trials
from benchmarks.evoagentbench.domains.retrieval_feedback import (
    summarize_retrieval_trials,
)


def load_dataset(
    path: Path, role: DatasetRole | None, settings: DomainSettings
) -> Dataset:
    """校验数据集角色、版本，以及非空且不重复的案例集合。

    Args:
        path: 待加载的公开数据集清单。
        role: 预期角色；评分器直接读取清单中的角色时传入 None。
        settings: 包含指定上游版本的冻结配置。

    Returns:
        通过校验且保留清单中案例顺序的数据集。

    Raises:
        ValueError: 角色、版本或案例集合不符合领域约定。
    """
    dataset = Dataset.model_validate_json(path.read_text())
    ids = [case.id for case in dataset.cases]
    if (
        (role is not None and dataset.role != role.value)
        or dataset.upstream_revision != settings.upstream_revision
        or not ids
        or len(ids) != len(set(ids))
    ):
        raise ValueError("Invalid domain dataset identity or membership")
    return dataset


def upstream(
    settings: DomainSettings,
    operation: str,
    ids: list[str],
    *,
    target: Path | None = None,
    stop: Event | None = None,
) -> dict[str, JsonValue]:
    """监督完整执行生命周期，并为评分器设置硬超时。

    Args:
        settings: 已解析的依赖及执行、评分时间预算。
        operation: 导出公开案例或执行一次试验。
        ids: 向官方 adapter 请求的完整任务 ID 列表。
        target: 试验执行使用的可选候选资产目录。
        stop: 传递给子进程监督器的协作取消信号。

    Returns:
        上游 worker 成功完成后返回的 JSON 结果。

    Raises:
        TimeoutError: worker 的完整生命周期超过硬超时。
        asyncio.CancelledError: 监督器收到协作停止信号。
    """
    tag = uuid4().hex
    container = None
    if operation == "trial" and settings.domain == "knowledge_work":
        container = f"eab-office-{tag}"
    elif operation == "trial" and settings.research_execution_image:
        container = f"eab-retrieval-{tag}"
    return invoke(
        [
            str(settings.upstream_python),
            str(Path(__file__).with_name("domain_worker.py")),
        ],
        {
            "settings": settings.model_dump(mode="json"),
            "operation": operation,
            "ids": ids,
            "target": str(target.resolve()) if target else None,
            "runtime_python": sys.executable,
            "job_tag": tag,
            "container": container,
        },
        timeout=settings.case_timeout_seconds + settings.verifier_timeout_seconds + 180,
        cancel=stop,
        owned_container=container,
    )


class DomainExecutor:
    """在销毁官方单次试验环境前完成产物验证。"""

    def __init__(self, settings: DomainSettings) -> None:
        """绑定已解析的依赖，并校验可选的基线配置。

        Args:
            settings: 冻结的领域、执行器和证据存储配置。

        Raises:
            ValueError: 官方基线配置缺失或无效。
        """
        if settings.runner == "official_baseline":
            from benchmarks.evoagentbench.runners.official_baseline import (
                load_baseline_config,
            )

            if settings.official_agent_config is None:
                raise ValueError("Official baseline requires an agent config")
            load_baseline_config(settings.official_agent_config)
        self.settings = settings

    def execute(
        self,
        target_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 3,
        retry_delay_seconds: float = 2,
        cancel_event: Event | None = None,
    ) -> ExecutionResult:
        """按固定试次数执行全部案例，并在恢复时校验证据身份。

        普通 worker 错误会收集到整批执行结束后
        再传播；不完整的批次不会生成用于评分的 ExecutionResult。

        Args:
            target_dir: 所选执行器使用的冻结候选资产目录。
            dataset_path: 明确列出全部案例的公开数据集清单。
            dataset_role: 数据集角色，用于选择每个案例的配置试次数。
            max_case_concurrency: 同时执行的案例生命周期数量上限。
            max_retries: 必须为零，基础设施错误不得触发案例重新采样。
            retry_delay_seconds: 调度策略参数；重试次数为零时不使用。
            cancel_event: 与 worker 共享的可选协作停止信号。

        Returns:
            按输入顺序排列的案例，包含全部独立试次及汇总用量。

        Raises:
            ValueError: 重试策略、数据集身份或候选证据无效。
            asyncio.CancelledError: 调度器传播外部停止信号。
        """
        if max_retries != 0:
            raise ValueError("EvoAgentBench requires max_retries=0")
        dataset = load_dataset(dataset_path, dataset_role, self.settings)
        identity = experiment_identity(self.settings, dataset_path)
        candidate_digest = tree_digest(target_dir)
        digest = (
            hashlib.sha256(self.settings.official_agent_config.read_bytes()).hexdigest()
            if self.settings.runner == "official_baseline"
            and self.settings.official_agent_config
            else candidate_digest
        )
        count = getattr(self.settings, f"{dataset_role.value}_trials")

        def once(case: Case, stop: Event) -> CaseExecution:
            attempts = []
            for index in range(count):
                store = (
                    TrialStore(
                        self.settings.artifact_dir,
                        protocol=identity,
                        target_sha256=digest,
                        case_id=case.id,
                        trial_index=index,
                        candidate=target_dir.name,
                    )
                    if self.settings.artifact_dir
                    else None
                )
                trial = store.resume() if store else None
                if trial is None:
                    if store:
                        store.save("running")
                    try:
                        value = upstream(
                            self.settings,
                            "trial",
                            [case.id],
                            target=target_dir,
                            stop=stop,
                        )
                        trial = CaseAttempt.model_validate(value)
                    except BaseException as error:
                        if store:
                            store.save(
                                "failed",
                                diagnostic={
                                    "exception_type": type(error).__name__,
                                    "process": getattr(
                                        error, "process_diagnostic", None
                                    ),
                                },
                            )
                        raise
                if (
                    not isinstance(trial.tracelog, dict)
                    or tree_digest(target_dir) != candidate_digest
                ):
                    raise ValueError("Missing evidence or mutated candidate")
                trial = trial.model_copy(
                    update={
                        "tracelog": {
                            **trial.tracelog,
                            "protocol": identity,
                            "target_sha256": digest,
                        }
                    }
                )
                if store:
                    store.save("succeeded", execution=trial)
                attempts.append(trial)
            first = attempts[0]
            return CaseExecution(
                id=case.id,
                result=first.result,
                tracelog=first.tracelog,
                metadata=ExecutionMetadata(
                    total_tokens=sum(t.metadata.total_tokens for t in attempts),
                    duration_seconds=sum(t.metadata.duration_seconds for t in attempts),
                ),
                trials=tuple(attempts) if count > 1 else (),
            )

        return run_cases(
            dataset.cases,
            case_id=lambda case: case.id,
            execute_once=once,
            max_case_concurrency=max_case_concurrency,
            max_retries=0,
            continue_on_error=True,
            retry_delay_seconds=retry_delay_seconds,
            cancel_event=cancel_event,
        )


def trial_feedback(
    domain: str, trials: tuple[CaseAttempt, ...]
) -> tuple[str, str | None]:
    """选择领域对应的公开证据摘要，不导出评分器输入。"""
    if domain == "knowledge_work":
        return summarize_office_trials(
            [(t.result, t.tracelog) for t in trials if isinstance(t.tracelog, dict)]
        )
    if domain == "information_retrieval":
        return summarize_retrieval_trials(
            [(t.result, t.tracelog) for t in trials if isinstance(t.tracelog, dict)]
        )
    return "Official verification completed for every trial", None


class DomainEvaluator:
    """汇总产物仍可访问时记录的官方评分结果。"""

    def __init__(self, settings: DomainSettings) -> None:
        """绑定冻结的评分协议，不创建执行环境。

        Args:
            settings: 用于校验已有评分证据的领域配置。
        """
        self.settings = settings

    def evaluate(
        self, execution: ExecutionResult, dataset_path: Path
    ) -> EvaluationResult:
        """汇总通过校验的官方评分证据，不重新执行或重新评分案例。

        Args:
            execution: 包含官方评分证据的完整案例执行结果。
            dataset_path: 数据集清单，其角色决定预期试次数。

        Returns:
            平均任务通过率、各试次分数及公开诊断反馈。

        Raises:
            ValueError: 案例覆盖、协议、试次数或官方评分证据无效。
        """
        dataset = load_dataset(dataset_path, None, self.settings)
        if {c.id for c in execution.cases} != {c.id for c in dataset.cases} or len(
            execution.cases
        ) != len(dataset.cases):
            raise ValueError("Execution must exactly cover the dataset")
        identity = experiment_identity(self.settings, dataset_path)
        expected_count = getattr(self.settings, f"{dataset.role}_trials")
        rows = []
        for case in execution.cases:
            trials = case.trials or (
                CaseAttempt(
                    result=case.result, tracelog=case.tracelog, metadata=case.metadata
                ),
            )
            if case.attempts or len(trials) != expected_count:
                raise ValueError("Unexpected trial count or retries")
            scores = []
            rewards = []
            for trial in trials:
                trace = trial.tracelog
                if (
                    not isinstance(trace, dict)
                    or trace.get("protocol") != identity
                    or trace.get("status") != "succeeded"
                ):
                    raise ValueError("Protocol identity mismatch")
                verdict = trace.get("verifier")
                if not isinstance(verdict, dict):
                    raise ValueError("Missing official verifier evidence")
                score = verdict.get("score")
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not 0 <= score <= 1
                ):
                    raise ValueError("Invalid official score")
                passed = verdict.get("passed")
                threshold = verdict.get("pass_threshold")
                if (
                    not isinstance(passed, bool)
                    or not isinstance(threshold, (int, float))
                    or passed != (score > threshold)
                ):
                    raise ValueError("Missing official pass-threshold evidence")
                rewards.append(float(score))
                scores.append(float(passed))
            feedback, failure_kind = trial_feedback(self.settings.domain, trials)
            rows.append(
                CaseEvaluation(
                    id=case.id,
                    result=case.result,
                    tracelog={
                        "execution": case.model_dump(mode="json"),
                        "scores": scores,
                        "rewards": rewards,
                    },
                    metadata=case.metadata,
                    expected=None,
                    score=sum(scores) / len(scores),
                    feedback=feedback,
                    failure_kind=failure_kind,
                )
            )
        return EvaluationResult(
            total_score=sum(row.score for row in rows) / len(rows), cases=tuple(rows)
        )
