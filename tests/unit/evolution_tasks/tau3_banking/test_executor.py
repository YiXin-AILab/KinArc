"""Tau3 Banking 顺序 Executor 与数据隔离测试。"""

import asyncio
import json
import logging
import shutil
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from agentsystem.common.errors import AdapterError
from evolution.models import CaseExecution, DatasetRole, ExecutionMetadata
from evolution.task import DatasetCase, load_task, load_task_config
from evolution_tasks.tau3_banking._internal.process import (
    CandidateServer,
    RunnerError,
    RunnerRequest,
    RunnerResult,
    Tau3BankingConfig,
)
from evolution_tasks.tau3_banking.executor import (
    BankingExecutor,
    cleanup_redis_prefix,
)
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def isolated_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度单元测试不依赖真实环境；预检另有真实边界测试。"""
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.executor.validate_execution_environment",
        lambda *args, **kwargs: None,
        raising=False,
    )


@pytest.mark.parametrize("fallback", [False, True])
def test_golden_cases_keep_private_policy_and_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    """并发 golden case 的应用目录与文档路径独立，结束后完整回收。"""
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.executor._UNIX_SOCKET_PATH_LIMIT",
        1 if fallback else 10000,
    )
    bridge_root = (Path("/tmp") if fallback else tmp_path) / ".evolution"
    config = _config(tmp_path).model_copy(
        update={
            "agent_id": "banking-golden-agent",
            "retrieval_config": "golden_retrieval",
        }
    )
    barrier = threading.Barrier(2)
    paths: list[Path] = []

    def server_factory(**kwargs: object) -> _Server:
        root = kwargs["applications_root"]
        assert isinstance(root, Path)
        assert (root / "banking_golden_agent/config/agent.yaml").is_file()
        return _Server(kwargs, [])

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        assert request.agent_id == "banking-golden-agent"
        assert request.retrieval_config == "golden_retrieval"
        path = request.tool_bridge_socket.with_suffix(".policy.md")
        assert path.parent == bridge_root
        request.tool_bridge_socket.touch()
        paths.append(path)
        path.write_text(str(request.case["id"]))
        barrier.wait(timeout=5)
        assert path.read_text() == request.case["id"]
        return RunnerResult(
            case_id=str(request.case["id"]),
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=0,
            duration_seconds=0,
        )

    result = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=server_factory,
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _dataset(tmp_path),
        DatasetRole.TRAIN,
        max_case_concurrency=2,
        max_retries=0,
    )
    assert len(result.cases) == 2
    assert len(set(paths)) == 2
    assert all(not path.exists() for path in paths)
    assert all(not path.with_suffix("").with_suffix(".sock").exists() for path in paths)
    assert not list(tmp_path.glob(".t3b-*"))


def _execution(case_id: str) -> CaseExecution:
    return CaseExecution(
        id=case_id,
        result="completed",
        tracelog={},
        metadata=ExecutionMetadata(total_tokens=0, duration_seconds=0),
    )


def test_bounded_workers_overlap_and_return_dataset_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = BankingExecutor(config=_config(tmp_path), project_root=tmp_path)
    entered = {key: threading.Event() for key in ("task_001", "task_002")}
    release = {key: threading.Event() for key in entered}
    finished_second = threading.Event()

    def run(candidate: Path, case: DatasetCase, stop: threading.Event) -> CaseExecution:
        entered[case.id].set()
        assert release[case.id].wait(5)
        if case.id == "task_002":
            finished_second.set()
        return _execution(case.id)

    monkeypatch.setattr(executor, "_execute_case", run)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            executor.execute,
            _candidate(tmp_path),
            _dataset(tmp_path),
            DatasetRole.TRAIN,
            max_retries=0,
            max_case_concurrency=2,
        )
        try:
            assert all(event.wait(2) for event in entered.values())
            release["task_002"].set()
            assert finished_second.wait(2)
        finally:
            for event in release.values():
                event.set()
        assert [case.id for case in future.result(2).cases] == ["task_001", "task_002"]


@pytest.mark.parametrize("fatal", [False, True])
def test_cancel_or_fatal_error_stops_pending_and_drains_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal: bool,
) -> None:
    executor = BankingExecutor(config=_config(tmp_path), project_root=tmp_path)
    dataset = _dataset(tmp_path)
    dataset.write_text(json.dumps([{"id": f"task_00{i}"} for i in (1, 2, 3)]))
    gate = threading.Barrier(3, timeout=3)
    external = threading.Event()
    drained = threading.Event()
    started: list[str] = []

    def run(candidate: Path, case: DatasetCase, stop: threading.Event) -> CaseExecution:
        started.append(case.id)
        gate.wait()
        if fatal and case.id == "task_001":
            raise RuntimeError("cleanup failed")
        assert stop.wait(3)
        drained.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(executor, "_execute_case", run)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            executor.execute,
            _candidate(tmp_path),
            dataset,
            DatasetRole.TRAIN,
            max_retries=0,
            max_case_concurrency=2,
            cancel_event=external,
        )
        gate.wait()
        if not fatal:
            external.set()
        with pytest.raises(RuntimeError if fatal else asyncio.CancelledError):
            future.result(4)
    assert set(started) == {"task_001", "task_002"}
    assert drained.is_set()
    assert external.is_set() is (not fatal)


@pytest.mark.parametrize("fail", [False, True])
def test_candidate_copies_are_cleaned_after_concurrent_cases(
    tmp_path: Path,
    fail: bool,
) -> None:
    """临时副本可被回收，源文件权限不变；清理不覆盖执行异常。"""
    candidate = _candidate(tmp_path)
    modes = {path: path.stat().st_mode for path in [candidate, *candidate.rglob("*")]}
    barrier = threading.Barrier(2, timeout=3)

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        barrier.wait()
        if fail:
            raise RuntimeError("original runner failure")
        return RunnerResult(
            case_id=str(request.case["id"]),
            simulation={"termination_reason": "agent_stop"},
            candidate_events=(),
            total_tokens=0,
            duration_seconds=0,
        )

    executor = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    )
    if fail:
        with pytest.raises(RuntimeError, match="original runner failure"):
            executor.execute(
                candidate,
                _dataset(tmp_path),
                DatasetRole.TRAIN,
                max_retries=0,
                max_case_concurrency=2,
            )
    else:
        result = executor.execute(
            candidate,
            _dataset(tmp_path),
            DatasetRole.TRAIN,
            max_retries=0,
            max_case_concurrency=2,
        )
        assert [case.result for case in result.cases] == ["completed", "completed"]
    assert not list((tmp_path / ".evolution").glob(".tau3-banking-case-*"))
    assert not list(tmp_path.glob(".tau3-banking-case-*"))
    assert all(path.stat().st_mode == mode for path, mode in modes.items())


@pytest.mark.parametrize("retry", [False, True])
def test_executor_uses_fresh_resources_for_each_attempt(
    tmp_path: Path,
    retry: bool,
) -> None:
    """同一 Case 重试也必须先回收旧资源，再创建独立副本。"""
    candidate = _candidate(tmp_path)
    dataset = _dataset(tmp_path)
    config = _config(tmp_path)
    lifecycle: list[str] = []
    cleaned: list[str] = []
    seen_requests: list[RunnerRequest] = []
    copied_flags: list[bool] = []
    socket_paths: list[Path] = []
    application_paths: list[Path] = []

    def server_factory(
        *,
        config: Tau3BankingConfig,
        project_root: Path,
        applications_root: Path,
        redis_prefix: str,
        tool_bridge_socket: Path,
        cancel_event: threading.Event | None = None,
    ) -> _Server:
        del config
        assert len(cleaned) == len(socket_paths)
        assert all(not path.exists() for path in application_paths)
        application_paths.append(applications_root)
        applications = applications_root
        assert applications.is_relative_to(project_root)
        assert applications.name == "application"
        assert (applications / "__init__.py").is_file()
        copied_database = json.loads(
            (applications / "banking_agent" / "data" / "db.json").read_text()
        )
        copied_flags.append(copied_database["task_config"]["data"]["flag"])
        assert tool_bridge_socket.is_absolute()
        assert tool_bridge_socket.parent in {
            tmp_path / ".evolution",
            Path("/tmp/.evolution"),
        }
        assert tool_bridge_socket.name.startswith(".t3b-")
        assert tool_bridge_socket.suffix == ".sock"
        socket_paths.append(tool_bridge_socket)
        return _Server({"redis_prefix": redis_prefix}, lifecycle)

    def runner(
        received_config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        assert received_config is config
        assert lifecycle[-1].startswith("start:")
        seen_requests.append(request)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(request.tool_bridge_socket))
        return RunnerResult(
            case_id=str(request.case["id"]),
            simulation={
                "termination_reason": "agent_error"
                if retry and len(seen_requests) == 1
                else "user_stop"
            },
            candidate_events=({"type": "run.completed"},),
            total_tokens=7,
            duration_seconds=0.25,
        )

    executor = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=server_factory,
        runner=runner,
        cleanup_prefix=lambda _, prefix: cleaned.append(prefix),
    )

    execution = executor.execute(
        candidate,
        dataset,
        DatasetRole.TRAIN,
        max_retries=int(retry),
        retry_delay_seconds=0,
    )

    assert [case.id for case in execution.cases] == ["task_001", "task_002"]
    assert [case.metadata.total_tokens for case in execution.cases] == [
        7 * (1 + retry),
        7,
    ]
    count = 2 + int(retry)
    assert [item.split(":", 1)[0] for item in lifecycle] == ["start", "stop"] * count
    prefixes = [item.split(":", 1)[1] for item in lifecycle if item.startswith("start")]
    assert len(set(prefixes)) == count
    assert cleaned == prefixes
    assert copied_flags == [False] * count
    assert len(set(socket_paths)) == count
    assert len(execution.cases[0].attempts) == (2 if retry else 0)
    assert [request.case["id"] for request in seen_requests] == ["task_001"] * (
        1 + retry
    ) + ["task_002"]
    assert [request.tool_bridge_socket for request in seen_requests] == socket_paths
    assert all(not socket_path.exists() for socket_path in socket_paths)
    assert (
        json.loads((candidate / "data" / "db.json").read_text())["task_config"]["data"][
            "flag"
        ]
        is False
    )


def test_executor_runs_all_cases_in_dataset_order(tmp_path: Path) -> None:
    """执行整份 Dataset，保持稳定顺序。"""
    seen: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        seen.append(case_id)
        return RunnerResult(
            case_id=case_id,
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=1,
            duration_seconds=0.1,
        )

    execution = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _dataset(tmp_path),
        DatasetRole.TRAIN,
        max_retries=0,
    )

    assert seen == ["task_001", "task_002"]
    assert tuple(case.id for case in execution.cases) == ("task_001", "task_002")


def test_validation_role_repeats_each_case_with_isolated_trials(
    tmp_path: Path,
) -> None:
    """Validation Case 按配置重复采样，合并为带 Trials 的单条事实。"""
    config = _config(tmp_path).model_copy(update={"validation_num_trials": 3})
    lifecycle: list[str] = []
    calls: list[str] = []

    def server_factory(**kwargs: object) -> _Server:
        return _Server(kwargs, lifecycle)

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        return RunnerResult(
            case_id=case_id,
            simulation={
                "termination_reason": "user_stop",
                "messages": [{"role": "user", "content": f"hi-{len(calls)}"}],
            },
            candidate_events=(),
            total_tokens=len(calls),
            duration_seconds=0.1,
        )

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=server_factory,
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.VALIDATION,
        max_retries=0,
    )

    assert calls == ["task_001"] * 3
    case = execution.cases[0]
    assert len(case.trials) == 3
    assert case.attempts == ()
    assert case.retryable is False
    assert case.result == "completed" == case.trials[0].result
    assert case.tracelog == case.trials[0].tracelog
    assert case.metadata.total_tokens == 1 + 2 + 3
    assert case.metadata.duration_seconds == pytest.approx(0.3)
    prefixes = [item.split(":", 1)[1] for item in lifecycle if item.startswith("start")]
    assert len(set(prefixes)) == 3


def test_train_role_keeps_single_execution_with_trials_configured(
    tmp_path: Path,
) -> None:
    """Train/Test 不受 validation_num_trials 影响，行为完全不变。"""
    config = _config(tmp_path).model_copy(update={"validation_num_trials": 3})
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        return RunnerResult(
            case_id=case_id,
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=1,
            duration_seconds=0.1,
        )

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.TRAIN,
        max_retries=0,
    )

    assert calls == ["task_001"]
    case = execution.cases[0]
    assert case.trials == ()
    assert case.result == "completed"
    assert case.metadata.total_tokens == 1


def test_validation_trial_failure_scores_without_bundle_retry(tmp_path: Path) -> None:
    """单次 Trial 的 Runner 失败只计入该 Trial，不触发整案重试。"""
    config = _config(tmp_path).model_copy(update={"validation_num_trials": 2})
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        case_id = str(request.case["id"])
        calls.append(case_id)
        if len(calls) == 2:
            raise AdapterError(
                operation="tau3.runner",
                code="tau3_runner_timeout",
                message="Tau3 Runner 执行超时",
            )
        return RunnerResult(
            case_id=case_id,
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=5,
            duration_seconds=0.5,
        )

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.VALIDATION,
        max_retries=3,
        retry_delay_seconds=0,
    )

    assert calls == ["task_001"] * 2
    case = execution.cases[0]
    assert case.retryable is False
    assert case.result == "completed"
    assert tuple(trial.result for trial in case.trials) == ("completed", "timeout")
    assert case.metadata.total_tokens == 5
    assert case.metadata.duration_seconds == pytest.approx(1.5)


def test_executor_always_closes_server_and_cleans_owned_prefix(
    tmp_path: Path,
) -> None:
    """Runner 异常不得跳过精确 prefix 清理。"""
    lifecycle: list[str] = []
    cleaned: list[str] = []

    def server_factory(
        *,
        config: Tau3BankingConfig,
        project_root: Path,
        applications_root: Path,
        redis_prefix: str,
        tool_bridge_socket: Path,
        cancel_event: threading.Event | None = None,
    ) -> _Server:
        del config, project_root, applications_root, tool_bridge_socket
        return _Server({"redis_prefix": redis_prefix}, lifecycle)

    def failing_runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config, request
        raise TimeoutError("runner timeout")

    executor = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=server_factory,
        runner=failing_runner,
        cleanup_prefix=lambda _, prefix: cleaned.append(prefix),
    )

    with pytest.raises(TimeoutError):
        executor.execute(
            _candidate(tmp_path),
            _one_case_dataset(tmp_path),
            DatasetRole.TRAIN,
            max_retries=0,
        )

    assert [item.split(":", 1)[0] for item in lifecycle] == ["start", "stop"]
    assert cleaned == [lifecycle[0].split(":", 1)[1]]


def test_executor_records_case_timeout_and_continues(tmp_path: Path) -> None:
    """单 Case Runner 超时必须记为失败，并继续执行后续 Case。"""
    seen: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        seen.append(case_id)
        if case_id == "task_001":
            raise AdapterError(
                operation="tau3.runner",
                code="tau3_runner_timeout",
                message="Tau3 Runner 执行超时",
            )
        return RunnerResult(
            case_id=case_id,
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=7,
            duration_seconds=0.25,
        )

    execution = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path), _dataset(tmp_path), DatasetRole.TRAIN, max_retries=0
    )

    assert seen == ["task_001", "task_002"]
    timed_out, completed = execution.cases
    assert timed_out.result == "timeout"
    tracelog = timed_out.tracelog
    assert isinstance(tracelog, dict)
    simulation = tracelog["simulation"]
    assert isinstance(simulation, dict)
    assert simulation["id"] == "task_001-timeout"
    assert simulation["task_id"] == "task_001"
    assert simulation["duration"] == 1
    assert simulation["termination_reason"] == "timeout"
    assert simulation["messages"] == []
    assert isinstance(simulation["start_time"], str)
    assert isinstance(simulation["end_time"], str)
    assert tracelog["candidate_events"] == []
    assert tracelog["error"] == {
        "code": "tau3_runner_timeout",
        "message": "Tau3 Runner 执行超时",
    }
    assert timed_out.metadata.total_tokens == 0
    assert timed_out.metadata.duration_seconds == 1
    assert timed_out.retryable is True
    assert completed.result == "completed"
    assert completed.retryable is False


def test_executor_records_runner_failure_and_continues(tmp_path: Path) -> None:
    """Runner 协议失败必须只结束当前 Case。"""
    seen: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        seen.append(case_id)
        if case_id == "task_001":
            raise AdapterError(
                operation="tau3.runner",
                code="tau3_runner_failed",
                message="Tau3 Runner 返回非零状态",
            )
        return RunnerResult(
            case_id=case_id,
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=7,
            duration_seconds=0.25,
        )

    execution = BankingExecutor(
        config=_config(tmp_path).model_copy(update={"max_reshoot_rounds": 0}),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path), _dataset(tmp_path), DatasetRole.TRAIN, max_retries=0
    )

    assert seen == ["task_001", "task_002"]
    failed, completed = execution.cases
    assert failed.result == "agent_error"
    tracelog = failed.tracelog
    assert isinstance(tracelog, dict)
    simulation = tracelog["simulation"]
    assert isinstance(simulation, dict)
    assert simulation["id"] == "task_001-agent-error"
    assert simulation["task_id"] == "task_001"
    assert simulation["duration"] == 0.0
    assert simulation["termination_reason"] == "agent_error"
    assert simulation["messages"] == []
    assert isinstance(simulation["start_time"], str)
    assert isinstance(simulation["end_time"], str)
    assert tracelog["candidate_events"] == []
    assert tracelog["error"] == {
        "code": "tau3_runner_failed",
        "message": "Tau3 Runner 返回非零状态",
    }
    assert failed.metadata.total_tokens == 0
    assert failed.metadata.duration_seconds == 0
    assert failed.retryable is True
    assert completed.result == "completed"


def test_executor_persists_structured_candidate_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Runner 返回的公开失败必须成为可评分的 CaseExecution。"""

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        return RunnerResult(
            case_id=str(request.case["id"]),
            simulation={"termination_reason": "agent_error", "messages": []},
            error=RunnerError(
                code="context_compaction_failed",
                message="Context compaction failed",
                exception_type="RuntimeError",
                stacktrace="Traceback...\nRuntimeError: compaction failed\n",
                error_id="error-123",
            ),
            candidate_events=({"type": "run.failed", "payload": {}},),
            total_tokens=123,
            duration_seconds=4.5,
        )

    execution = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.TRAIN,
        max_retries=0,
    )

    case = execution.cases[0]
    assert "case_attempt_failed case=task_001 result=agent_error" in caplog.text
    assert "code=context_compaction_failed" in caplog.text
    assert "error_id=error-123" in caplog.text
    assert case.result == "agent_error"
    assert case.tracelog == {
        "simulation": {"termination_reason": "agent_error", "messages": []},
        "error": {
            "code": "context_compaction_failed",
            "message": "Context compaction failed",
            "exception_type": "RuntimeError",
            "stacktrace": "Traceback...\nRuntimeError: compaction failed\n",
            "error_id": "error-123",
        },
        "candidate_events": [{"type": "run.failed", "payload": {}}],
    }
    assert case.metadata.total_tokens == 123
    assert case.metadata.duration_seconds == 4.5
    assert case.retryable is True
    assert "case_attempt_failed case=task_001 result=agent_error" in caplog.text
    assert "code=context_compaction_failed" in caplog.text
    assert "error_id=error-123" in caplog.text
    assert "RuntimeError: compaction failed\n" in caplog.text


