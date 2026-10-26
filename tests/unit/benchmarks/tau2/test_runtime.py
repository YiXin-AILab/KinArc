"""Runtime copies preserve source configuration and own process groups."""

import os
import select
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, cast

import pytest
import yaml
from benchmarks.tau2bench.config import IntegrationConfig
from benchmarks.tau2bench.runtime import CaseRuntime, stop_process


@pytest.mark.parametrize("hide_group", [False, True])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_stop_reaps_child_even_when_group_is_missing(
    monkeypatch: pytest.MonkeyPatch, hide_group: bool, ignore_term: bool
) -> None:
    """A live child must receive TERM and, if needed, KILL despite a missed group."""
    code = (
        "import signal,time\n"
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if ignore_term else "")
        + "print('ready',flush=True)\ntime.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == "ready"
        if hide_group:
            monkeypatch.setattr(
                subprocess,
                "run",
                lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 0, stdout="", stderr=""
                ),
            )
        stop_process(process, 0.2)
        expected_signal = signal.SIGKILL if ignore_term else signal.SIGTERM
        assert process.returncode == -expected_signal
        stop_process(process, 0.2)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        process.stdout.close()


@pytest.mark.parametrize("ps_failure", [None, "missing", "timeout"])
def test_stop_kills_descendant_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch, ps_failure: str | None
) -> None:
    """A descendant must stop before owned state can be deleted."""
    code = (
        "import os,signal,time\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " print(os.getpid(),flush=True)\n"
        " time.sleep(20)\n"
        "else:\n time.sleep(20)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child = int(process.stdout.readline())
    try:
        with monkeypatch.context() as patch:
            if ps_failure is not None:

                def broken_ps(
                    *args: object, **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    if ps_failure == "timeout":
                        raise subprocess.TimeoutExpired("ps", 1)
                    return subprocess.CompletedProcess("ps", 0, stdout="", stderr="")

                patch.setattr(subprocess, "run", broken_ps)
            try:
                # 成功路径需等待外部 ps 检查，避免负载下误判清理超时。
                stop_process(process, 0.5 if ps_failure is None else 0.2)
            except RuntimeError:
                # Linux may retain zombie descendants. Failed inspection must not
                # prevent KILL, but cannot certify that their group is stopped.
                assert ps_failure is not None
        process.wait(timeout=2)
        status = subprocess.run(
            ["ps", "-p", str(child), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert not status.stdout.strip() or status.stdout.strip().startswith("Z")
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        process.stdout.close()


def test_private_copy(tmp_path: Path) -> None:
    """Model and paths change only inside each private application."""
    source = tmp_path / "agent"
    (source / "config").mkdir(parents=True)
    original = {"agent_id": "banking-agent", "llm_model": {"model": "old"}}
    (source / "config/agent.yaml").write_text(yaml.safe_dump(original))
    runtime_path = tmp_path / "runtime.yaml"
    trace = {"enabled": True, "endpoint": "http://localhost:4318/v1/traces"}
    runtime_path.write_text(
        yaml.safe_dump({"redis": {"key_prefix": "old"}, "trace": trace})
    )
    config = IntegrationConfig(
        agent_dir=source,
        runtime_config=runtime_path,
        runtime_python=Path(sys.executable),
    )
    first = CaseRuntime(config, source, model="new", temperature=0.3)
    second = CaseRuntime(config, source, model="new", temperature=0.3)
    try:
        assert first.root != second.root
        assert first.prefix != second.prefix
        assert first.session_id != second.session_id
        copy = yaml.safe_load(
            (first.root / "application/agent/config/agent.yaml").read_text()
        )
        assert copy["llm_model"]["model"] == "new"
        assert yaml.safe_load((source / "config/agent.yaml").read_text()) == original
        assert first.environ["PYTHONPATH"].split(os.pathsep)[0] == str(first.root)
        assert (
            yaml.safe_load((first.root / "runtime.yaml").read_text())["trace"] == trace
        )
    finally:
        first.close()
        second.close()


def test_selected_agent_and_policy_are_case_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "banking_golden_agent"
    (source / "config").mkdir(parents=True)
    original = {"agent_id": "banking-golden-agent", "llm_model": {}}
    (source / "config/agent.yaml").write_text(yaml.safe_dump(original))
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text("redis: {}\n")
    config = IntegrationConfig(
        agent_dir=source,
        runtime_config=runtime_path,
        runtime_python=Path(sys.executable),
    )
    monkeypatch.setenv("TAU2_DOMAIN_POLICY_FILE", "/not-this-case.md")
    first = CaseRuntime(
        config, source, model="test", temperature=0, domain_policy="case-a"
    )
    second = CaseRuntime(
        config, source, model="test", temperature=0, domain_policy="case-b"
    )
    probe = CaseRuntime(config, source, model="test", temperature=0)
    try:
        assert first.agent_id == "banking-golden-agent"
        assert (
            first.root / "application/banking_golden_agent/config/agent.yaml"
        ).exists()
        first_file = Path(first.environ["TAU2_DOMAIN_POLICY_FILE"])
        second_file = Path(second.environ["TAU2_DOMAIN_POLICY_FILE"])
        assert first_file != second_file
        assert first_file.read_text() == "case-a"
        assert second_file.read_text() == "case-b"
        assert first_file.stat().st_mode & 0o777 == 0o600
        assert "TAU2_DOMAIN_POLICY_FILE" not in probe.environ
        assert os.environ["TAU2_DOMAIN_POLICY_FILE"] == "/not-this-case.md"
        assert yaml.safe_load((source / "config/agent.yaml").read_text()) == original
    finally:
        first.close()
        second.close()
        probe.close()
    assert not first_file.exists() and not second_file.exists()


def test_failed_start_preserves_result_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed Runtime writes directly to results and cleanup retains its log."""
    source = tmp_path / "agent"
    (source / "config").mkdir(parents=True)
    (source / "config/agent.yaml").write_text(
        yaml.safe_dump({"agent_id": "banking-agent", "llm_model": {}})
    )
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(yaml.safe_dump({"redis": {}}))
    config = IntegrationConfig(
        agent_dir=source,
        runtime_config=runtime_path,
        runtime_python=Path(sys.executable),
    )
    log_path = tmp_path / "results" / "runtime-task_006-attempt.log"
    runtime = CaseRuntime(
        config, source, model="test", temperature=0, log_path=log_path
    )
    popen = subprocess.Popen

    def start_child(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        return popen(
            [sys.executable, "-c", "print('diagnostic failure', flush=True)"],
            stdout=cast(BinaryIO, kwargs["stdout"]),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def fail_ready(path: Path, deadline: float) -> bool:
        assert runtime.process is not None
        runtime.process.wait(timeout=5)
        assert path == log_path
        raise RuntimeError("injected startup failure")

    monkeypatch.setattr(subprocess, "Popen", start_child)
    monkeypatch.setattr(runtime, "_wait_ready", fail_ready)
    monkeypatch.setattr(
        "benchmarks.tau2bench.runtime.worker_command", lambda *args: None
    )
    try:
        with pytest.raises(RuntimeError, match="injected startup failure"):
            runtime.start()
    finally:
        monkeypatch.setattr(subprocess, "Popen", popen)
        runtime.close()
    assert not runtime.root.exists()
    assert "diagnostic failure" in log_path.read_text()
    assert runtime.session_id in log_path.read_text()


def test_stop_reaps_real_process() -> None:
    """A bounded shutdown reaps the process group, including repeated close."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    stop_process(process, 0.2)
    stop_process(process, 0.2)
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


@pytest.mark.parametrize(
    "override",
    [
        {"asset_store": {"type": "s3"}},
        {"managers": {"message": {"type": "sql"}}},
        {"managers": {"state": {"type": "sql"}}},
    ],
)
def test_rejects_unsupported_storage(
    tmp_path: Path, override: dict[str, object]
) -> None:
    """Reject source storage before private overrides can hide invalid settings."""
    source = tmp_path / "agent"
    (source / "config").mkdir(parents=True)
    (source / "config/agent.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_id": "banking-agent",
                "llm_model": {"model": "test"},
            }
        )
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump({"redis": {}} | override))
    config = IntegrationConfig(
        agent_dir=source, runtime_config=path, runtime_python=Path(sys.executable)
    )
    with pytest.raises(ValueError, match="requires"):
        CaseRuntime(config, source, model="test", temperature=0)


def test_permission_race_requires_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """EPERM during shutdown is safe only once the actual child is reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    killpg = os.killpg

    def denied(pid: int, sig: int) -> None:
        if sig == 0:
            return killpg(pid, sig)
        # Make the exit during signaling deterministic.
        process.wait(timeout=2)
        raise PermissionError("injected process-exit race")

    monkeypatch.setattr(os, "killpg", denied)
    stop_process(process, 2)
    assert process.returncode == 0
