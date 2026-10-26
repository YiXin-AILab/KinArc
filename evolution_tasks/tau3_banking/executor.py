"""Tau3 Banking Candidate 的逐 Case 隔离执行器。"""

import asyncio
import logging
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import RuntimeConfig
from agentsystem.redis.client import close_redis_client, create_redis_client
from evolution.execution import run_cases, validate_execution_policy
from evolution.models import (
    CaseAttempt,
    CaseExecution,
    DatasetRole,
    ExecutionMetadata,
    ExecutionResult,
)
from evolution.task import DatasetCase, load_json_cases

from evolution_tasks.tau3_banking._internal.process import (
    CandidateServer,
    RunnerError,
    RunnerRequest,
    RunnerResult,
    Tau3BankingConfig,
    check_cancelled,
    load_banking_config,
    run_tau2,
    validate_execution_environment,
)

_OWNED_PREFIX = re.compile(r"^tau3:[0-9a-f]{32}$")
# macOS limits AF_UNIX paths to roughly 104 bytes, including the terminator.
# Keep the normal project-local path for short worktree roots, but fall back to
# the system temporary directory when a deep worktree would exceed that limit.
_UNIX_SOCKET_PATH_LIMIT = 100
_LOGGER = logging.getLogger(__name__)
_CASE_FAILURE_CODES = frozenset(
    {
        "tau3_runner_timeout",
        "tau3_runner_failed",
        "candidate_start_failed",
        "candidate_start_timeout",
    }
)
_RESHOOTABLE_AGENT_ERROR_CODES = frozenset(
    {
        "tau3_runner_failed",
        "tau3_runner_timeout",
        "candidate_start_failed",
        "candidate_start_timeout",
        "execution_failed",
        "provider_error",
    }
)


class ServerFactory(Protocol):
    """构造一个精确 Case Server 的端口。"""

    def __call__(
        self,
        *,
        config: Tau3BankingConfig,
        project_root: Path,
        applications_root: Path,
        redis_prefix: str,
        tool_bridge_socket: Path,
        cancel_event: Event | None = None,
    ) -> CandidateServer:
        """创建尚未启动的 Server owner。"""
        ...


class Runner(Protocol):
    """可取消的单 case Runner 端口。"""

    def __call__(
        self,
        config: Tau3BankingConfig,
        request: RunnerRequest,
        /,
        *,
        cancel_event: Event | None = None,
    ) -> RunnerResult:
        """返回结果或在回收子进程后传播取消/错误。"""
        ...


type PrefixCleanup = Callable[[Tau3BankingConfig, str], None]


