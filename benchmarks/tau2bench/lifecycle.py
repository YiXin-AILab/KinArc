"""原生 Runner 钩子：冻结评测目标并管理各次尝试的生命周期。"""

import logging
import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from threading import Event, Lock
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from agentsystem.common.errors import AdapterError
from agentsystem.common.logging import log_unexpected_exception

from benchmarks.tau2bench.config import (
    AGENT_NAME,
    TAU2_PROJECT,
    UPSTREAM_TAG,
    IntegrationConfig,
    load_config,
    validate_run,
)
from benchmarks.tau2bench.runtime import CaseRuntime, make_tree_writable, worker_command

LOGGER = logging.getLogger(__name__)


@dataclass
class BatchContext:
    """由相互独立的各次尝试共享的批次配置。"""

    integration: IntegrationConfig
    source: Path
    model: str
    temperature: float
    timeout: float


@dataclass
class AttemptContext:
    """单次官方 simulation 的私有资源所有权边界。"""

    simulation_id: str
    task_id: str
    seed: int | None
    batch: BatchContext
    cancel: Event
    log_path: Path | None = None
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def own(self, close: Callable[[], None]) -> None:
        """获取资源后立即登记，确保后续失败仍能回收。"""
        if self.cancel.is_set():
            close()
            raise RuntimeError("native attempt stopped")
        self._closers.append(close)

    def close(self) -> None:
        """尝试关闭所有资源，并暴露 Tau2.stop 之外的清理失败。"""
        errors: list[Exception] = []
        for close in reversed(self._closers):
            try:
                close()
            except Exception as error:
                errors.append(error)
        self._closers.clear()
        if errors:
            self.cancel.set()
            raise ExceptionGroup("native tau2 attempt cleanup failed", errors)


_attempt: ContextVar[AttemptContext] = ContextVar("tau2_native_attempt")
_batches: dict[int, BatchContext] = {}
_batch_lock = Lock()


def current_attempt() -> AttemptContext:
    """只读取当前工作线程显式绑定的尝试上下文。"""
    try:
        return _attempt.get()
    except LookupError:
        raise RuntimeError(
            "agentsystem_banking requires the native Runner patch"
        ) from None


@contextmanager
def bound_attempt(context: AttemptContext) -> Iterator[AttemptContext]:
    """用同一清理边界覆盖构建、初始化、运行和评分。"""
    token = _attempt.set(context)
    try:
        yield context
    except Exception as error:
        if not isinstance(error, (ValueError, TimeoutError, AdapterError)):
            log_unexpected_exception(
                LOGGER,
                "native_tau2_attempt_failed",
                error,
                operation="tau2.native.attempt",
            )
        raise
    finally:
        try:
            context.close()
        except Exception as error:
            log_unexpected_exception(
                LOGGER,
                "native_tau2_cleanup_failed",
                error,
                operation="tau2.native.cleanup",
            )
            raise
        finally:
            _attempt.reset(token)


@contextmanager
def attempt_scope(
    config: Any, task: Any, simulation_id: str, seed: int | None, save_dir: Path | None
) -> Iterator[None]:
    """将一次原生 Tau2 尝试绑定到私有清理作用域。

    其他 Agent 类型直接通过，不创建尝试上下文。作用域退出时，
    已绑定的上下文会关闭其登记的资源。

    Args:
        config: 原生 Tau2 运行配置。
        task: 含任务 ID 的原生任务对象。
        simulation_id: 本次尝试的官方 simulation ID。
        seed: 可选的 trial 随机种子。
        save_dir: 可选的尝试日志目录。

    Yields:
        将已绑定尝试的线程控制权交还官方 Runner。

    Raises:
        RuntimeError: 当前 Agent 没有活跃的批次上下文。
    """
    if config.effective_agent != AGENT_NAME:
        yield
        return
    with _batch_lock:
        batch = _batches.get(id(config))
    if batch is None:
        raise RuntimeError("native banking batch is not active")
    context = AttemptContext(simulation_id, task.id, seed, batch, Event())
    if save_dir is not None:
        context.log_path = Path(save_dir).resolve() / (
            f"runtime-{quote(task.id, safe='')}-{quote(simulation_id, safe='')}"
            f"-{uuid4().hex}.log"
        )
    with bound_attempt(context):
        yield