@pytest.mark.parametrize(
    ("termination_reason", "expected_retryable"),
    [
        ("user_stop", False),
        ("agent_stop", False),
        ("timeout", True),
        ("agent_error", True),
        ("user_error", True),
        (None, True),
    ],
)
def test_executor_marks_only_normal_stops_nonretryable(
    tmp_path: Path,
    termination_reason: str | None,
    expected_retryable: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """除 user_stop 和 agent_stop 外，所有终态都允许 Engine 重试。"""

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        simulation = {}
        if termination_reason is not None:
            simulation["termination_reason"] = termination_reason
        return RunnerResult(
            case_id=str(request.case["id"]),
            simulation=simulation,
            candidate_events=(),
            total_tokens=1,
            duration_seconds=0.1,
        )

    execution = BankingExecutor(
        config=_config(tmp_path).model_copy(update={"max_reshoot_rounds": 0}),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.TRAIN,
        max_retries=0,
    )

    assert execution.cases[0].retryable is expected_retryable
    assert ("case_attempt_failed" in caplog.text) is expected_retryable
    if expected_retryable:
        assert "error=unavailable" in caplog.text


def _completed_result(case_id: str, *, tokens: int = 1) -> RunnerResult:
    return RunnerResult(
        case_id=case_id,
        simulation={"termination_reason": "user_stop"},
        candidate_events=(),
        total_tokens=tokens,
        duration_seconds=0.1,
    )


def _user_error_result(case_id: str, *, tokens: int = 1) -> RunnerResult:
    return RunnerResult(
        case_id=case_id,
        simulation={"termination_reason": "user_error"},
        candidate_events=(),
        total_tokens=tokens,
        duration_seconds=0.1,
    )


def test_validation_reshoot_skipped_when_ratio_meets_threshold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """有效比例达到阈值时不触发补跑，结果原样进入评分。"""
    caplog.set_level(logging.INFO)
    config = _config(tmp_path).model_copy(
        update={"validation_num_trials": 4, "min_valid_ratio": 0.75}
    )
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        if len(calls) == 4:
            return _user_error_result(case_id)
        return _completed_result(case_id)

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.VALIDATION,
        max_retries=0,
    )

    assert calls == ["task_001"] * 4
    case = execution.cases[0]
    assert len(case.trials) == 4
    assert [trial.result for trial in case.trials] == ["completed"] * 3 + ["user_error"]
    assert "reshoot_pass_started" not in caplog.text