class BankingExecutor:
    """有界并发执行 Dataset，每个 Case 拥有独立进程与数据。"""

    def __init__(
        self,
        *,
        config: Tau3BankingConfig,
        project_root: Path,
        server_factory: ServerFactory | None = None,
        runner: Runner = run_tau2,
        cleanup_prefix: PrefixCleanup | None = None,
    ) -> None:
        """绑定 Case 执行依赖及资源清理函数，不启动外部服务。"""
        self._config = config
        self._project_root = project_root.resolve(strict=True)
        self._server_factory = server_factory or CandidateServer
        self._runner = runner
        self._cleanup_prefix = cleanup_prefix or cleanup_redis_prefix

    def execute(
        self,
        candidate_dir: Path,
        dataset_path: Path,
        dataset_role: DatasetRole,
        *,
        max_case_concurrency: int = 1,
        max_retries: int = 3,
        retry_delay_seconds: float = 2,
        cancel_event: Event | None = None,
    ) -> ExecutionResult:
        """解析 Dataset，并在每个并发位置内完成该 Case 的重试。

        整批完成后按 min_valid_ratio 门控，仅对可归因为基建的异常单元
        定向补跑（max_reshoot_rounds 轮上限，0 表示关闭）。

        Args:
            candidate_dir: 本次执行的 Candidate 应用快照目录。
            dataset_path: 包含唯一 Case ID 的 JSON 数据集。
            dataset_role: 仅 Validation 使用配置的独立采样次数。
            max_case_concurrency: 同时执行的 Case 上限。
            max_retries: 单 Case 首次执行后的重试次数上限。
            retry_delay_seconds: 重试间隔秒数。
            cancel_event: 调用方提供的协作取消信号。

        Returns:
            按数据集顺序排列的执行事实；空数据集返回空结果。

        Raises:
            ValueError: 执行参数、数据集、Candidate 或依赖预检无效。
            AdapterError: 不能收敛为 Case 结果的协议或资源清理错误。
            asyncio.CancelledError: 收到停止信号，清理后传播取消。
        """
        validate_execution_policy(
            max_case_concurrency, max_retries, retry_delay_seconds
        )
        check_cancelled(cancel_event)
        candidate = candidate_dir.resolve(strict=True)
        if not candidate.is_dir():
            raise ValueError("Candidate 必须是目录")
        cases = load_json_cases(dataset_path)
        if not cases:
            return ExecutionResult(cases=())
        validate_execution_environment(
            self._config,
            candidate,
            self._project_root,
            cancel_event=cancel_event,
        )
        _LOGGER.info(
            "case_batch_started candidate=%s role=%s cases=%s concurrency=%s",
            candidate.name,
            dataset_role.value,
            len(cases),
            max_case_concurrency,
        )
        num_trials = (
            self._config.validation_num_trials
            if dataset_role is DatasetRole.VALIDATION
            else 1
        )
        execute_once = (
            (lambda case, stop: self._execute_case(candidate, case, stop))
            if num_trials == 1
            else (
                lambda case, stop: self._execute_case_trials(
                    candidate, case, stop, num_trials
                )
            )
        )
        result = run_cases(
            cases,
            case_id=lambda case: case.id,
            execute_once=execute_once,
            max_case_concurrency=max_case_concurrency,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
            cancel_event=cancel_event,
        )
        return self._reshoot(
            candidate=candidate,
            cases=cases,
            result=result,
            dataset_role=dataset_role,
            num_trials=num_trials,
            max_case_concurrency=max_case_concurrency,
            retry_delay_seconds=retry_delay_seconds,
            cancel_event=cancel_event,
        )

    def _reshoot(
        self,
        *,
        candidate: Path,
        cases: tuple[DatasetCase, ...],
        result: ExecutionResult,
        dataset_role: DatasetRole,
        num_trials: int,
        max_case_concurrency: int,
        retry_delay_seconds: float,
        cancel_event: Event | None,
    ) -> ExecutionResult:
        """按全局有效比例门控，仅对可归因为基建的异常单元定向补跑。

        每轮先重算有效比例（Validation 为 Trial 级、Train 为 Case 级），达到
        min_valid_ratio 即接受；否则只补跑 _is_reshootable 的异常单元，补跑
        本身不再叠加整案重试（max_retries=0），最多 max_reshoot_rounds 轮。
        Case 自然超时（result == "timeout"）视为真实表现，永不补跑。
        """
        for round_no in range(1, self._config.max_reshoot_rounds + 1):
            valid, total = _count_valid_units(result)
            if total == 0 or valid / total >= self._config.min_valid_ratio:
                return result
            work = _collect_reshoot_work(result)
            if not work:
                return result
            _LOGGER.info(
                "reshoot_pass_started round=%s role=%s valid=%s/%s cases=%s units=%s",
                round_no,
                dataset_role.value,
                valid,
                total,
                len(work),
                sum(work.values()),
            )
            reshot = run_cases(
                [case for case in cases if case.id in work],
                case_id=lambda case: case.id,
                execute_once=self._reshoot_once(candidate, num_trials, work),
                max_case_concurrency=max_case_concurrency,
                max_retries=0,
                retry_delay_seconds=retry_delay_seconds,
                cancel_event=cancel_event,
            )
            result = _merge_reshoot(result, reshot.cases)
            valid, total = _count_valid_units(result)
            _LOGGER.info(
                "reshoot_pass_completed round=%s role=%s valid=%s/%s",
                round_no,
                dataset_role.value,
                valid,
                total,
            )
        return result

    def _reshoot_once(
        self,
        candidate: Path,
        num_trials: int,
        work: Mapping[str, int],
    ) -> Callable[[DatasetCase, Event], CaseExecution]:
        """构造补跑的 execute_once：Validation 只补缺失槽位，Train 整案重跑。"""
        if num_trials == 1:
            return lambda case, stop: self._execute_case(candidate, case, stop)

        def execute_trials(case: DatasetCase, stop: Event) -> CaseExecution:
            """按待补跑的槽位数执行 Case，只有一次时保留单次结果形态。"""
            needed = work[case.id]
            if needed == 1:
                return self._execute_case(candidate, case, stop)
            return self._execute_case_trials(candidate, case, stop, needed)

        return execute_trials

    def _execute_case_trials(
        self,
        candidate: Path,
        case: DatasetCase,
        stop: Event,
        num_trials: int,
    ) -> CaseExecution:
        """串行完成同一 Case 的多次独立重复采样并合并为一条事实。

        每次 Trial 复用 _execute_case 的完整隔离资源（独立 Server、Redis
        prefix 与临时目录）。单次 Trial 的 agent_error/timeout 只计入该
        Trial；硬性基础设施异常照旧抛出，由调度层按既有逻辑重试整个
        bundle。抖动由多次采样的均值吸收，合并结果不再整案重试。
        """
        executions = [
            self._execute_case(candidate, case, stop) for _ in range(num_trials)
        ]
        return _merge_trials(case.id, executions)

    def _execute_case(
        self,
        candidate: Path,
        case: DatasetCase,
        stop: Event,
    ) -> CaseExecution:
        """创建 Case 私有应用与服务；确认进程安全退出后清理关联资源。"""
        case_token = uuid4().hex
        prefix = f"tau3:{case_token}"
        bridge_directory = self._project_root / ".evolution"
        tool_bridge_socket = bridge_directory / f".t3b-{case_token[:12]}.sock"
        if len(str(tool_bridge_socket).encode()) >= _UNIX_SOCKET_PATH_LIMIT:
            bridge_directory = Path("/tmp/.evolution")
            tool_bridge_socket = bridge_directory / tool_bridge_socket.name
        check_cancelled(stop)
        bridge_directory.mkdir(parents=True, exist_ok=True)
        temporary_root = self._project_root / ".evolution"
        temporary_root.mkdir(exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=".tau3-banking-case-",
                dir=temporary_root,
            )
        )
        server: CandidateServer | None = None
        unsafe = False
        try:
            applications = temporary / "application"
            applications.mkdir()
            (applications / "__init__.py").touch()
            copied_target = applications / self._config.agent_id.replace("-", "_")
            shutil.copytree(candidate, copied_target)
            check_cancelled(stop)
            server = self._server_factory(
                config=self._config,
                project_root=self._project_root,
                applications_root=applications,
                redis_prefix=prefix,
                tool_bridge_socket=tool_bridge_socket,
                cancel_event=stop,
            )
            try:
                with server:
                    try:
                        result = self._run_case(
                            server.base_url,
                            case,
                            tool_bridge_socket,
                            stop,
                        )
                    except AdapterError as error:
                        unsafe = error.code == "case_cleanup_failed"
                        raise
            except AdapterError as error:
                unsafe = unsafe or error.code == "case_cleanup_failed"
                if error.code not in _CASE_FAILURE_CODES:
                    raise
                result = _failed_result(
                    case.id, error, self._config.case_timeout_seconds
                )
        finally:
            if not unsafe and (server is None or server.stopped):
                self._cleanup_prefix(self._config, prefix)
                tool_bridge_socket.unlink(missing_ok=True)
                tool_bridge_socket.with_suffix(".policy.md").unlink(missing_ok=True)
                shutil.rmtree(temporary)
        return _to_case_execution(case.id, result)

    def _run_case(
        self,
        base_url: str,
        case: DatasetCase,
        tool_bridge_socket: Path,
        stop: Event,
    ) -> RunnerResult:
        """执行一个 Case，并将 Runner 故障收敛为可评分失败。"""
        try:
            check_cancelled(stop)
            return self._runner(
                self._config,
                RunnerRequest(
                    case=case.content,
                    retrieval_config=self._config.retrieval_config,
                    tool_bridge_socket=tool_bridge_socket,
                    base_url=base_url,
                    app_id="tau3-banking",
                    agent_id=self._config.agent_id,
                    user_id=f"tau3-{case.id}",
                    user_llm=self._config.user_llm,
                    timeout_seconds=self._config.case_timeout_seconds,
                ),
                cancel_event=stop,
            )
        except AdapterError as error:
            if error.code not in _CASE_FAILURE_CODES:
                raise
            return _failed_result(case.id, error, self._config.case_timeout_seconds)