def native_batch(function: Callable[..., Any]) -> Callable[..., Any]:
    """仅包装当前 Agent 的 run_domain，保留官方调度与评分。"""

    @wraps(function)
    def run(config: Any, *args: Any, **kwargs: Any) -> Any:
        if config.effective_agent != AGENT_NAME:
            return function(config, *args, **kwargs)
        with batch_scope(config):
            return function(config, *args, **kwargs)

    return run


def request_timeout(
    arguments: dict[str, object], *, default: float, case_timeout: float
) -> float:
    """保留显式 SDK 超时，但不超过 case 总预算。

    Args:
        arguments: 原生 LLM 参数，可包含 ``timeout``。
        default: 未显式指定时使用的超时。
        case_timeout: 当前 case 允许的最大超时。

    Returns:
        受 case 总预算限制的显式超时或默认超时。

    Raises:
        ValueError: 选择的超时不是有限正数。
    """
    value = arguments.get("timeout", default)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("LLM timeout must be finite and positive")
    return min(float(value), case_timeout)


@contextmanager
def batch_scope(config: Any) -> Iterator[BatchContext]:
    """在原生调度前冻结评测目标并完成预检。

    作用域内临时设置用户模拟器和裁判模型的限制；退出时恢复原值，
    并移除批次登记。

    Args:
        config: 原生 Tau2 运行配置。

    Yields:
        各次独立 simulation 尝试共享的批次配置。

    Raises:
        ValueError: 运行参数或 Tau2 checkout 未通过预检。
        RuntimeError: 当前进程已有活跃的 Banking 批次。
    """
    validate_run(config.model_dump())
    integration = load_config()
    _verify_upstream()
    root = Path(tempfile.mkdtemp(prefix="t2batch-", dir="/tmp"))
    batch = BatchContext(
        integration,
        root / integration.agent_dir.name,
        config.llm_agent,
        config.llm_args_agent.get("temperature", 0),
        config.timeout,
    )
    original_user_args = config.llm_args_user
    from tau2.evaluator import evaluator_nl_assertions

    original_judge_args = evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS
    original_judge_model = evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS
    try:
        shutil.copytree(
            integration.agent_dir,
            batch.source,
            ignore=shutil.ignore_patterns("__pycache__", ".env", ".git"),
        )
        make_tree_writable(batch.source)
        probe = CaseRuntime(
            integration, batch.source, model=batch.model, temperature=batch.temperature
        )
        try:
            worker_command(integration, "check", probe.root, probe.environ)
        finally:
            probe.close()
        with _batch_lock:
            if _batches:
                raise RuntimeError(
                    "one native banking batch per CLI process is supported"
                )
            _batches[id(config)] = batch
        # 在 worker 启动前统一设置有限的 SDK 预算，作用域退出时恢复。
        config.llm_args_user = dict(original_user_args or {})
        config.llm_args_user.setdefault("max_tokens", 32768)
        config.llm_args_user["timeout"] = request_timeout(
            config.llm_args_user,
            default=integration.http_timeout_seconds,
            case_timeout=batch.timeout,
        )
        evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {
            **original_judge_args,
            "max_tokens": 32768,
            "timeout": request_timeout(
                original_judge_args,
                default=integration.http_timeout_seconds,
                case_timeout=batch.timeout,
            ),
        }
        evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = config.llm_user
        yield batch
    finally:
        config.llm_args_user = original_user_args
        evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = original_judge_args
        evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = original_judge_model
        with _batch_lock:
            _batches.pop(id(config), None)
        shutil.rmtree(root)


def _verify_upstream() -> None:
    head = subprocess.run(
        [
            "git",
            "-C",
            str(TAU2_PROJECT),
            "describe",
            "--tags",
            "--exact-match",
            "--match",
            UPSTREAM_TAG,
            "HEAD",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if head.returncode or head.stdout.strip() != UPSTREAM_TAG:
        raise ValueError("Tau2 checkout does not match the v1.0.1 tag")
    patch = Path(__file__).parent / "patches/native-banking.patch"
    result = subprocess.run(
        ["git", "-C", str(TAU2_PROJECT), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        timeout=5,
    )
    if result.returncode:
        raise ValueError("native banking Runner patch is missing or incompatible")
