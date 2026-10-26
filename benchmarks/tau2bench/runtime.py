"""每次尝试独占应用副本和 Runtime 进程。"""

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import yaml

from benchmarks.tau2bench.config import PROJECT_ROOT, IntegrationConfig
from benchmarks.tau2bench.tool_bridge import CaseToolBridge


def stop_process(
    process: subprocess.Popen[bytes] | subprocess.Popen[str], timeout: float
) -> None:
    """在有限时间内终止所持有的 POSIX 进程组，必要时强杀。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        process.poll()
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            # 原进程组不可访问时，仍须回收当前子进程。
            process.send_signal(sig)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None and not _group_alive(process.pid):
                return
            time.sleep(0.05)
    raise RuntimeError("native tau2 Runtime process group did not stop")


def make_tree_writable(root: Path) -> None:
    """为只读来源复制出的目录树添加所有者写权限。"""
    for path in (root, *root.rglob("*")):
        path.chmod(path.stat().st_mode | 0o200)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 仅检查仍存在的进程组，以区分僵尸进程和存活的后代进程。
    # ps 缺失或失败表示状态未知，不能据此认定清理成功。
    try:
        result = subprocess.run(
            ["ps", "-axo", "pgid=,stat="],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    states = [
        fields[1]
        for line in result.stdout.splitlines()
        if len(fields := line.split()) == 2 and fields[0] == str(pgid)
    ]
    return not states or any(not state.startswith("Z") for state in states)


def worker_command(
    config: IntegrationConfig, mode: str, root: Path, environ: dict[str, str]
) -> None:
    """使用主项目的 Python 环境执行预检或 Redis 清理。"""
    result = subprocess.run(
        [
            str(config.runtime_python),
            "-P",
            "-m",
            "benchmarks.tau2bench.runtime_worker",
            mode,
            str(root),
        ],
        env=environ,
        cwd=root,
        capture_output=True,
        timeout=config.startup_timeout_seconds
        if mode == "check"
        else config.shutdown_timeout_seconds,
    )
    if result.returncode:
        raise RuntimeError(
            f"native tau2 Runtime {mode} failed (configuration or Redis)"
        )


class CaseRuntime:
    """仅创建和释放当前 simulation 尝试持有的资源。"""

    def __init__(
        self,
        config: IntegrationConfig,
        source: Path,
        *,
        model: str,
        temperature: float,
        cancel: Event | None = None,
        log_path: Path | None = None,
        domain_policy: str | None = None,
    ) -> None:
        """准备私有文件，不启动服务，也不访问 Redis。"""
        self.config = config
        self.cancel = cancel or Event()
        token = uuid4().hex
        self.prefix = f"tau2native:{token}"
        self.session_id = f"tau2-{token}"
        self.root = Path(tempfile.mkdtemp(prefix="t2runtime-", dir="/tmp"))
        self.log_path = log_path or self.root / "startup.log"
        self.bridge = CaseToolBridge(timeout=config.http_timeout_seconds)
        self.base_url = ""
        self.process: subprocess.Popen[bytes] | None = None
        self._started = False
        self._closed = False
        self.environ = dict(os.environ)
        self.environ.pop("TAU2_DOMAIN_POLICY_FILE", None)
        self.environ.update(
            PYTHONPATH=os.pathsep.join((str(self.root), str(PROJECT_ROOT))),
            PYTHON_DOTENV_DISABLED="1",
            TEST_AGENT_LLM_MODEL=model,
            TAU2_TOOL_BRIDGE_SOCKET=str(self.bridge.socket_path),
        )
        try:
            self._prepare(source, model, temperature)
            if domain_policy is not None:
                policy_path = self.root / "domain-policy.md"
                policy_path.touch(mode=0o600)
                policy_path.write_text(domain_policy, encoding="utf-8")
                self.environ["TAU2_DOMAIN_POLICY_FILE"] = str(policy_path)
        except BaseException:
            self.bridge.close()
            shutil.rmtree(self.root)
            raise

    def _prepare(self, source: Path, model: str, temperature: float) -> None:
        applications = self.root / "application"
        applications.mkdir()
        (applications / "__init__.py").touch()
        target = applications / source.name
        shutil.copytree(
            source, target, ignore=shutil.ignore_patterns("__pycache__", ".env", ".git")
        )
        make_tree_writable(target)
        agent_path = target / "config/agent.yaml"
        agent = yaml.safe_load(agent_path.read_text())
        agent_id = agent.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("target must provide agent_id")
        self.agent_id = agent_id
        agent["llm_model"].update(model=model, temperature=temperature)
        agent.update(output_mode="stream", outbound={"type": "stream"})
        agent_path.write_text(yaml.safe_dump(agent, sort_keys=False))
        runtime = yaml.safe_load(self.config.runtime_config.read_text())
        if runtime.get("asset_store", {}).get("type", "local") != "local":
            raise ValueError("native benchmark requires local assets")
        managers = runtime.get("managers", {})
        if any(
            managers.get(name, {}).get("type", "redis") != "redis"
            for name in ("message", "state")
        ):
            raise ValueError("native benchmark requires Redis Message/State managers")
        runtime["redis"]["key_prefix"] = self.prefix
        runtime["logging"] = {
            "level": "INFO",
            "console_enabled": True,
            "file_enabled": False,
        }
        runtime["asset_store"] = {"type": "local", "root": "assets"}
        runtime["scheduler"] = {"enabled": False}
        path = self.root / "runtime.yaml"
        path.write_text(yaml.safe_dump(runtime, sort_keys=False))
        path.chmod(0o600)

    def start(self) -> None:
        """启动 case 专属的 HTTP Runtime，仅对已确认的端口冲突重试。"""
        if self._closed or self._started:
            raise ValueError("Runtime cannot start twice")
        self.bridge.start()
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise RuntimeError("native attempt stopped")
            port = _available_port()
            self.base_url = f"http://127.0.0.1:{port}"
            log_path = self.log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as output:
                log_path.chmod(0o600)
                output.write(f"Runtime session_id={self.session_id}\n".encode())
                output.flush()
                self.process = subprocess.Popen(
                    [
                        str(self.config.runtime_python),
                        "-P",
                        "-m",
                        "server.entrypoint",
                        "--project-root",
                        str(self.root),
                        "--applications-root",
                        "application",
                        "--runtime-config",
                        "runtime.yaml",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=self.root,
                    env=self.environ,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self._started = True
            if self._wait_ready(log_path, deadline):
                return
            self._stop()
        raise TimeoutError("native tau2 Runtime startup timed out")

    def _wait_ready(self, log_path: Path, deadline: float) -> bool:
        assert self.process is not None
        with httpx.Client(timeout=1) as client:
            while time.monotonic() < deadline:
                if self.cancel.is_set():
                    raise RuntimeError("native attempt stopped")
                log = log_path.read_bytes()[-65_536:].lower()
                if self.process.poll() is not None:
                    if b"address already in use" in log:
                        return False
                    raise RuntimeError("native tau2 Runtime startup failed")
                # 当前服务成功绑定端口前，不得把其他服务的响应视为启动成功。
                if b"uvicorn running on" in log:
                    try:
                        response = client.get(f"{self.base_url}/v1/agents")
                        if response.status_code == 200 and any(
                            item.get("id") == self.agent_id
                            for item in response.json().get("agents", [])
                        ):
                            return True
                    except httpx.HTTPError:
                        pass
                time.sleep(0.05)
        raise TimeoutError("native tau2 Runtime startup timed out")

    def _stop(self) -> None:
        if self.process is not None:
            stop_process(self.process, self.config.shutdown_timeout_seconds)
            self.process = None

    def close(self) -> None:
        """删除私有 namespace 前先停止进程；失败后可安全重试。"""
        if self._closed:
            return
        try:
            self.bridge.close()
        finally:
            self._stop()
        if self._started:
            worker_command(self.config, "clean", self.root, self.environ)
        shutil.rmtree(self.root)
        self._closed = True


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