def _merge_trials(case_id: str, executions: list[CaseExecution]) -> CaseExecution:
    """把同一 Case 的多次独立执行合并为一条带 Trials 的事实。

    顶层 result/tracelog 取首个 Trial 作为代表，metadata 为全部 Trial 之和；
    retryable 固定为 False，抖动由均值吸收而不再整案重试。
    """
    trials = tuple(
        CaseAttempt(
            result=execution.result,
            tracelog=execution.tracelog,
            metadata=execution.metadata,
        )
        for execution in executions
    )
    return CaseExecution(
        id=case_id,
        result=executions[0].result,
        tracelog=executions[0].tracelog,
        metadata=ExecutionMetadata(
            total_tokens=sum(trial.metadata.total_tokens for trial in trials),
            duration_seconds=sum(trial.metadata.duration_seconds for trial in trials),
        ),
        retryable=False,
        trials=trials,
    )


def _count_valid_units(result: ExecutionResult) -> tuple[int, int]:
    """统计有效计分单元：有 Trials 按 Trial 计，否则按 Case 计。"""
    valid = 0
    total = 0
    for execution in result.cases:
        if execution.trials:
            total += len(execution.trials)
            valid += sum(trial.result == "completed" for trial in execution.trials)
        else:
            total += 1
            valid += execution.result == "completed"
    return valid, total