def test_validation_reshoot_replaces_only_failed_trial_slot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """低于阈值时仅异常槽位被补跑结果原位替换，槽位总数不变。"""
    caplog.set_level(logging.INFO)
    config = _config(tmp_path).model_copy(update={"validation_num_trials": 2})
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        if len(calls) == 4:
            return _user_error_result(case_id)
        if len(calls) == 5:
            return RunnerResult(
                case_id=case_id,
                simulation={
                    "termination_reason": "user_stop",
                    "messages": [{"role": "user", "content": "reshot"}],
                },
                candidate_events=(),
                total_tokens=9,
                duration_seconds=0.9,
            )
        return _completed_result(case_id)

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _dataset(tmp_path),
        DatasetRole.VALIDATION,
        max_retries=0,
    )

    assert calls == ["task_001"] * 2 + ["task_002"] * 3
    first, second = execution.cases
    assert [trial.result for trial in first.trials] == ["completed"] * 2
    assert len(second.trials) == 2
    assert [trial.result for trial in second.trials] == ["completed"] * 2
    tracelog = second.trials[1].tracelog
    assert isinstance(tracelog, dict)
    simulation = tracelog["simulation"]
    assert isinstance(simulation, dict)
    assert simulation["messages"] == [{"role": "user", "content": "reshot"}]
    assert second.trials[1].metadata.total_tokens == 9
    assert second.metadata.total_tokens == 1 + 9
    assert second.metadata.duration_seconds == pytest.approx(0.1 + 0.9)
    assert (
        "reshoot_pass_started round=1 role=validation valid=3/4 cases=1 units=1"
        in caplog.text
    )
    assert "reshoot_pass_completed round=1 role=validation valid=4/4" in caplog.text


