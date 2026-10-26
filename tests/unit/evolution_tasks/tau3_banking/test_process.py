"""Tau3 Banking 子进程边界测试。"""

import json
import sys
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from agentsystem.common.errors import AdapterError
from evolution_tasks.tau3_banking._internal.process import (
    CandidateServer,
    RunnerRequest,
    RunnerResult,
    Tau3BankingConfig,
    build_runtime_mapping,
    run_tau2,
)


@pytest.fixture(autouse=True)
def simulated_process_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """传输测试使用假进程；真实进程组行为由 lifecycle 测试覆盖。"""

    def stop(child: _FakeProcess, timeout: float) -> None:
        child.terminate()
        child.wait(timeout=timeout)

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process._stop_process", stop
    )


def test_build_runtime_mapping_preserves_config_except_redis_prefix() -> None:
    """Case 隔离只覆盖 Redis 前缀，保留其他配置且不修改输入。"""
    source: dict[str, dict[str, object]] = {
        "redis": {"host": "${REDIS_HOST}", "key_prefix": "original"},
        "public_stream": {
            "enabled": True,
            "include_tool_arguments": False,
            "include_tool_results": False,
            "max_event_bytes": 4096,
        },
        "trace": {"enabled": False},
    }

    generated = build_runtime_mapping(source, redis_prefix="case-owned")
    assert source["redis"]["key_prefix"] == "original"
    assert generated == {
        **source,
        "redis": {**source["redis"], "key_prefix": "case-owned"},
    }


def test_run_tau2_uses_argument_list_and_json_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner 协议不得解析 stdout，也不得使用 shell。"""
    config = _config(tmp_path)
    request = RunnerRequest(
        case={"id": "task_001"},
        tool_bridge_socket=tmp_path / "tools.sock",
        base_url="http://127.0.0.1:32123",
        app_id="tau3",
        agent_id="banking-agent",
        user_id="user-1",
        user_llm="openai/user-model",
        timeout_seconds=30,
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured.update(kwargs)
        request_path = Path(command[command.index("--request") + 1])
        captured["request"] = json.loads(request_path.read_text(encoding="utf-8"))
        result_path = Path(command[command.index("--result") + 1])
        result_path.write_text(
            RunnerResult(
                case_id="task_001",
                simulation={"id": "simulation-1"},
                candidate_events=(),
                total_tokens=3,
                duration_seconds=0.5,
            ).model_dump_json(),
            encoding="utf-8",
        )
        return _CompletedProcess(0)

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.subprocess.Popen", fake_run
    )
    monkeypatch.setenv("OPENAI_API_KEY", "required-test-key")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    result = run_tau2(config, request)

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == str(config.tau2_project / ".venv" / "bin" / "python")
    assert captured["start_new_session"] is True
    assert captured["shell"] is False
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["OPENAI_API_KEY"] == "required-test-key"
    assert environment["PYTHONPATH"].split(":", 1)[0] == str(Path(__file__).parents[4])
    assert "UNRELATED_SECRET" not in environment
    assert result.case_id == "task_001"
    request_json = captured["request"]
    assert isinstance(request_json, dict)
    assert request_json["case"]["id"] == "task_001"


def test_run_tau2_preserves_bounded_diagnostics_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子进程失败后必须留下可定位的 stdout/stderr 文件。"""
    config = _config(tmp_path)
    diagnostics = tmp_path / "failed-run"

    def fake_mkdtemp(
        suffix: str | None = None,
        prefix: str | None = None,
        directory: str | None = None,
    ) -> str:
        del suffix, directory
        assert prefix == "tau3-runner-"
        diagnostics.mkdir()
        return str(diagnostics)

    def fake_run(command: list[str], **kwargs: object) -> object:
        del command
        stdout = cast(BinaryIO, kwargs["stdout"])
        stderr = cast(BinaryIO, kwargs["stderr"])
        stdout.write(b"runner output")
        stderr.write(b"runner error")
        return _CompletedProcess(1)

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.tempfile.mkdtemp", fake_mkdtemp
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.subprocess.Popen", fake_run
    )
    request = RunnerRequest(
        case={"id": "task_001"},
        tool_bridge_socket=tmp_path / "tools.sock",
        base_url="http://127.0.0.1:32123",
        app_id="tau3",
        agent_id="banking-agent",
        user_id="user-1",
        user_llm="openai/user-model",
        timeout_seconds=30,
    )

    with pytest.raises(AdapterError, match=str(diagnostics)):
        run_tau2(config, request)

    assert (diagnostics / "stdout.log").read_bytes() == b"runner output"
    assert (diagnostics / "stderr.log").read_bytes() == b"runner error"