def _collect_reshoot_work(result: ExecutionResult) -> dict[str, int]:
    """返回需要补跑的 Case ID 到补跑单元数的映射。"""
    work: dict[str, int] = {}
    for execution in result.cases:
        if execution.trials:
            needed = sum(
                _is_reshootable(trial.result, trial.tracelog)
                for trial in execution.trials
            )
        else:
            needed = int(_is_reshootable(execution.result, execution.tracelog))
        if needed:
            work[execution.id] = needed
    return work


def _is_reshootable(result: str, tracelog: JsonValue) -> bool:
    """仅基建可归因的异常可补跑；Case 自然超时视为真实表现。"""
    if result == "completed":
        return False
    if result == "user_error":
        return True
    if result != "agent_error" or not isinstance(tracelog, dict):
        return False
    error = tracelog.get("error")
    return (
        isinstance(error, dict) and error.get("code") in _RESHOOTABLE_AGENT_ERROR_CODES
    )


def _merge_reshoot(
    result: ExecutionResult, reshot: tuple[CaseExecution, ...]
) -> ExecutionResult:
    """把补跑结果并回原批次；未受影响的 Case 原样保留。"""
    reshot_by_id = {case.id: case for case in reshot}
    return ExecutionResult(
        cases=tuple(
            _merge_case_reshoot(case, reshot_by_id[case.id])
            if case.id in reshot_by_id
            else case
            for case in result.cases
        )
    )


def _merge_case_reshoot(
    original: CaseExecution, reshot: CaseExecution
) -> CaseExecution:
    """Validation 原位替换异常 Trial 槽位；Train 整案替换并保留审计痕迹。"""
    if original.trials:
        replacements = iter(reshot.trials or (_to_attempt(reshot),))
        trials = tuple(
            next(replacements)
            if _is_reshootable(trial.result, trial.tracelog)
            else trial
            for trial in original.trials
        )
        return CaseExecution(
            id=original.id,
            result=trials[0].result,
            tracelog=trials[0].tracelog,
            metadata=_sum_metadata(trials),
            retryable=False,
            trials=trials,
        )
    attempts = (
        *(original.attempts or (_to_attempt(original),)),
        *(reshot.attempts or (_to_attempt(reshot),)),
    )
    return CaseExecution(
        id=original.id,
        result=reshot.result,
        tracelog=reshot.tracelog,
        metadata=_sum_metadata(attempts),
        retryable=reshot.retryable,
        attempts=attempts,
    )


def _to_attempt(execution: CaseExecution) -> CaseAttempt:
    """把单次 Case 执行折叠为一条 Attempt 事实。"""
    return CaseAttempt(
        result=execution.result,
        tracelog=execution.tracelog,
        metadata=execution.metadata,
    )


