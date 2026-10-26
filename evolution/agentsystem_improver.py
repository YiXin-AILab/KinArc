"""通过当前 agentsystem Runtime 进程内运行 Evolution Agent。"""

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from agentsystem.common.errors import (
    ServiceError,
    ServiceErrorKind,
    validate_error_code,
)
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import SessionContext
from agentsystem.config.loader import interpolate_environment, load_yaml_config
from agentsystem.config.models import AgentConfig, RuntimeConfig
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.provider import LLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.plugin.models import PluginSource
from agentsystem.redis.client import RedisClient
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import (
    CancelCommand,
    SessionSnapshot,
    SubmitMessageCommand,
)

from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.agent.plugins.evolution_tools import (
    ToolThreads,
    create_evolution_tool_plugin,
)
from evolution.agent.plugins.file_tools import WorkspaceTools
from evolution.diagnosis import (
    ProblemLibrary,
    render_brief_markdown,
    render_deferred_section,
)
from evolution.improver import ImprovementRequest
from evolution.workspace import WorkspaceAccess

LOGGER = logging.getLogger(__name__)


class AgentSystemImprover:
    """把 Workspace 改进请求映射到进程内 ApplicationService。"""

    def __init__(
        self,
        *,
        host: ServiceHost,
        request_timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
        id_factory: Callable[[], str] | None = None,
        workspace_tools: WorkspaceTools | None = None,
        evidence_tools: EvidenceTools | None = None,
        cleanup_timeout_seconds: float = 10.0,
        tool_threads: ToolThreads | None = None,
    ) -> None:
        """绑定运行宿主及请求级工具，校验执行与清理的时间预算。"""
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds 必须为正数")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds 不得为负数")
        if cleanup_timeout_seconds <= 0:
            raise ValueError("cleanup_timeout_seconds 必须为正数")
        self._host = host
        self._timeout = request_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._workspace_tools = workspace_tools or WorkspaceTools()
        self._evidence_tools = evidence_tools or EvidenceTools()
        self._improve_lock = asyncio.Lock()
        self._cleanup_timeout = cleanup_timeout_seconds
        self._poisoned = False
        self._binding_safe = True
        self._tool_threads = tool_threads or ToolThreads()

    @classmethod
    async def create(
        cls,
        *,
        runtime_config: RuntimeConfig,
        project_root: Path,
        redis_prefix: str,
        request_timeout_seconds: float,
        environ: Mapping[str, str] = os.environ,
        redis_client: RedisClient | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> "AgentSystemImprover":
        """创建并启动命令生命周期内唯一的 Evolution ServiceHost。"""
        redis_config = runtime_config.redis.model_copy(
            update={"key_prefix": redis_prefix}
        )
        isolated_runtime = runtime_config.model_copy(update={"redis": redis_config})
        workspace_tools = WorkspaceTools()
        evidence_tools = EvidenceTools()
        tool_threads = ToolThreads()
        plugin_source = create_evolution_tool_plugin(
            workspace_tools,
            evidence_tools,
            tool_threads,
        )
        host = await ServiceHost.create(
            runtime_config=isolated_runtime,
            project_root=project_root,
            agents=(
                load_evolution_agent_spec(
                    environ=environ,
                    llm_provider=llm_provider,
                    plugin_source=plugin_source,
                ),
            ),
            consumer_id=f"evolution-{uuid4().hex}",
            redis_client=redis_client,
        )
        try:
            await host.start()
        except BaseException:
            await host.close()
            raise
        return cls(
            host=host,
            request_timeout_seconds=request_timeout_seconds,
            workspace_tools=workspace_tools,
            evidence_tools=evidence_tools,
            cleanup_timeout_seconds=runtime_config.shutdown_timeout_seconds,
            tool_threads=tool_threads,
        )

    async def __aenter__(self) -> "AgentSystemImprover":
        """返回已启动的适配器。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """无论命令结果如何都关闭专用 ServiceHost。"""
        del exc_type
        try:
            await self.close()
        except BaseException as close_error:
            if exc_value is None:
                raise
            raise exc_value.with_traceback(traceback) from close_error

    async def close(self) -> None:
        """关闭专用宿主；停止屏障失败时已有一次有界关闭及诊断。"""
        if self._poisoned:
            return
        await self._host.close()

    async def improve(self, request: ImprovementRequest) -> None:
        """提交一轮文件改进并确认成功终态及停止屏障。

        文件和证据工具只绑定到当前请求；安全停止后解除绑定。
        若无法确认清理完成，将拒绝后续请求，避免上一轮继续修改新工作区。

        Args:
            request: 当前轮次的工作区、Changelog、Train 证据和保护规则。

        Raises:
            RuntimeError: 同一实例已经在处理另一轮改进。
            ServiceError: 优化以失败终态结束，或无法安全完成清理。
            TimeoutError: 优化请求超过时间预算。
            asyncio.CancelledError: 调用方或运行中的优化 Agent 取消请求。
        """
        if self._improve_lock.locked():
            raise RuntimeError("Improver 不允许并发调用")
        if self._poisoned:
            raise _cleanup_failed_error()
        async with self._improve_lock:
            self._workspace_tools.bind(request)
            self._evidence_tools.bind(request)
            try:
                await self._run_request(request)
            finally:
                if self._binding_safe:
                    self._workspace_tools.unbind()
                    self._evidence_tools.unbind()

    async def _run_request(self, request: ImprovementRequest) -> None:
        """提交已绑定请求并等待公开成功终态。"""
        identifier = self._id_factory()
        context = SessionContext(
            app_id="evolution",
            user_id="local-evolution",
            agent_id="evolution-agent",
            session_id=f"evolution-{identifier}",
        )
        LOGGER.info(
            "improver_session_started session_id=%s workspace=%s pid=%s ppid=%s",
            context.session_id,
            request.workspace_dir,
            os.getpid(),
            os.getppid(),
        )
        started_at = asyncio.get_running_loop().time()
        terminal = False
        self._binding_safe = False
        try:
            async with asyncio.timeout(self._timeout):
                await self._host.application.submit_message(
                    context,
                    SubmitMessageCommand(
                        message_id=f"message-{identifier}",
                        role=InboundRole.USER,
                        origin="evolution",
                        content=(TextBlock(text=_request_text(request)),),
                    ),
                )
                snapshot = await self._wait_terminal(context)
                terminal = True
                _require_success(snapshot)
                await self._tool_threads.wait()
                self._binding_safe = True
        except BaseException as error:
            task = asyncio.current_task()
            LOGGER.info(
                "improver_cleanup_started session_id=%s pid=%s "
                "exception_type=%s terminal=%s elapsed_seconds=%.3f "
                "timeout_seconds=%s task_cancelling=%s",
                context.session_id,
                os.getpid(),
                type(error).__name__,
                terminal,
                asyncio.get_running_loop().time() - started_at,
                self._timeout,
                task.cancelling() if task is not None else None,
            )
            cancellation: asyncio.CancelledError | None = None
            cleanup = asyncio.create_task(self._cleanup(context, terminal=terminal))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as canceled:
                    cancellation = canceled
            cleanup_error = cleanup.result()
            if cancellation is not None:
                raise cancellation from error
            if not isinstance(error, Exception):
                raise
            if cleanup_error is not None:
                if not isinstance(error, ServiceError):
                    log_unexpected_exception(
                        LOGGER,
                        "improver_request_failed",
                        error,
                        operation="evolution.improver",
                        session_id=context.session_id,
                    )
                raise _cleanup_failed_error() from error
            raise
        finally:
            LOGGER.info(
                "improver_session_finished session_id=%s cleanup_confirmed=%s",
                context.session_id,
                self._binding_safe and not self._poisoned,
            )

    async def _wait_terminal(
        self,
        context: SessionContext,
        *,
        cancel: bool = False,
    ) -> SessionSnapshot:
        """仅明确终态证明单条输入已领取并结束；cancel 返回不是停止屏障。"""
        while True:
            snapshot = await self._host.application.snapshot(context)
            if snapshot is not None:
                if snapshot.status in {
                    SessionStatus.SUCCEEDED,
                    SessionStatus.FAILED,
                    SessionStatus.CANCELED,
                }:
                    _log_terminal(context, snapshot)
                    return snapshot
                if cancel and snapshot.status in {
                    SessionStatus.RUNNING,
                    SessionStatus.PENDING,
                }:
                    LOGGER.info(
                        "improver_cancel_requested source=improver_cleanup "
                        "session_id=%s pid=%s status=%s",
                        context.session_id,
                        os.getpid(),
                        snapshot.status.value,
                    )
                    await self._host.application.cancel(context, CancelCommand())
                    cancel = False
            await asyncio.sleep(self._poll_interval)

    async def _cleanup(
        self,
        context: SessionContext,
        *,
        terminal: bool,
    ) -> Exception | None:
        """有界确认停止，无法确认时关闭并永久禁用专用宿主。"""
        deadline = asyncio.get_running_loop().time() + self._cleanup_timeout
        try:
            # Reserve half of the single cleanup budget for host shutdown.
            async with asyncio.timeout(self._cleanup_timeout / 2):
                if not self._binding_safe:
                    if not terminal:
                        await self._wait_terminal(context, cancel=True)
                    await self._tool_threads.wait()
                self._binding_safe = True
            return None
        except Exception as error:
            self._poisoned = True
            try:
                async with asyncio.timeout_at(deadline):
                    await self._host.close()
                    await self._tool_threads.wait()
                    self._binding_safe = True
            except Exception as close_error:
                log_unexpected_exception(
                    LOGGER,
                    "improver_host_close_failed",
                    close_error,
                    operation="evolution.improver_cleanup",
                    session_id=context.session_id,
                )
            else:
                log_unexpected_exception(
                    LOGGER,
                    "improver_cleanup_failed",
                    error,
                    operation="evolution.improver_cleanup",
                    session_id=context.session_id,
                )
            return error


def _cleanup_failed_error() -> ServiceError:
    """返回不可重试的停止屏障错误，不复制底层错误正文。"""
    return ServiceError(
        kind=ServiceErrorKind.UNAVAILABLE,
        code="improver_cleanup_failed",
        message="无法确认前一次优化执行已停止，优化器已禁用",
    )


def _require_success(snapshot: SessionSnapshot) -> None:
    """检查成功终态，并保留外部取消的控制流语义。"""
    if snapshot.status is SessionStatus.CANCELED:
        raise asyncio.CancelledError("Evolution Agent 已取消")
    if snapshot.status is not SessionStatus.SUCCEEDED:
        raise ServiceError(
            kind=ServiceErrorKind.UNAVAILABLE,
            code="improver_execution_failed",
            message=f"Evolution Agent 未成功完成：{snapshot.status.name}",
        )


def _log_terminal(context: SessionContext, snapshot: SessionSnapshot) -> None:
    """记录公开终态及稳定错误码，不回显任意错误正文。"""
    error_code = None
    if snapshot.result is not None and snapshot.result.error_code:
        try:
            error_code = validate_error_code(snapshot.result.error_code)
        except ValueError:
            error_code = None
    LOGGER.info(
        "improver_session_terminal session_id=%s status=%s error_code=%s",
        context.session_id,
        snapshot.status.value,
        error_code,
    )


def _request_text(request: ImprovementRequest) -> str:
    """生成本轮优化任务，详细约束由系统提示词提供。"""
    policy = WorkspaceAccess(inaccessible=request.inaccessible)
    read_only = (
        "\n".join(
            f"- {path}"
            for path in request.read_only
            if not policy.is_inaccessible(path)
        )
        or "无"
    )
    restoration = ""
    if request.evidence_reused_from_candidate_id is not None:
        restoration = (
            "本候选创建时复制评测证据自："
            f"{request.evidence_reused_from_candidate_id}\n"
            f"恢复日志模式：{request.rollback_changelog_source}\n"
            "当前评分与轨迹读取本候选自己的结果文件，"
            "并非本候选重新执行的结果。\n\n"
        )
    return (
        f"优化目标：{request.objective}\n"
        "Workspace：.\n"
        f"Read-only Paths：\n{read_only}\n"
        f"Changelog Path（可读写）：{request.changelog_path}\n\n"
        f"评测版本：{request.evidence_candidate_id}\n"
        f"迭代版本：{request.output_candidate_id}\n\n"
        f"{restoration}"
        f"{_train_evaluation_text(request)}\n\n"
        "按 changelog Skill 复盘评测版本，并记录迭代版本的修改。"
    )


def _train_evaluation_text(request: ImprovementRequest) -> str:
    """以诊断简报为主视图，保留逐 Case 分数底表，不内嵌反馈、轨迹和答案。"""
    evaluation = request.train_evaluation
    parts = [
        "参考评测",
        render_brief_markdown(
            request.diagnosis_brief,
            problem_library=request.problem_library,
            deferred_clusters=request.deferred_clusters,
        ),
    ]
    if request.deferred_clusters:
        parts.extend(
            [
                "",
                render_deferred_section(
                    request.deferred_clusters,
                    request.problem_library or ProblemLibrary(),
                    min_cases=request.single_case_min_cases,
                ),
            ]
        )
    parts.extend(
        [
            "",
            "评分范围：case 分数与总分均为 [0, 1]，满分 1；"
            "总分按评测规则聚合，不是 case 分数之和。",
            "\nCase\t本次分数\t上次分数\t变化",
        ]
    )
    for case in evaluation.cases:
        previous = case.previous_score
        delta = f"{case.score - previous:+.4g}" if previous is not None else "—"
        parts.append(
            f"{case.id}\t{case.score}\t"
            f"{previous if previous is not None else '—'}\t{delta}"
        )
    if not evaluation.cases:
        parts.append("无")
    return "\n".join(parts)


def load_evolution_agent_spec(
    *,
    plugin_source: PluginSource,
    environ: Mapping[str, str] = os.environ,
    llm_provider: LLMProvider | None = None,
) -> AgentRuntimeSpec:
    """从包内静态资产装配唯一 Evolution Agent。"""
    assets = Path(__file__).resolve().parent / "agent"
    config = load_yaml_config(
        assets / "config" / "agent.yaml",
        AgentConfig,
        environ=environ,
    )
    raw_prompt = (assets / config.prompt).read_text(encoding="utf-8")
    prompt = interpolate_environment(raw_prompt, environ)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Evolution Agent Prompt 不能为空")
    return AgentRuntimeSpec(
        agent_config=config,
        base_prompt=prompt.strip(),
        skill_roots=tuple(
            (assets / root).resolve(strict=True) for root in config.skill_roots
        ),
        llm_provider=llm_provider,
        plugin_sources=(plugin_source,),
    )


__all__ = ["AgentSystemImprover", "load_evolution_agent_spec"]