def test_timeout_results_are_never_reshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 自然超时是 Agent 真实表现，即使拉低有效比例也零额外执行。"""
    caplog.set_level(logging.INFO)
    config = _config(tmp_path).model_copy(update={"validation_num_trials": 2})
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        return RunnerResult(
            case_id=case_id,
            simulation={"termination_reason": "timeout"},
            candidate_events=(),
            total_tokens=1,
            duration_seconds=0.1,
        )

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _one_case_dataset(tmp_path),
        DatasetRole.VALIDATION,
        max_retries=0,
    )

    assert calls == ["task_001"] * 2
    assert [trial.result for trial in execution.cases[0].trials] == ["timeout"] * 2
    assert "reshoot_pass_started" not in caplog.text


def test_train_reshoot_replaces_case_and_appends_attempts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Train 低于阈值时整条补跑替换结果，原 Attempts 保留并追加补跑记录。"""
    caplog.set_level(logging.INFO)
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        if case_id == "task_001" and calls.count(case_id) <= 2:
            return _user_error_result(case_id, tokens=len(calls))
        return _completed_result(case_id, tokens=len(calls))

    execution = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path),
        _dataset(tmp_path),
        DatasetRole.TRAIN,
        max_retries=1,
        retry_delay_seconds=0,
    )

    assert calls == ["task_001", "task_001", "task_002", "task_001"]
    reshot, untouched = execution.cases
    assert reshot.result == "completed"
    assert len(reshot.attempts) == 3
    assert [attempt.result for attempt in reshot.attempts] == [
        "user_error",
        "user_error",
        "completed",
    ]
    assert reshot.metadata.total_tokens == 1 + 2 + 4
    assert untouched.result == "completed"
    assert untouched.attempts == ()
    assert untouched.metadata.total_tokens == 3
    assert (
        "reshoot_pass_started round=1 role=train valid=1/2 cases=1 units=1"
        in caplog.text
    )
    assert "reshoot_pass_completed round=1 role=train valid=2/2" in caplog.text


