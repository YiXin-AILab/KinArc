"""Tau3 Banking 配置、子进程协议及 Candidate Server 生命周期管理。"""

import asyncio
import copy
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from threading import Event
from types import TracebackType
from typing import Literal, Self, cast

import httpx
import yaml
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue, StrictModel
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import AgentConfig, RuntimeConfig
from pydantic import Field, model_validator


class Tau3BankingConfig(StrictModel):
    """Tau3 Banking Task 的固定本地执行边界。"""

    tau2_project: Path
    runtime_config: Path
    user_llm: str = Field(min_length=1)
    judge_llm: str = Field(min_length=1)
    host: Literal["127.0.0.1"] = "127.0.0.1"
    agent_id: Literal["banking-agent", "banking-golden-agent"] = "banking-agent"
    retrieval_config: Literal["bm25_grep", "golden_retrieval"] = "bm25_grep"
    startup_timeout_seconds: float = Field(default=60, gt=0)
    case_timeout_seconds: float = Field(default=1200, gt=0)
    shutdown_timeout_seconds: float = Field(default=10, gt=0)
    validation_num_trials: int = Field(default=1, ge=1, strict=True)
    min_valid_ratio: float = Field(default=0.85, ge=0.0, le=1.0)
    max_reshoot_rounds: int = Field(default=2, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_local_dependencies(self) -> Self:
        """在启动前固定外部 Tau2 版本并确认 Runtime 配置。"""
        pyproject = self.tau2_project / "pyproject.toml"
        if not pyproject.is_file():
            raise ValueError(
                f"Tau2 Bench 1.0.1 不存在：{self.tau2_project}\n"
                "请执行：\n"
                "cd evolution_tasks/tau3_banking\n"
                "git clone --branch v1.0.1 --single-branch --depth 1 "
                "https://github.com/sierra-research/tau2-bench.git"
            )
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project")
        if not isinstance(project, dict) or project.get("version") != "1.0.1":
            raise ValueError("tau2_project 必须是 Tau2 1.0.1")
        if not self.runtime_config.is_file():
            raise ValueError("runtime_config 不存在")
        return self


class RunnerRequest(StrictModel):
    """当前进程发给 Tau2 Runner 的单 Case 请求。"""

    case: dict[str, JsonValue]
    retrieval_config: Literal["bm25_grep", "golden_retrieval"] = "bm25_grep"
    tool_bridge_socket: Path
    base_url: str
    app_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    user_llm: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class RunnerError(StrictModel):
    """Candidate Runtime 返回的完整结构化失败。"""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    exception_type: str | None = Field(default=None, min_length=1)
    stacktrace: str | None = Field(default=None, min_length=1)
    error_id: str | None = Field(default=None, min_length=1)


class RunnerResult(StrictModel):
    """Tau2 Runner 返回的未评分 Simulation 与 Candidate 证据。"""

    case_id: str = Field(min_length=1)
    simulation: dict[str, JsonValue]
    error: RunnerError | None = None
    candidate_events: tuple[dict[str, JsonValue], ...]
    total_tokens: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)


class EvaluationRequest(StrictModel):
    """官方 Tau2 重评输入。"""

    case: dict[str, JsonValue]
    retrieval_config: Literal["bm25_grep", "golden_retrieval"] = "bm25_grep"
    simulation: dict[str, JsonValue]
    judge_llm: str = Field(min_length=1)


class DBDifference(StrictModel):
    """官方 DB hash 输入的单个差异，path 使用 JSON Pointer。"""

    database: Literal["agent", "user"]
    path: str
    expected_present: bool = True
    actual_present: bool = True
    expected: JsonValue = None
    actual: JsonValue = None


class DBChecks(StrictModel):
    """官方 DB 评分及对应状态差异，不推断 agent 的思考原因。"""

    db_match: bool
    db_reward: float
    diagnosis_status: Literal["matched", "differences_found", "unavailable"]
    differences: list[DBDifference] = Field(default_factory=list)


class EvaluationResponse(StrictModel):
    """官方 Tau2 重评输出。"""

    case_id: str = Field(min_length=1)
    reward_info: dict[str, JsonValue]
    db_checks: DBChecks | None = None