@pytest.mark.parametrize("golden", [False, True])
def test_candidate_server_owns_runtime_process_and_finite_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden: bool,
) -> None:
    """单 Case Server 必须使用专属配置并在上下文退出时关闭。"""
    config = _config(tmp_path)
    if golden:
        config = config.model_copy(update={"retrieval_config": "golden_retrieval"})
    config.runtime_config.write_text(
        """redis:
  host: ${REDIS_HOST}
  key_prefix: original
public_stream:
  enabled: true
  include_tool_arguments: false
  include_tool_results: false
""",
        encoding="utf-8",
    )
    applications = tmp_path / "application"
    applications.mkdir()
    (applications / "__init__.py").touch()
    process = _FakeProcess()
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        stdout = cast(BinaryIO, kwargs["stdout"])
        port = command[command.index("--port") + 1]
        stdout.write(
            f"Uvicorn running on http://127.0.0.1:{port} (Press CTRL+C)\n".encode()
        )
        stdout.flush()
        return process

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.httpx.Client", _ReadyClient
    )

    with CandidateServer(
        config=config,
        project_root=tmp_path,
        applications_root=applications,
        redis_prefix="tau3:owned-case",
        tool_bridge_socket=tmp_path / "tools.sock",
        environ={
            "REDIS_HOST": "127.0.0.1",
            "TEST_AGENT_LLM_MODEL": "openai/test-model",
            "UNRELATED_SECRET": "must-not-leak",
        },
    ) as server:
        assert server.base_url.startswith("http://127.0.0.1:")
        child_env = captured["env"]
        assert isinstance(child_env, dict)
        assert child_env.get("TAU2_DOMAIN_POLICY_FILE") == (
            str(tmp_path / "tools.policy.md") if golden else None
        )
        command = captured["command"]
        assert isinstance(command, list)
        assert command[:4] == [
            sys.executable,
            "-P",
            "-m",
            "server.entrypoint",
        ]
        assert "pass_fds" not in captured
        assert captured["start_new_session"] is True
        runtime_path = Path(command[command.index("--runtime-config") + 1])
        assert runtime_path.is_relative_to(tmp_path)
        generated = runtime_path.read_text(encoding="utf-8")
        assert "key_prefix: tau3:owned-case" in generated
        assert "${REDIS_HOST}" in generated
        assert runtime_path.parent.parent == tmp_path / ".evolution"
        assert f"directory: .evolution/{runtime_path.parent.name}/logs" in generated
        assert f"root: .evolution/{runtime_path.parent.name}/assets" in generated
        assert command[command.index("--applications-root") + 1] == str(applications)
        environment = captured["env"]
        assert isinstance(environment, dict)
        assert environment["PYTHONPATH"].split(":", 1)[0] == str(tmp_path)
        assert environment["REDIS_HOST"] == "127.0.0.1"
        assert environment["TEST_AGENT_LLM_MODEL"] == "openai/test-model"
        assert environment["TAU2_TOOL_BRIDGE_SOCKET"] == str(tmp_path / "tools.sock")
        assert "UNRELATED_SECRET" not in environment

    assert process.terminated is True
    assert process.wait_timeout == config.shutdown_timeout_seconds