def test_train_reshoot_skipped_when_all_completed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Train 全部 completed 时有效比例为 1，不触发补跑。"""
    caplog.set_level(logging.INFO)
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        return _completed_result(case_id)

    execution = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path), _dataset(tmp_path), DatasetRole.TRAIN, max_retries=0
    )

    assert calls == ["task_001", "task_002"]
    assert [case.result for case in execution.cases] == ["completed"] * 2
    assert "reshoot_pass_started" not in caplog.text


@pytest.mark.parametrize(
    ("max_reshoot_rounds", "expected_calls", "expected_attempts"),
    [(0, 2, 0), (2, 6, 3)],
)
def test_reshoot_respects_max_rounds(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    max_reshoot_rounds: int,
    expected_calls: int,
    expected_attempts: int,
) -> None:
    """补跑轮数受配置上限约束；0 表示完全关闭补跑。"""
    caplog.set_level(logging.INFO)
    config = _config(tmp_path).model_copy(
        update={"max_reshoot_rounds": max_reshoot_rounds}
    )
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        return _user_error_result(case_id)

    execution = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path), _dataset(tmp_path), DatasetRole.TRAIN, max_retries=0
    )

    assert len(calls) == expected_calls
    assert caplog.text.count("reshoot_pass_started") == max_reshoot_rounds
    for case in execution.cases:
        assert case.result == "user_error"
        assert len(case.attempts) == expected_attempts


def test_reshoot_propagates_cancellation(tmp_path: Path) -> None:
    """补跑沿用主批次取消语义，外部取消置位时终止整批。"""
    external = threading.Event()
    calls: list[str] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        del config
        case_id = str(request.case["id"])
        calls.append(case_id)
        if len(calls) == 1:
            return _user_error_result(case_id)
        if len(calls) == 3:
            external.set()
        return _completed_result(case_id)

    with pytest.raises(asyncio.CancelledError):
        BankingExecutor(
            config=_config(tmp_path),
            project_root=tmp_path,
            server_factory=lambda **kwargs: _Server(kwargs, []),
            runner=runner,
            cleanup_prefix=lambda *_: None,
        ).execute(
            _candidate(tmp_path),
            _dataset(tmp_path),
            DatasetRole.TRAIN,
            max_retries=0,
            cancel_event=external,
        )
    assert calls == ["task_001", "task_002", "task_001"]


def test_reshoot_config_validation(tmp_path: Path) -> None:
    """门控配置越界时在启动前失败。"""
    base = _config(tmp_path).model_dump()
    assert base["min_valid_ratio"] == 0.85
    assert base["max_reshoot_rounds"] == 2
    for patch in (
        {"min_valid_ratio": 1.5},
        {"min_valid_ratio": -0.1},
        {"max_reshoot_rounds": -1},
    ):
        with pytest.raises(ValidationError):
            Tau3BankingConfig.model_validate({**base, **patch})


def test_cleanup_refuses_prefix_not_owned_by_executor(tmp_path: Path) -> None:
    """Redis 清理边界必须在建立连接前拒绝任意外部 prefix。"""
    with pytest.raises(ValueError, match="非 Tau3"):
        cleanup_redis_prefix(_config(tmp_path), "agentsystem")


def test_independent_server_start_failure_does_not_abort_batch(tmp_path: Path) -> None:
    calls = 0

    class FailedServer(_Server):
        def __enter__(self) -> Self:
            raise AdapterError(
                operation="tau3.candidate.start",
                code="candidate_start_failed",
                message="start failed",
            )

    def factory(**kwargs: object) -> _Server:
        nonlocal calls
        calls += 1
        return FailedServer(kwargs, []) if calls == 1 else _Server(kwargs, [])

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        return RunnerResult(
            case_id=str(request.case["id"]),
            simulation={"termination_reason": "user_stop"},
            candidate_events=(),
            total_tokens=0,
            duration_seconds=0,
        )

    result = BankingExecutor(
        config=_config(tmp_path).model_copy(update={"max_reshoot_rounds": 0}),
        project_root=tmp_path,
        server_factory=factory,
        runner=runner,
        cleanup_prefix=lambda *_: None,
    ).execute(
        _candidate(tmp_path), _dataset(tmp_path), DatasetRole.TRAIN, max_retries=0
    )
    assert [case.result for case in result.cases] == ["agent_error", "completed"]


@pytest.mark.parametrize("code", ["case_cleanup_failed", "tau3_runner_result_invalid"])
def test_unsafe_runner_cleanup_and_invalid_protocol_are_fatal(
    tmp_path: Path,
    code: str,
) -> None:
    cleaned: list[str] = []
    sockets: list[Path] = []

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        sockets.append(request.tool_bridge_socket)
        request.tool_bridge_socket.touch()
        raise AdapterError(operation="tau3.runner", code=code, message="failed")

    executor = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: _Server(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda _, prefix: cleaned.append(prefix),
    )
    with pytest.raises(AdapterError) as caught:
        executor.execute(
            _candidate(tmp_path),
            _one_case_dataset(tmp_path),
            DatasetRole.TRAIN,
            max_retries=0,
        )
    assert caught.value.code == code
    unsafe = code == "case_cleanup_failed"
    assert bool(cleaned) is not unsafe
    assert bool(list((tmp_path / ".evolution").glob(".tau3-banking-case-*"))) is unsafe
    assert not list(tmp_path.glob(".tau3-banking-case-*"))
    try:
        assert all(path.exists() is unsafe for path in sockets)
    finally:
        for path in sockets:
            path.unlink(missing_ok=True)


def test_empty_dataset_does_not_start_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("empty batch started dependencies")

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking.executor.validate_execution_environment",
        forbidden,
    )
    dataset = _dataset(tmp_path)
    dataset.write_text("[]")
    result = BankingExecutor(config=_config(tmp_path), project_root=tmp_path).execute(
        _candidate(tmp_path),
        dataset,
        DatasetRole.TRAIN,
        max_retries=0,
        max_case_concurrency=2,
    )
    assert result.cases == ()


def test_runner_cleanup_failure_survives_secondary_server_close_error(
    tmp_path: Path,
) -> None:
    """Server 的后续关闭异常不能掩盖 Runner 尚未停止的安全事实。"""
    sockets: list[Path] = []

    class BrokenClose(_Server):
        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self._stopped = True
            raise OSError("server log close failed")

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        sockets.append(request.tool_bridge_socket)
        request.tool_bridge_socket.touch()
        raise AdapterError(
            operation="tau3.process.close",
            code="case_cleanup_failed",
            message="runner still alive",
        )

    cleaned: list[str] = []
    executor = BankingExecutor(
        config=_config(tmp_path),
        project_root=tmp_path,
        server_factory=lambda **kwargs: BrokenClose(kwargs, []),
        runner=runner,
        cleanup_prefix=lambda _, prefix: cleaned.append(prefix),
    )
    with pytest.raises(OSError):
        executor.execute(
            _candidate(tmp_path),
            _one_case_dataset(tmp_path),
            DatasetRole.TRAIN,
            max_retries=0,
        )
    assert not cleaned
    assert list((tmp_path / ".evolution").glob(".tau3-banking-case-*"))
    assert not list(tmp_path.glob(".tau3-banking-case-*"))
    try:
        assert sockets and all(path.exists() for path in sockets)
    finally:
        for path in sockets:
            path.unlink(missing_ok=True)


def test_task_loader_builds_real_banking_ports(
    tmp_path: Path,
) -> None:
    """测试自有配置通过通用 Loader 装配真实 Banking Factory。"""
    project_root = Path(__file__).parents[4]
    task_dir = tmp_path / "evolution_tasks" / "tau3_banking"
    shutil.copytree(
        project_root / "evolution_tasks" / "tau3_banking",
        task_dir,
        ignore=shutil.ignore_patterns(
            "tau2-bench", ".evolution", "__pycache__", "*.yaml", "datasets"
        ),
    )
    tau2 = task_dir / "tau2-bench"
    tau2.mkdir(parents=True)
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "config" / "runtime.yaml"
    runtime.parent.mkdir()
    runtime.write_text("redis: {}\n", encoding="utf-8")
    (tmp_path / "application" / "banking_agent").mkdir(parents=True)
    (task_dir / "config.yaml").write_text(
        "objective: test banking factories\n"
        "initial_target: ../../application/banking_agent\n"
        "workspace: {read_only: [], inaccessible: []}\n"
        "settings:\n"
        "  runtime_config: ../../config/runtime.yaml\n"
        "  user_llm: openai/test-user\n"
        "  judge_llm: openai/test-judge\n",
        encoding="utf-8",
    )
    datasets = task_dir / "datasets"
    datasets.mkdir()
    for role in DatasetRole:
        (datasets / f"{role.value}.json").write_text(
            json.dumps([{"id": role.value}]), encoding="utf-8"
        )

    task = load_task(load_task_config(task_dir, environ={}))

    assert task.executor.__class__.__name__ == "BankingExecutor"
    assert task.evaluator.__class__.__name__ == "BankingEvaluator"


class _Server(CandidateServer):
    def __init__(self, kwargs: dict[str, object], lifecycle: list[str]) -> None:
        self._stopped = True
        self._prefix = str(kwargs["redis_prefix"])
        self._lifecycle = lifecycle

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:32123"

    def __enter__(self) -> Self:
        self._stopped = False
        self._lifecycle.append(f"start:{self._prefix}")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self._stopped = True
        self._lifecycle.append(f"stop:{self._prefix}")


def _candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    (candidate / "data").mkdir(parents=True, exist_ok=True)
    (candidate / "config").mkdir()
    (candidate / "config" / "agent.yaml").write_text(
        "agent_id: banking-agent\n",
        encoding="utf-8",
    )
    (candidate / "data" / "db.json").write_text(
        json.dumps(
            {
                "users": {"data": {}},
                "task_config": {"data": {"flag": False}},
            }
        ),
        encoding="utf-8",
    )
    return candidate


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(
        json.dumps(
            [
                {"id": "task_001", "initial_state": None},
                {
                    "id": "task_002",
                    "initial_state": {
                        "initialization_data": {
                            "agent_data": {"task_config": {"data": {"flag": True}}}
                        }
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    return path


def _one_case_dataset(tmp_path: Path) -> Path:
    path = tmp_path / "one-case.json"
    path.write_text(json.dumps([{"id": "task_001"}]), encoding="utf-8")
    return path


def _config(tmp_path: Path) -> Tau3BankingConfig:
    tau2 = tmp_path / "tau2"
    tau2.mkdir(exist_ok=True)
    (tau2 / "pyproject.toml").write_text(
        '[project]\nname = "tau2"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("redis: {}\n", encoding="utf-8")
    return Tau3BankingConfig(
        tau2_project=tau2,
        runtime_config=runtime,
        judge_llm="openai/judge-model",
        user_llm="openai/user",
        host="127.0.0.1",
        agent_id="banking-agent",
        startup_timeout_seconds=1,
        case_timeout_seconds=1,
        shutdown_timeout_seconds=1,
    )