def load_banking_config(
    task_dir: str | Path,
    settings: Mapping[str, JsonValue],
    *,
    entry_dir: Path,
) -> Tau3BankingConfig:
    """解析 Banking 专属配置并固定 Tau2 安装路径。

    Args:
        task_dir: 当前任务目录，作为 runtime_config 相对路径的基准。
        settings: 已完成环境变量插值的任务专属配置。
        entry_dir: Banking 入口实现目录，其下的 tau2-bench 存放 Tau2。

    Returns:
        已检查 Tau2 版本声明和 Runtime 文件存在性的配置。

    Raises:
        ValueError: 试图覆盖固定依赖路径，或配置、版本、文件检查失败。
    """
    task_root = Path(task_dir).resolve(strict=True)
    raw = dict(settings)
    if "tau2_project" in raw:
        raise ValueError("settings 不允许配置 tau2_project")
    runtime_config = raw.get("runtime_config")
    if isinstance(runtime_config, str):
        raw["runtime_config"] = str((task_root / runtime_config).resolve())
    raw["tau2_project"] = str((entry_dir / "tau2-bench").resolve())
    return Tau3BankingConfig.model_validate(raw)


_BASE_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
        "UV_CACHE_DIR",
        "VIRTUAL_ENV",
        "PYTHON_DOTENV_DISABLED",
    }
)
_CANDIDATE_ENVIRONMENT_NAMES = _BASE_ENVIRONMENT_NAMES | {
    "OTEL_AUTHORIZATION",
    "OTEL_ENDPOINT",
    "REDIS_DATABASE",
    "REDIS_HOST",
    "REDIS_PASSWORD",
    "REDIS_PORT",
    "REDIS_USERNAME",
    "TEST_AGENT_LLM_API_KEY",
    "TEST_AGENT_LLM_BASE_URL",
    "TEST_AGENT_LLM_MODEL",
}
_TAU2_ENVIRONMENT_NAMES = _BASE_ENVIRONMENT_NAMES | {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}


def build_runtime_mapping(
    source: Mapping[str, object],
    *,
    redis_prefix: str,
) -> dict[str, object]:
    """复制未插值 Runtime，仅覆盖用于 Case 隔离的 Redis 前缀。"""
    generated = copy.deepcopy(dict(source))
    redis = generated.get("redis")
    if not isinstance(redis, dict):
        raise ValueError("Runtime 必须配置 redis")
    redis["key_prefix"] = redis_prefix
    return generated