def _sum_metadata(attempts: tuple[CaseAttempt, ...]) -> ExecutionMetadata:
    """按全部计分单元重新累计资源消耗。"""
    return ExecutionMetadata(
        total_tokens=sum(attempt.metadata.total_tokens for attempt in attempts),
        duration_seconds=sum(attempt.metadata.duration_seconds for attempt in attempts),
    )


def _failed_result(case_id: str, error: AdapterError, timeout: float) -> RunnerResult:
    """将可记录的 Runner 或启动错误转为可评分的失败 Simulation。"""
    is_timeout = error.code == "tau3_runner_timeout"
    duration = timeout if is_timeout else 0.0
    return RunnerResult(
        case_id=case_id,
        simulation=_failure_simulation(
            case_id,
            duration,
            "timeout" if is_timeout else "agent_error",
        ),
        error=RunnerError(code=error.code, message=error.message),
        candidate_events=(),
        total_tokens=0,
        duration_seconds=duration,
    )


def _failure_simulation(
    case_id: str,
    duration_seconds: float,
    termination_reason: str,
) -> dict[str, JsonValue]:
    """构造可由 Tau2 1.0.1 官方 Evaluator 校验的失败终态。"""
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=duration_seconds)
    suffix = "timeout" if termination_reason == "timeout" else "agent-error"
    return {
        "id": f"{case_id}-{suffix}",
        "task_id": case_id,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration": duration_seconds,
        "termination_reason": termination_reason,
        "messages": [],
    }


def cleanup_redis_prefix(config: Tau3BankingConfig, prefix: str) -> None:
    """只清理当前 Executor 生成并验证过的精确 Redis namespace。"""
    if _OWNED_PREFIX.fullmatch(prefix) is None:
        raise ValueError("拒绝清理非 Tau3 所有的 Redis prefix")
    runtime = load_yaml_config(config.runtime_config, RuntimeConfig)
    asyncio.run(_delete_prefix(runtime, prefix))


async def _delete_prefix(runtime: RuntimeConfig, prefix: str) -> None:
    """扫描并删除指定命名空间的 Redis key，最后关闭客户端。"""
    client = await create_redis_client(runtime.redis)
    try:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
    finally:
        await close_redis_client(client)


def create_executor(
    task_dir: Path,
    settings: Mapping[str, JsonValue],
) -> BankingExecutor:
    """Task Loader 固定 Factory。"""
    entry_dir = Path(__file__).parent
    project_root = entry_dir.parents[1]
    return BankingExecutor(
        config=load_banking_config(task_dir, settings, entry_dir=entry_dir),
        project_root=project_root,
    )


def _to_case_execution(case_id: str, result: RunnerResult) -> CaseExecution:
    """校验 Case ID，将官方终态映射为结果并保留事件与错误证据。"""
    if result.case_id != case_id:
        raise ValueError("Tau3 RunnerResult Case ID 不一致")
    termination = result.simulation.get("termination_reason")
    if termination in {"agent_stop", "user_stop"}:
        status = "completed"
    elif isinstance(termination, str) and termination:
        status = termination
    else:
        status = "failed"
    tracelog: dict[str, JsonValue] = {
        "simulation": result.simulation,
        "candidate_events": list(result.candidate_events),
    }
    if result.error is not None:
        tracelog["error"] = result.error.model_dump(mode="json", exclude_none=True)
    if status != "completed":
        _log_case_failure(case_id, status, result.error)
    return CaseExecution(
        id=case_id,
        result=status,
        tracelog=tracelog,
        metadata=ExecutionMetadata(
            total_tokens=result.total_tokens,
            duration_seconds=result.duration_seconds,
        ),
        retryable=termination not in {"agent_stop", "user_stop"},
    )


def _log_case_failure(case_id: str, status: str, error: RunnerError | None) -> None:
    """记录触发 case retry 的结构化失败原因。"""
    if error is None:
        _LOGGER.warning(
            "case_attempt_failed case=%s result=%s error=unavailable",
            case_id,
            status,
        )
        return
    _LOGGER.warning(
        "case_attempt_failed case=%s result=%s code=%s message=%s "
        "exception_type=%s error_id=%s stacktrace=%s",
        case_id,
        status,
        error.code,
        error.message,
        error.exception_type,
        error.error_id,
        error.stacktrace or "unavailable",
    )


__all__ = [
    "BankingExecutor",
    "cleanup_redis_prefix",
    "create_executor",
]