def test_candidate_server_preserves_startup_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate 启动失败时日志目录必须保留并通过安全路径定位。"""
    config = _config(tmp_path)
    config.runtime_config.write_text(
        "redis: {}\npublic_stream: {}\n",
        encoding="utf-8",
    )
    applications = tmp_path / "application"
    applications.mkdir()
    diagnostics = tmp_path / ".evolution" / "failed-candidate"

    def fake_mkdtemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | Path | None = None,
    ) -> str:
        del suffix
        assert prefix == ".tau3-candidate-"
        assert dir is not None
        assert Path(dir) == tmp_path / ".evolution"
        diagnostics.mkdir()
        return str(diagnostics)

    def fake_popen(command: list[str], **kwargs: object) -> "_ExitedProcess":
        del command
        stdout = cast(BinaryIO, kwargs["stdout"])
        stdout.write(b"candidate failed")
        stdout.flush()
        return _ExitedProcess()

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.tempfile.mkdtemp", fake_mkdtemp
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.subprocess.Popen", fake_popen
    )

    with pytest.raises(AdapterError, match=str(diagnostics)):
        CandidateServer(
            config=config,
            project_root=tmp_path,
            applications_root=applications,
            redis_prefix="tau3:owned-case",
            tool_bridge_socket=tmp_path / "tools.sock",
            environ={},
        ).start()

    assert (diagnostics / "candidate.log").read_bytes() == b"candidate failed"


@pytest.mark.parametrize(
    "errno,failures,expected",
    [(48, 1, 2), (98, 15, 16), (98, 100, 20), (13, 1, 1)],
)
def test_candidate_retries_only_bind_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    errno: int,
    failures: int,
    expected: int,
) -> None:
    """端口冲突不限次数但共用 60 秒预算，权限错误立即失败。"""
    config = _config(tmp_path).model_copy(update={"startup_timeout_seconds": 60})
    now = 0.0
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.time.monotonic", lambda: now
    )
    config.runtime_config.write_text("redis: {}\npublic_stream: {}\n")
    children: list[_FakeProcess] = []
    ports: list[str] = []

    def popen(command: list[str], **kwargs: object) -> _FakeProcess:
        nonlocal now
        now += 3.0
        assert not children or children[-1].terminated
        port = command[command.index("--port") + 1]
        ports.append(port)
        failed = len(children) < failures
        child = _ExitedProcess() if failed else _FakeProcess()
        children.append(child)
        output = (
            f"[Errno {errno}] error while attempting to bind on address "
            f"('127.0.0.1', {port}): failure\n"
            if failed
            else f"Uvicorn running on http://127.0.0.1:{port} (Press CTRL+C to quit)\n"
        )
        stdout = cast(BinaryIO, kwargs["stdout"])
        stdout.write(output.encode())
        stdout.flush()
        return child

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.subprocess.Popen", popen
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.httpx.Client", _ReadyClient
    )
    server = CandidateServer(
        config=config,
        project_root=tmp_path,
        applications_root=tmp_path,
        redis_prefix="tau3:retry",
        tool_bridge_socket=tmp_path / "bridge.sock",
        environ={},
    )
    if failures < expected:
        with server:
            assert server.base_url.endswith(ports[-1])
    else:
        with pytest.raises(AdapterError) as failure:
            server.start()
        assert failure.value.code == (
            "candidate_start_timeout" if errno == 98 else "candidate_start_failed"
        )
    assert len(children) == expected
    assert len(set(ports)) == expected
    assert all(child.terminated for child in children)


def test_candidate_does_not_accept_other_servers_http_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本进程没有绑定成功日志时，即便端口返回 200 也不能判为就绪。"""
    config = _config(tmp_path).model_copy(update={"startup_timeout_seconds": 0.05})
    config.runtime_config.write_text("redis: {}\npublic_stream: {}\n")
    children: list[_FakeProcess] = []

    def popen(*args: object, **kwargs: object) -> _FakeProcess:
        child = _FakeProcess()
        children.append(child)
        return child

    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.subprocess.Popen", popen
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.process.httpx.Client", _ReadyClient
    )
    server = CandidateServer(
        config=config,
        project_root=tmp_path,
        applications_root=tmp_path,
        redis_prefix="tau3:readiness",
        tool_bridge_socket=tmp_path / "bridge.sock",
        environ={},
    )
    with pytest.raises(AdapterError) as failure:
        server.start()
    assert failure.value.code == "candidate_start_timeout"
    assert len(children) == 1
    assert children[0].terminated


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.wait_timeout: float | None = None

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
        self.wait_timeout = timeout
        return 0

    def kill(self) -> None:
        raise AssertionError("正常关闭不应 kill")


class _ExitedProcess(_FakeProcess):
    def poll(self) -> int:
        return 1


class _CompletedProcess(_FakeProcess):
    def __init__(self, returncode: int) -> None:
        super().__init__()
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode


class _ReadyClient:
    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> "_ReadyClient":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def get(self, url: str, *, timeout: float = 1) -> object:
        assert url.endswith("/v1/agents")
        return type("Response", (), {"status_code": 200})()


def _config(tmp_path: Path) -> Tau3BankingConfig:
    tau2 = tmp_path / "tau2"
    tau2.mkdir()
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
        user_llm="openai/user-model",
        host="127.0.0.1",
        agent_id="banking-agent",
        startup_timeout_seconds=2,
        case_timeout_seconds=10,
        shutdown_timeout_seconds=2,
    )