def validate_execution_environment(
    config: Tau3BankingConfig,
    candidate: Path,
    project_root: Path,
    *,
    cancel_event: Event | None = None,
) -> None:
    """在派发前验证公共配置、Redis 和已安装的 Tau2；不安装依赖。"""
    check_cancelled(cancel_event)
    python = config.tau2_project / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(
            "Tau2 环境未准备好；请先执行 uv sync --project "
            f"{config.tau2_project} --extra knowledge"
        )
    runtime = load_yaml_config(config.runtime_config, RuntimeConfig)
    if not runtime.public_stream.enabled:
        raise ValueError(
            "Banking 评测依赖 SSE，请在 Runtime 配置中开启 public_stream.enabled"
        )
    if (
        runtime.managers.message.type != "redis"
        or runtime.managers.state.type != "redis"
    ):
        raise ValueError("Banking Case 隔离当前仅支持 Redis Message/State Manager")
    agent = load_yaml_config(candidate / "config" / "agent.yaml", AgentConfig)
    if agent.agent_id != config.agent_id:
        raise ValueError("Candidate agent_id 与 Banking 配置不一致")
    asyncio.run(_check_redis(runtime))
    check_cancelled(cancel_event)
    # 实际构建当前检索环境，覆盖可选 knowledge 依赖及数据文件。仅进程内 DB/索引。
    probe = (
        "import importlib.metadata; "
        "assert importlib.metadata.version('tau2') == '1.0.1'; "
        "from tau2.domains.banking_knowledge.environment import get_environment; "
        f"get_environment(retrieval_variant={config.retrieval_config!r})"
    )
    with tempfile.TemporaryFile() as diagnostics:
        child = subprocess.Popen(
            [str(python), "-c", probe],
            cwd=config.tau2_project,
            env=_child_environment(os.environ, allowed_names=_TAU2_ENVIRONMENT_NAMES),
            stdout=diagnostics,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            _wait_process(child, config.startup_timeout_seconds, cancel_event)
        finally:
            _stop_process(child, config.shutdown_timeout_seconds)
        if child.returncode:
            raise ValueError(
                "Tau2 环境预检失败；请检查 1.0.1 数据文件并执行 "
                f"uv sync --project {config.tau2_project} --extra knowledge"
            )


async def _check_redis(runtime: RuntimeConfig) -> None:
    # Tau2 子进程只使用协议模型，不要求安装父进程的 Redis 依赖。
    """创建短期 Redis 客户端完成连通性预检，随后关闭连接。"""
    from agentsystem.redis.client import close_redis_client, create_redis_client

    client = await create_redis_client(runtime.redis)
    await close_redis_client(client)  # create 已完成 ping 和配置类型校验。


class CandidateServer:
    """拥有单个 Case Candidate 进程及其有限关闭流程。"""

    def __init__(
        self,
        *,
        config: Tau3BankingConfig,
        project_root: Path,
        applications_root: Path,
        redis_prefix: str,
        tool_bridge_socket: Path,
        environ: Mapping[str, str] = os.environ,
        cancel_event: Event | None = None,
    ) -> None:
        """绑定 Case 私有配置及资源路径，实际进程在 start 时创建。"""
        self._config = config
        self._project_root = project_root.resolve(strict=True)
        self._applications_root = applications_root.resolve(strict=True)
        self._redis_prefix = redis_prefix
        self._tool_bridge_socket = tool_bridge_socket
        self._environ = environ
        self._cancel_event = cancel_event
        self._temporary: Path | None = None
        self._preserve_diagnostics = False
        self._process: subprocess.Popen[bytes] | None = None
        self._diagnostics: object | None = None
        self._port: int | None = None

    @property
    def stopped(self) -> bool:
        """仅在进程所有权已经安全释放后允许清理 case 数据。"""
        return self._process is None

    @property
    def base_url(self) -> str:
        """返回已启动服务的 loopback URL。"""
        if self._port is None:
            raise RuntimeError("Candidate Server 尚未启动")
        return f"http://{self._config.host}:{self._port}"

    def start(self) -> "CandidateServer":
        """生成专属 Runtime、启动进程并等待 Agent Catalog。"""
        if self._process is not None:
            raise RuntimeError("Candidate Server 已启动")
        ports: set[int] = set()
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        while True:
            try:
                _check_startup_deadline(deadline, self._cancel_event)
                return self._start(ports, deadline)
            except BaseException as error:
                try:
                    retry = (
                        isinstance(error, AdapterError)
                        and error.code == "candidate_start_failed"
                        and self._port_conflict()
                    )
                finally:
                    self.close()
                if not retry:
                    raise

    def _start(self, ports: set[int], deadline: float) -> "CandidateServer":
        """生成 Case 私有配置并启动 Candidate，失败时保留诊断目录。"""
        temporary_parent = self._project_root / ".evolution"
        temporary_parent.mkdir(exist_ok=True)
        self._temporary = Path(
            tempfile.mkdtemp(
                prefix=".tau3-candidate-",
                dir=temporary_parent,
            )
        )
        self._preserve_diagnostics = False
        temporary_root = self._temporary
        runtime_path = temporary_root / "runtime.yaml"
        source = yaml.safe_load(self._config.runtime_config.read_text(encoding="utf-8"))
        if not isinstance(source, Mapping):
            raise ValueError("Runtime YAML 根节点必须是 mapping")
        generated = build_runtime_mapping(source, redis_prefix=self._redis_prefix)
        logging_config = generated.setdefault("logging", {})
        assert isinstance(logging_config, dict)
        relative_temporary = temporary_root.relative_to(self._project_root).as_posix()
        logging_config["directory"] = f"{relative_temporary}/logs"
        store = generated.setdefault("asset_store", {"type": "local"})
        if isinstance(store, dict) and store.get("type") == "local":
            store["root"] = f"{relative_temporary}/assets"
        runtime_path.write_text(
            yaml.safe_dump(generated, sort_keys=False),
            encoding="utf-8",
        )
        diagnostics_path = temporary_root / "candidate.log"
        self._diagnostics = diagnostics_path.open("wb")
        check_cancelled(self._cancel_event)
        self._port = _available_port(
            self._config.host, ports, deadline=deadline, cancel_event=self._cancel_event
        )
        ports.add(self._port)
        command = [
            sys.executable,
            "-P",
            "-m",
            "server.entrypoint",
            "--runtime-config",
            str(runtime_path),
            "--applications-root",
            str(self._applications_root),
            "--project-root",
            str(self._project_root),
            "--host",
            self._config.host,
            "--port",
            str(self._port),
        ]
        try:
            child_environment = _child_environment(
                self._environ,
                allowed_names=_CANDIDATE_ENVIRONMENT_NAMES,
                python_path_first=self._applications_root.parent,
            )
            child_environment["TAU2_TOOL_BRIDGE_SOCKET"] = str(self._tool_bridge_socket)
            if self._config.retrieval_config == "golden_retrieval":
                child_environment["TAU2_DOMAIN_POLICY_FILE"] = str(
                    self._tool_bridge_socket.with_suffix(".policy.md")
                )
            # -P 禁止 cwd 抢占私有 application；Runtime 源码放在副本之后。
            child_environment["PYTHONPATH"] = os.pathsep.join(
                (
                    str(self._applications_root.parent),
                    str(Path(__file__).parents[3]),
                )
            )
            _check_startup_deadline(deadline, self._cancel_event)
            self._process = subprocess.Popen(
                command,
                cwd=self._project_root,
                env=child_environment,
                stdout=self._diagnostics,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
            )
            self._wait_until_ready(deadline)
        except Exception as error:
            self._preserve_diagnostics = True
            if isinstance(error, AdapterError):
                raise AdapterError(
                    operation=error.operation,
                    code=error.code,
                    message=f"{error.message}；诊断目录：{temporary_root}",
                    retryable=error.retryable,
                ) from error
            raise AdapterError(
                operation="tau3.candidate.start",
                code="candidate_start_failed",
                message=f"Candidate Server 启动失败；诊断目录：{temporary_root}",
            ) from error
        return self

    def close(self) -> None:
        """按 terminate、有限等待、kill 的顺序收敛进程。"""
        process = self._process
        if process is not None:
            _stop_process(process, self._config.shutdown_timeout_seconds)
            self._process = None
        diagnostics = self._diagnostics
        self._diagnostics = None
        close = getattr(diagnostics, "close", None)
        if callable(close):
            close()
        if self._temporary is not None:
            if not self._preserve_diagnostics:
                shutil.rmtree(self._temporary)
            self._temporary = None
        self._port = None

    def __enter__(self) -> "CandidateServer":
        """启动 Candidate 服务，并把资源所有权交给上下文管理器。"""
        return self.start()

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文时关闭所持有的 Candidate 进程和临时资源。"""
        del exception_type, exception, traceback
        self.close()

    def _wait_until_ready(self, deadline: float) -> None:
        """确认当前进程已绑定端口且 Agent Catalog 可读，受启动期限约束。"""
        with httpx.Client(timeout=1) as client:
            while True:
                _check_startup_deadline(deadline, self._cancel_event)
                if self._process is None or self._process.poll() is not None:
                    raise AdapterError(
                        operation="tau3.candidate.start",
                        code="candidate_start_failed",
                        message="Candidate Server 启动失败",
                    )
                try:
                    # 先确认本进程已成功绑定，不能误认占用者的 HTTP 200。
                    if (
                        f"Uvicorn running on {self.base_url} "
                        not in self._startup_log()
                    ):
                        time.sleep(0.05)
                        continue
                    _check_startup_deadline(deadline, self._cancel_event)
                    response = client.get(
                        f"{self.base_url}/v1/agents",
                        timeout=min(1.0, max(0.001, deadline - time.monotonic())),
                    )
                    _check_startup_deadline(deadline, self._cancel_event)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)

    def _startup_log(self) -> str:
        """读取启动日志尾部，供就绪判断与端口冲突识别使用。"""
        assert self._temporary is not None
        with (self._temporary / "candidate.log").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65_536))
            return stream.read().decode(errors="replace")

    def _port_conflict(self) -> bool:
        """仅识别当前绑定地址对应的系统端口占用错误。"""
        if self._temporary is None or self._port is None:
            return False
        address = (
            f"error while attempting to bind on address "
            f"{(self._config.host, self._port)!r}:"
        )
        return any(
            address in line and ("[Errno 48]" in line or "[Errno 98]" in line)
            for line in self._startup_log().splitlines()
        )


def _check_startup_deadline(deadline: float, cancel_event: Event | None) -> None:
    """检查外部取消与启动期限，超时时抛出结构化启动错误。"""
    check_cancelled(cancel_event)
    if time.monotonic() >= deadline:
        raise AdapterError(
            operation="tau3.candidate.start",
            code="candidate_start_timeout",
            message="Candidate Server 总启动预算耗尽",
        )


def _available_port(
    host: str, excluded: set[int], *, deadline: float, cancel_event: Event | None
) -> int:
    # ponytail: 允许释放到启动间的竞争；明确的占用错误由启动边界有限重试。
    """选择尚未尝试的空闲端口；释放探测 socket 后仍可能发生竞争。"""
    while True:
        _check_startup_deadline(deadline, cancel_event)
        with socket.socket() as probe:
            probe.bind((host, 0))
            port = int(probe.getsockname()[1])
            if port not in excluded:
                return port


def run_tau2(
    config: Tau3BankingConfig,
    request: RunnerRequest,
    *,
    cancel_event: Event | None = None,
) -> RunnerResult:
    """在固定 Tau2 项目内运行一个 Case 并读取 JSON 结果。"""
    return _run_protocol(
        config,
        request,
        request_flag="--request",
        result_flag="--result",
        result_type=RunnerResult,
        cancel_event=cancel_event,
    )


def evaluate_tau2(
    config: Tau3BankingConfig,
    request: EvaluationRequest,
) -> EvaluationResponse:
    """在固定 Tau2 项目内调用官方 Evaluator。"""
    return _run_protocol(
        config,
        request,
        request_flag="--evaluation-request",
        result_flag="--evaluation-result",
        result_type=EvaluationResponse,
    )


def _run_protocol[ResultT: RunnerResult | EvaluationResponse](
    config: Tau3BankingConfig,
    request: RunnerRequest | EvaluationRequest,
    *,
    request_flag: str,
    result_flag: str,
    result_type: type[ResultT],
    cancel_event: Event | None = None,
) -> ResultT:
    """通过临时 JSON 文件执行一次 Tau2 子进程请求。

    子进程退出前始终尝试回收其进程组。成功后删除临时目录；失败时保留请求和日志。

    Args:
        config: 固定 Tau2 安装位置及执行、启停预算。
        request: 单次执行或评分的结构化请求。
        request_flag: Runner 命令行的输入文件参数名。
        result_flag: Runner 命令行的输出文件参数名。
        result_type: 用于校验输出 JSON 的响应模型。
        cancel_event: 可选的协作取消信号。

    Returns:
        经过响应模型校验的子进程结果。

    Raises:
        AdapterError: 子进程超时、运行失败、结果无效或进程组未能回收。
        asyncio.CancelledError: 收到取消信号。
    """
    runner = Path(__file__).with_name("runner.py")
    root = Path(tempfile.mkdtemp(prefix="tau3-runner-"))
    try:
        request_path = root / "request.json"
        result_path = root / "result.json"
        request_path.write_text(request.model_dump_json(), encoding="utf-8")
        command = [
            str(config.tau2_project / ".venv" / "bin" / "python"),
            str(runner),
            request_flag,
            str(request_path),
            result_flag,
            str(result_path),
        ]
        with (
            (root / "stdout.log").open("wb") as stdout,
            (root / "stderr.log").open("wb") as stderr,
        ):
            child_environment = _child_environment(
                os.environ,
                allowed_names=_TAU2_ENVIRONMENT_NAMES,
            )
            project_root = str(Path(__file__).parents[3])
            existing_pythonpath = child_environment.get("PYTHONPATH")
            child_environment["PYTHONPATH"] = (
                os.pathsep.join((project_root, existing_pythonpath))
                if existing_pythonpath
                else project_root
            )
            try:
                check_cancelled(cancel_event)
                completed = subprocess.Popen(
                    command,
                    cwd=config.tau2_project,
                    env=child_environment,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    shell=False,
                )
                try:
                    _wait_process(
                        completed,
                        config.case_timeout_seconds
                        + config.startup_timeout_seconds
                        + config.shutdown_timeout_seconds,
                        cancel_event,
                    )
                finally:
                    _stop_process(completed, config.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise AdapterError(
                    operation="tau3.runner",
                    code="tau3_runner_timeout",
                    message=f"Tau3 Runner 执行超时；诊断目录：{root}",
                ) from error
        if completed.returncode != 0 or not result_path.is_file():
            raise AdapterError(
                operation="tau3.runner",
                code="tau3_runner_failed",
                message=f"Tau3 Runner 执行失败；诊断目录：{root}",
            )
        result = cast(
            ResultT,
            result_type.model_validate_json(result_path.read_text(encoding="utf-8")),
        )
    except AdapterError:
        raise
    except Exception as error:
        raise AdapterError(
            operation="tau3.runner",
            code="tau3_runner_result_invalid",
            message=f"Tau3 Runner 结果无效；诊断目录：{root}",
        ) from error
    shutil.rmtree(root)
    return result


def check_cancelled(cancel_event: Event | None) -> None:
    """将协作停止信号保留为取消控制流。"""
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError


def _wait_process(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
    cancel_event: Event | None,
) -> None:
    """轮询子进程并响应取消；超过预算时抛出 TimeoutExpired。"""
    deadline = time.monotonic() + timeout_seconds
    while True:
        check_cancelled(cancel_event)
        if process.poll() is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        time.sleep(min(0.05, remaining))


def _group_exists(process: subprocess.Popen[bytes]) -> bool:
    """回收直属子进程后探测进程组，权限不明时保守视为仍存活。"""
    process.poll()  # Reap the direct child before probing its group.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS 进程退出中也可能短暂返回 EPERM；不能据此认定已经消失。
        return True
    return True


def _stop_process(process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    """仅回收由 start_new_session 创建的进程组；失败保留所有权。"""
    try:
        for action in (signal.SIGTERM, signal.SIGKILL):
            if not _group_exists(process):
                process.wait(timeout=timeout_seconds)
                return
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, action)
            deadline = time.monotonic() + timeout_seconds
            while _group_exists(process) and time.monotonic() < deadline:
                time.sleep(0.02)
        if _group_exists(process):
            raise TimeoutError("owned process group did not stop")
        process.wait(timeout=timeout_seconds)
    except Exception as error:
        raise AdapterError(
            operation="tau3.process.close",
            code="case_cleanup_failed",
            message="Case 进程组未能安全回收，已保留现场",
        ) from error


def _child_environment(
    environ: Mapping[str, str],
    *,
    allowed_names: frozenset[str],
    python_path_first: Path | None = None,
) -> dict[str, str]:
    """按白名单复制环境，并按需设置子进程的源码搜索路径。"""
    child = {
        str(key): str(value) for key, value in environ.items() if key in allowed_names
    }
    child["PYTHONUNBUFFERED"] = "1"
    if python_path_first is not None:
        existing_pythonpath = child.get("PYTHONPATH")
        child["PYTHONPATH"] = (
            os.pathsep.join((str(python_path_first), existing_pythonpath))
            if existing_pythonpath
            else str(python_path_first)
        )
    return child


__all__ = [
    "CandidateServer",
    "DBChecks",
    "DBDifference",
    "EvaluationRequest",
    "EvaluationResponse",
    "RunnerError",
    "RunnerRequest",
    "RunnerResult",
    "Tau3BankingConfig",
    "load_banking_config",
    "build_runtime_mapping",
    "evaluate_tau2",
    "run_tau2",
]
