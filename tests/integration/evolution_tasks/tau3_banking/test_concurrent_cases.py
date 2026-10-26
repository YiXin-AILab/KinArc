"""显式 opt-in：真实 Redis/HTTP/官方 DB，模型响应由本地服务确定。"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from agentsystem.common.errors import AdapterError
from agentsystem.redis.client import close_redis_client, create_redis_client
from evolution.models import DatasetRole, ExecutionResult
from evolution_tasks.tau3_banking._internal import process as banking_process
from evolution_tasks.tau3_banking._internal.process import (
    CandidateServer,
    RunnerRequest,
    RunnerResult,
    Tau3BankingConfig,
    _stop_process,
    _wait_process,
)
from evolution_tasks.tau3_banking.executor import BankingExecutor
from tests.redis_config import local_redis_config

ROOT = Path(__file__).parents[4]


@pytest.fixture
def case_root() -> Iterator[Path]:
    """macOS UDS 路径上限较短，测试项目使用独立短根目录。"""
    with tempfile.TemporaryDirectory(prefix="t3c-", dir="/tmp") as directory:
        yield Path(directory).resolve()


@pytest.mark.parametrize(
    "scenario", ["complete", "cancel", "timeout", "port_conflict", "retry"]
)
def test_concurrent_official_databases_and_runtime_cleanup(
    case_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """两个独立 Runtime/官方 DB 同时运行，停止后只清理自己的 namespace。"""
    tmp_path = case_root
    cancel = scenario == "cancel"
    dependency = os.environ.get("TEST_TAU2_PROJECT")
    if not dependency or not os.environ.get("TEST_REDIS_URL"):
        pytest.skip("需要显式 TEST_TAU2_PROJECT 与 TEST_REDIS_URL")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    both_models = threading.Barrier(2, timeout=30)
    entered = threading.Event()
    release = threading.Event()

    model = ThreadingHTTPServer(
        ("127.0.0.1", 0), _model_handler(both_models, entered, release, cancel)
    )
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    if scenario == "port_conflict":
        # 持续模拟探测后被占用，真实进程必须耗尽总预算后失败并回收。
        monkeypatch.setattr(
            banking_process,
            "_available_port",
            lambda host, excluded, **kwargs: model.server_port,
        )
    candidate = tmp_path / "candidate"
    # 模拟真实仓库根已有 application 包，不能抢占当前 case 的副本。
    (tmp_path / "application").mkdir()
    (tmp_path / "application/__init__.py").write_text(
        'raise RuntimeError("imported original application instead of candidate")\n'
    )
    (candidate / "config").mkdir(parents=True)
    (candidate / "plugins").mkdir()
    (candidate / "services").mkdir()
    for folder in (candidate, candidate / "plugins", candidate / "services"):
        (folder / "__init__.py").touch()
    shutil.copyfile(
        ROOT / "application/banking_agent/services/tau2_tool_client.py",
        candidate / "services/tau2_tool_client.py",
    )
    (candidate / "plugins/bank.py").write_text(_PLUGIN)
    (candidate / "prompt.yaml").write_text("prompt: Increment once.\n")
    (candidate / "config/agent.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_id": "banking-agent",
                "agent_name": "Bank",
                "prompt": "prompt.yaml",
                "outbound": {"type": "stream"},
                "enabled_tools": ["increment"],
                "enabled_middlewares": [],
                "llm_model": {
                    "model": "openai/test",
                    "api_key": "local-fixture",
                    "base_url": f"http://127.0.0.1:{model.server_port}/v1",
                    "timeout_seconds": 40,
                },
            }
        )
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "redis": local_redis_config().model_dump(mode="json"),
                "public_stream": {},
                "logging": {"file_enabled": False},
                "asset_store": {"type": "local"},
                "asset": {"max_file_bytes": 1048576},
            }
        )
    )
    config = Tau3BankingConfig(
        tau2_project=Path(dependency),
        runtime_config=runtime,
        user_llm="unused",
        judge_llm="unused",
        startup_timeout_seconds=10 if scenario == "port_conflict" else 45,
        case_timeout_seconds=60,
        shutdown_timeout_seconds=5,
    )
    dataset = tmp_path / "cases.json"
    dataset.write_text(
        json.dumps(
            [
                {"id": "first", "balance": 10, "timeout": scenario == "timeout"},
                {"id": "second", "balance": 100},
            ]
        )
    )
    program = tmp_path / "official_runner.py"
    program.write_text(_OFFICIAL_RUNNER)
    prefixes: list[str] = []
    sockets: list[Path] = []
    servers: list[CandidateServer] = []
    children: list[subprocess.Popen[bytes]] = []
    urls: list[str] = []
    applications: list[Path] = []
    attempts: dict[str, int] = {}
    owners: dict[Path, tuple[CandidateServer, Path]] = {}
    previous: dict[str, tuple[CandidateServer, Path]] = {}

    def server_factory(**kwargs: Any) -> CandidateServer:
        applications.append(kwargs["applications_root"])
        prefixes.append(kwargs["redis_prefix"])
        sockets.append(kwargs["tool_bridge_socket"])
        server = CandidateServer(**kwargs)
        owners[kwargs["tool_bridge_socket"]] = (server, kwargs["applications_root"])
        servers.append(server)
        return server

    def runner(
        config: Tau3BankingConfig,
        request: RunnerRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        urls.append(request.base_url)
        case_id = str(request.case["id"])
        attempts[case_id] = attempts.get(case_id, 0) + 1
        _assert_previous_attempt_stopped(previous.get(case_id))
        previous[case_id] = owners[request.tool_bridge_socket]
        request_path = tmp_path / f"{case_id}.json"
        result_path = tmp_path / f"{case_id}-result.json"
        request_path.write_text(request.model_dump_json())
        with (tmp_path / f"{case_id}.log").open("wb") as log:
            child = subprocess.Popen(
                [
                    str(config.tau2_project / ".venv/bin/python"),
                    str(program),
                    str(request_path),
                    str(result_path),
                ],
                cwd=tmp_path,
                env={
                    **os.environ,
                    "PYTHONPATH": str(ROOT),
                    "PYTHON_DOTENV_DISABLED": "1",
                },
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append(child)
            try:
                _wait_process(
                    child,
                    20 if request.case.get("timeout") else 60,
                    cancel_event,
                )
            except subprocess.TimeoutExpired as error:
                raise AdapterError(
                    operation="tau3.runner",
                    code="tau3_runner_timeout",
                    message="test case timed out",
                ) from error
            finally:
                _stop_process(child, 5)
        assert child.returncode == 0, (tmp_path / f"{case_id}.log").read_text()[-2000:]
        result = RunnerResult.model_validate_json(result_path.read_text())
        return _simulate_retry(result, scenario, case_id, attempts[case_id])

    executor = BankingExecutor(
        config=config,
        project_root=tmp_path,
        server_factory=server_factory,
        runner=runner,
    )
    stop = threading.Event()
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(
                executor.execute,
                candidate,
                dataset,
                DatasetRole.TRAIN,
                max_case_concurrency=2,
                max_retries=int(scenario == "retry"),
                retry_delay_seconds=0,
                cancel_event=stop,
            )
            try:
                if scenario == "port_conflict":
                    _assert_startup_timeouts(future.result(30))
                else:
                    _await_models(entered, future)
                if cancel:
                    stop.set()
                    with pytest.raises(asyncio.CancelledError):
                        future.result(20)
                elif scenario != "port_conflict":
                    _assert_case_results(future.result(30), scenario)
            finally:
                stop.set()
                release.set()
        expected = 2 + int(scenario == "retry")
        assert (
            len(set(prefixes))
            == len(set(sockets))
            == len(set(applications))
            == expected
        )
        # 操作系统允许已关闭 Server 的端口被后续尝试复用。
        assert len(urls) == (0 if scenario == "port_conflict" else expected)
        assert candidate.stat().st_mode & 0o200
        assert all(server.stopped for server in servers)
        assert all(child.poll() is not None for child in children)
        assert all(not path.exists() for path in sockets)
        assert not list((tmp_path / ".evolution").glob(".tau3-banking-case-*"))
        assert not list(tmp_path.glob(".tau3-banking-case-*"))
        asyncio.run(_assert_namespaces_empty(prefixes))
    finally:
        release.set()
        model.shutdown()
        model.server_close()
        model_thread.join(timeout=3)


def _assert_previous_attempt_stopped(
    owner: tuple[CandidateServer, Path] | None,
) -> None:
    if owner is not None:
        server, application = owner
        assert server.stopped
        assert not application.exists()


def _simulate_retry(
    result: RunnerResult, scenario: str, case_id: str, attempt: int
) -> RunnerResult:
    if scenario == "retry" and case_id == "first" and attempt == 1:
        return result.model_copy(
            update={"simulation": {"termination_reason": "agent_error"}}
        )
    return result


def _assert_startup_timeouts(result: ExecutionResult) -> None:
    assert [case.result for case in result.cases] == ["agent_error", "agent_error"]
    for case in result.cases:
        assert isinstance(case.tracelog, dict)
        error = case.tracelog["error"]
        assert isinstance(error, dict)
        assert error["code"] == "candidate_start_timeout"


def _assert_case_results(result: ExecutionResult, scenario: str) -> None:
    if scenario == "retry":
        assert len(result.cases[0].attempts) == 2
        assert result.cases[0].attempts[0].result == "agent_error"
        assert result.cases[1].attempts == ()
    assert [case.result for case in result.cases] == [
        "timeout" if scenario == "timeout" else "completed",
        "completed",
    ]
    events = []
    for case in result.cases:
        assert isinstance(case.tracelog, dict)
        events.append(case.tracelog["candidate_events"])
    assert events == [
        [] if scenario == "timeout" else [{"balance": 11}],
        [{"balance": 101}],
    ]


def _await_models(entered: threading.Event, future: Future[ExecutionResult]) -> None:
    for _ in range(1400):
        if entered.wait(0.05):
            return
        if future.done():
            early = future.result()
            pytest.fail(f"Case 提前结束：{early.model_dump_json()}")
    pytest.fail("两个 case 未同时进入模型请求")


def _model_handler(
    both_models: threading.Barrier,
    entered: threading.Event,
    release: threading.Event,
    cancel: bool,
) -> type[BaseHTTPRequestHandler]:
    requests = 0
    lock = threading.Lock()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            nonlocal requests
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            has_result = any(m["role"] == "tool" for m in request["messages"])
            if not has_result:
                with lock:
                    requests += 1
                    initial_pair = requests <= 2
                if initial_pair:
                    both_models.wait()
                entered.set()
                if cancel:
                    release.wait(30)
                delta: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "increment-1",
                            "type": "function",
                            "function": {"name": "increment", "arguments": "{}"},
                        }
                    ],
                }
            else:
                delta = {"role": "assistant", "content": "Done"}
            chunks = [
                {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop" if has_result else "tool_calls",
                        }
                    ],
                },
            ]
            body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
            body += "data: [DONE]\n\n"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # Cancellation deliberately closes the model connection.

    return Model


async def _assert_namespaces_empty(prefixes: list[str]) -> None:
    client = await create_redis_client(local_redis_config())
    try:
        for prefix in prefixes:
            assert not [key async for key in client.scan_iter(match=f"{prefix}:*")]
    finally:
        await close_redis_client(client)


_PLUGIN = '''
import os
from pathlib import Path
from agentsystem.tool.models import ToolOutput
from application.banking_agent.services.tau2_tool_client import Tau2ToolClient

async def increment() -> ToolOutput:
    """Increment the official database balance."""
    return await Tau2ToolClient(Path(os.environ["TAU2_TOOL_BRIDGE_SOCKET"])).execute(
        "increment", {})

def register(context):
    context.register_tool(increment, name="increment", description="Increment balance")
'''

_OFFICIAL_RUNNER = '''
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.data_model.message import UserMessage
from tau2.orchestrator.orchestrator import Orchestrator
from evolution_tasks.tau3_banking._internal.process import RunnerRequest, RunnerResult
from evolution_tasks.tau3_banking._internal.adapter import (
    AgentsystemHttpSession, create_http_agent,
)
from evolution_tasks.tau3_banking._internal.tool_bridge import (
    CaseToolBridge, ToolBridgeServer,
)

request = RunnerRequest.model_validate_json(Path(sys.argv[1]).read_text())
class Bank(DB):
    balance: int = 0
class Tools(ToolKitBase):
    @is_tool(ToolType.WRITE)
    def increment(self) -> str:
        """Increment this case balance."""
        self.db.balance += 1
        return str(self.db.balance)
class User:
    def get_init_state(self, message_history=None): return 0
    def generate_next_message(self, message, state):
        return UserMessage(role="user", content="Increment" if state == 0
                           else "###STOP###"), state + 1
    def stop(self, *args): pass
db = Bank(balance=request.case["balance"])
env = Environment(domain_name="test", policy="test", tools=Tools(db))
bridge = CaseToolBridge()
session = AgentsystemHttpSession(base_url=request.base_url, app_id=request.app_id,
    agent_id=request.agent_id, user_id=request.user_id,
    session_id="same-session-for-isolation", timeout_seconds=50)
agent = create_http_agent(tools=env.get_tools(), domain_policy="test",
                          session=session, bridge=bridge)
orchestrator = Orchestrator(domain="test", agent=agent, user=User(), environment=env,
    task=SimpleNamespace(id=request.case["id"], initial_state=None),
    max_steps=20, timeout=50, validate_communication=True)
with ToolBridgeServer(bridge, request.tool_bridge_socket):
    try:
        simulation = orchestrator.run()
    finally:
        bridge.close()
        session.close()
result = RunnerResult(case_id=request.case["id"],
    simulation=simulation.model_dump(mode="json"),
    candidate_events=({"balance": db.balance},), total_tokens=0,
    duration_seconds=float(simulation.duration))
if request.case.get("timeout"):
    time.sleep(60)
Path(sys.argv[2]).write_text(result.model_dump_json())
'''
