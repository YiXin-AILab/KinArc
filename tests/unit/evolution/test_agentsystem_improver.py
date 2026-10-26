"""进程内 agentsystem Improver 适配测试。"""

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from agentsystem.common.errors import ServiceError, ToolError
from agentsystem.common.models import SessionContext
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.application import ApplicationService
from agentsystem.service.host import ServiceHost
from agentsystem.service.models import (
    ApplicationStatus,
    CancelCommand,
    MessageSubmissionResult,
    SessionPublicResult,
    SessionSnapshot,
    SubmitMessageCommand,
)
from agentsystem.tool.models import ToolOutput
from evolution.agent.plugins.evidence_tools import EvidenceTools
from evolution.agent.plugins.evolution_tools import _WorkspaceHandlers
from evolution.agent.plugins.file_tools import WorkspaceTools
from evolution.agentsystem_improver import (
    AgentSystemImprover,
    _request_text,
)
from evolution.diagnosis import (
    ProblemLibrary,
    build_diagnosis_brief,
    split_actionable_clusters,
    update_problem_library,
)
from evolution.improver import ImprovementRequest
from evolution.models import (
    CaseEvaluation,
    EvaluationResult,
    ExecutionMetadata,
    ParentComparison,
)


class _Application:
    def __init__(self, snapshots: list[SessionSnapshot | None]) -> None:
        self._snapshots = iter(snapshots)
        self.context: SessionContext | None = None
        self.command: SubmitMessageCommand | None = None

    async def submit_message(
        self,
        ctx: SessionContext,
        request: SubmitMessageCommand,
    ) -> MessageSubmissionResult:
        self.context = ctx
        self.command = request
        return MessageSubmissionResult(message_id=request.message_id)

    async def snapshot(self, ctx: SessionContext) -> SessionSnapshot | None:
        assert ctx == self.context
        return next(self._snapshots)

    async def cancel(
        self,
        ctx: SessionContext,
        request: CancelCommand,
    ) -> ApplicationStatus:
        del ctx, request
        return ApplicationStatus.ACCEPTED


class _Host:
    def __init__(self, application: _Application) -> None:
        self.closed = False
        service = create_autospec(ApplicationService, instance=True, spec_set=True)
        service.submit_message.side_effect = application.submit_message
        service.snapshot.side_effect = application.snapshot
        service.cancel.side_effect = application.cancel
        host = create_autospec(ServiceHost, instance=True)
        # application 在真实宿主的 __init__ 中创建，类级 autospec 不包含该属性。
        host.application = service
        host.close.side_effect = self.close
        self.runtime: ServiceHost = host

    async def close(self) -> None:
        self.closed = True


def _request(tmp_path: Path) -> ImprovementRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    metadata = ExecutionMetadata(total_tokens=1, duration_seconds=0.1)
    evaluation = EvaluationResult(
        total_score=0.5,
        cases=(
            CaseEvaluation(
                id="train-1",
                result="failed",
                tracelog={},
                metadata=metadata,
                expected={},
                score=0,
                feedback="missing verification",
            ),
        ),
    )
    return ImprovementRequest(
        objective="improve banking",
        base_candidate_id="candidate-20260901T000000",
        changelog_source_candidate_id="candidate-20260901T000000",
        evidence_candidate_id="candidate-20260901T000000",
        output_candidate_id="candidate-output",
        evidence_parent_candidate_id=None,
        workspace_dir=workspace,
        read_only=("data/",),
        train_evaluation=evaluation,
        diagnosis_brief=build_diagnosis_brief("candidate-20260901T000000", evaluation),
        changelog_path=tmp_path / "changelog.md",
    )


def _snapshot(
    status: SessionStatus,
    *,
    content: str | None = "done",
) -> SessionSnapshot:
    return SessionSnapshot(
        session_id="session-1",
        status=status,
        result=SessionPublicResult(content=content)
        if status is SessionStatus.SUCCEEDED
        else None,
    )


async def test_improver_submits_only_train_and_workspace_context(
    tmp_path: Path,
) -> None:
    """请求正文不得向 Evolution Agent 暴露 Validation 或 Test。"""
    application = _Application([None, _snapshot(SessionStatus.SUCCEEDED)])
    host = _Host(application)
    workspace_tools = WorkspaceTools()
    evidence_tools = EvidenceTools()
    improver = AgentSystemImprover(
        host=host.runtime,
        request_timeout_seconds=1,
        poll_interval_seconds=0,
        id_factory=lambda: "fixed-id",
        workspace_tools=workspace_tools,
        evidence_tools=evidence_tools,
    )

    await improver.improve(_request(tmp_path))

    assert application.command is not None
    block = application.command.content[0]
    assert block.type == "text"
    assert "improve banking" in block.text
    assert "Workspace：.\n" in block.text
    assert str(tmp_path / "workspace") not in block.text
    assert "missing verification" not in block.text
    assert "evaluation.json" not in block.text
    assert application.context is not None
    assert application.context.agent_id == "evolution-agent"
    with pytest.raises(ToolError) as files_unbound:
        workspace_tools.list_path()
    with pytest.raises(ToolError) as evidence_unbound:
        evidence_tools.read_case_evidence("train-1")
    assert files_unbound.value.code == "request_not_bound"
    assert evidence_unbound.value.code == "request_not_bound"


async def test_improver_rejects_non_success_terminal(tmp_path: Path) -> None:
    """失败终态不得被自然语言完成消息掩盖。"""
    application = _Application([_snapshot(SessionStatus.FAILED)])
    improver = AgentSystemImprover(
        host=_Host(application).runtime,
        request_timeout_seconds=1,
        poll_interval_seconds=0,
        id_factory=lambda: "fixed-id",
    )

    with pytest.raises(ServiceError, match="FAILED"):
        await improver.improve(_request(tmp_path))


@pytest.mark.parametrize("content", [None, "  \n"])
async def test_improver_accepts_success_without_review(
    tmp_path: Path,
    content: str | None,
) -> None:
    """成功终态即可交付文件，不要求最终文本。"""
    application = _Application([_snapshot(SessionStatus.SUCCEEDED, content=content)])
    improver = AgentSystemImprover(
        host=_Host(application).runtime,
        request_timeout_seconds=1,
        poll_interval_seconds=0,
        id_factory=lambda: "fixed-id",
    )

    await improver.improve(_request(tmp_path))


async def test_improver_cancels_runtime_run_when_request_times_out(
    tmp_path: Path,
) -> None:
    """请求总超时必须取消实际 run，不能只停止状态轮询。"""

    class RunningApplication(_Application):
        def __init__(self) -> None:
            super().__init__([])
            self.canceled_context: SessionContext | None = None

        async def snapshot(self, ctx: SessionContext) -> SessionSnapshot:
            assert ctx == self.context
            if self.canceled_context is not None:
                return _snapshot(SessionStatus.CANCELED)
            return _snapshot(SessionStatus.RUNNING)

        async def cancel(
            self,
            ctx: SessionContext,
            request: CancelCommand,
        ) -> ApplicationStatus:
            del request
            self.canceled_context = ctx
            return ApplicationStatus.ACCEPTED

    application = RunningApplication()
    improver = AgentSystemImprover(
        host=_Host(application).runtime,
        request_timeout_seconds=0.01,
        poll_interval_seconds=0,
        id_factory=lambda: "fixed-id",
    )

    with pytest.raises(TimeoutError):
        await improver.improve(_request(tmp_path))

    assert application.canceled_context == application.context


async def test_improver_waits_through_idle_and_pending(tmp_path: Path) -> None:
    """尚未领取和挂起均不能误判为执行终态。"""
    application = _Application(
        [
            None,
            _snapshot(SessionStatus.IDLE),
            _snapshot(SessionStatus.RUNNING),
            _snapshot(SessionStatus.PENDING),
            _snapshot(SessionStatus.SUCCEEDED),
        ]
    )
    improver = AgentSystemImprover(
        host=_Host(application).runtime,
        request_timeout_seconds=1,
        poll_interval_seconds=0,
    )
    await improver.improve(_request(tmp_path))


@pytest.mark.parametrize("failure", ["submit", "snapshot", "timeout", "cancel"])
async def test_failure_waits_for_stopped_before_unbinding(
    tmp_path: Path,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """提交结果不确定、查询失败、超时和取消都必须保留绑定直到终态。"""
    caplog.set_level(logging.INFO, logger="evolution.agentsystem_improver")
    canceled = asyncio.Event()
    stopped = asyncio.Event()
    submitted = asyncio.Event()

    class Application(_Application):
        async def submit_message(
            self,
            ctx: SessionContext,
            request: SubmitMessageCommand,
        ) -> MessageSubmissionResult:
            result = await super().submit_message(ctx, request)
            submitted.set()
            if failure == "submit":
                raise ValueError("secret-token-not-for-record")
            if failure in {"timeout", "cancel"}:
                await asyncio.Event().wait()
            return result

        async def snapshot(self, ctx: SessionContext) -> SessionSnapshot:
            if failure == "snapshot" and not canceled.is_set():
                # Only the request observation fails; cleanup can inspect it.
                failure_seen = getattr(self, "failure_seen", False)
                self.failure_seen = True
                if not failure_seen:
                    raise ValueError("secret-token-not-for-record")
            if canceled.is_set():
                await stopped.wait()
                return _snapshot(SessionStatus.CANCELED)
            return _snapshot(SessionStatus.RUNNING)

        async def cancel(
            self,
            ctx: SessionContext,
            request: CancelCommand,
        ) -> ApplicationStatus:
            canceled.set()
            return ApplicationStatus.ACCEPTED

    files = WorkspaceTools()
    improver = AgentSystemImprover(
        host=_Host(Application([])).runtime,
        request_timeout_seconds=0.02,
        cleanup_timeout_seconds=1,
        poll_interval_seconds=0,
        workspace_tools=files,
    )
    request = _request(tmp_path)
    task = asyncio.create_task(improver.improve(request))
    await submitted.wait()
    if failure == "cancel":
        task.cancel()
    try:
        await asyncio.wait_for(canceled.wait(), 0.5)
        files.list_path()
        assert not task.done()
    finally:
        stopped.set()
        expected = {
            "submit": ValueError,
            "snapshot": ValueError,
            "timeout": TimeoutError,
            "cancel": asyncio.CancelledError,
        }[failure]
        with pytest.raises(expected):
            await task
    assert f"exception_type={expected.__name__} terminal=False" in caplog.text
    assert f"task_cancelling={int(failure == 'cancel')}" in caplog.text
    assert "improver_cancel_requested source=improver_cleanup" in caplog.text
    assert "elapsed_seconds=" in caplog.text
    assert "timeout_seconds=0.02" in caplog.text
    assert "pid=" in caplog.text
    assert "secret-token-not-for-record" not in caplog.text
    with pytest.raises(ToolError):
        files.list_path()


async def test_cleanup_failure_closes_host_and_prevents_rebinding(
    tmp_path: Path,
) -> None:
    """未调度请求无法证明停止时关闭宿主并禁止后续调用。"""

    class Application(_Application):
        async def snapshot(self, ctx: SessionContext) -> None:
            return None

    host = _Host(Application([]))
    improver = AgentSystemImprover(
        host=host.runtime,
        request_timeout_seconds=0.01,
        cleanup_timeout_seconds=0.02,
        poll_interval_seconds=0,
    )
    with pytest.raises(ServiceError) as raised:
        await improver.improve(_request(tmp_path))
    assert raised.value.code == "improver_cleanup_failed"
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert host.closed
    with pytest.raises(ServiceError) as repeated:
        await improver.improve(_request(tmp_path / "second"))
    assert repeated.value.code == "improver_cleanup_failed"


@pytest.mark.parametrize("release_late", [False, True])
async def test_canceled_thread_finishes_before_tool_binding_is_cleared(
    tmp_path: Path,
    release_late: bool,
) -> None:
    """取消线程等待不得使仍运行的文件操作访问下一轮绑定。"""
    from evolution.agent.plugins.evolution_tools import ToolThreads

    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    threads = ToolThreads()

    class Files(WorkspaceTools):
        def list_path(self, path: str = ".", max_depth: int = 1) -> ToolOutput:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            return super().list_path(path, max_depth)

    files = Files()
    handler = _WorkspaceHandlers(files, threads)

    class Application(_Application):
        async def snapshot(self, ctx: SessionContext) -> SessionSnapshot:
            return _snapshot(SessionStatus.CANCELED)

        async def submit_message(
            self,
            ctx: SessionContext,
            request: SubmitMessageCommand,
        ) -> MessageSubmissionResult:
            result = await super().submit_message(ctx, request)
            task = asyncio.create_task(handler.list_path())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return result

    improver = AgentSystemImprover(
        host=_Host(Application([])).runtime,
        request_timeout_seconds=0.05 if release_late else 1,
        cleanup_timeout_seconds=0.05 if release_late else 1,
        workspace_tools=files,
        tool_threads=threads,
    )
    task = asyncio.create_task(improver.improve(_request(tmp_path)))
    await entered.wait()
    await asyncio.sleep(0)
    assert not task.done()
    if release_late:
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            WorkspaceTools.list_path(files)
            with pytest.raises(ServiceError):
                await improver.improve(_request(tmp_path / "second"))
        finally:
            release.set()
            await threads.wait()
        return
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ToolError):
        WorkspaceTools.list_path(files)


async def test_failed_host_close_retains_binding_and_both_error_types(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """关闭失败不能乐观解绑，主错误与清理错误需可追溯。"""
    primary_message = "private-service-address"
    close_message = "private-close-secret"

    class Application(_Application):
        async def snapshot(self, ctx: SessionContext) -> None:
            raise LookupError(primary_message)

    class Host(_Host):
        async def close(self) -> None:
            raise OSError(close_message)

    files = WorkspaceTools()
    improver = AgentSystemImprover(
        host=Host(Application([])).runtime,
        request_timeout_seconds=1,
        cleanup_timeout_seconds=0.02,
        workspace_tools=files,
    )
    request = _request(tmp_path)
    with pytest.raises(ServiceError) as raised:
        await improver.improve(request)
    assert raised.value.code == "improver_cleanup_failed"
    assert isinstance(raised.value.__cause__, LookupError)
    files.list_path()
    primary_logs = [
        item
        for item in caplog.records
        if "improver_request_failed" in item.getMessage()
    ]
    cleanup_logs = [
        item
        for item in caplog.records
        if "improver_host_close_failed" in item.getMessage()
    ]
    assert len(primary_logs) == 1
    assert len(cleanup_logs) == 1
    assert primary_logs[0].exc_info is not None
    assert cleanup_logs[0].exc_info is not None
    assert "session_id=evolution-" in primary_logs[0].getMessage()
    assert "private-" not in caplog.text
    # Engine consumes cleanup_failed as a result; context exit must not raise it again.
    await improver.close()


@pytest.mark.parametrize("code", ["provider_error", "token=private-secret"])
async def test_session_log_keeps_only_valid_public_error_code(
    tmp_path: Path,
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """日志保留稳定错误码和session，不输出错误正文与非法错误码。"""
    caplog.set_level(logging.INFO, logger="evolution.agentsystem_improver")
    snapshot = SessionSnapshot(
        session_id="session-1",
        status=SessionStatus.FAILED,
        result=SessionPublicResult(error_code=code, error_message="private-token"),
    )
    improver = AgentSystemImprover(
        host=_Host(_Application([snapshot])).runtime,
        request_timeout_seconds=1,
    )
    request = _request(tmp_path)
    with pytest.raises(ServiceError):
        await improver.improve(request)
    assert "private-token" not in caplog.text
    assert "private-secret" not in caplog.text
    assert "session_id=evolution-" in caplog.text
    assert ("error_code=provider_error" in caplog.text) == (code == "provider_error")


async def test_improver_context_manager_closes_host(tmp_path: Path) -> None:
    """命令结束必须释放其专用 ServiceHost。"""
    host = _Host(_Application([_snapshot(SessionStatus.SUCCEEDED)]))
    improver = AgentSystemImprover(
        host=host.runtime,
        request_timeout_seconds=1,
        poll_interval_seconds=0,
        id_factory=lambda: "fixed-id",
    )

    async with improver:
        await improver.improve(_request(tmp_path))

    assert host.closed is True


async def test_improver_preserves_body_error_when_host_close_fails() -> None:
    """清理失败不得覆盖触发清理的原始请求异常。"""

    class FailingCloseHost(_Host):
        async def close(self) -> None:
            raise RuntimeError("close failed")

    improver = AgentSystemImprover(
        host=FailingCloseHost(_Application([])).runtime,
        request_timeout_seconds=1,
    )
    primary = TimeoutError("request timed out")

    with pytest.raises(TimeoutError) as raised:
        async with improver:
            raise primary

    assert raised.value is primary
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_user_prompt_includes_train_comparison_and_all_case_summaries(
    tmp_path: Path,
) -> None:
    """优化器第一次模型调用就应看到评分基线，但不能内嵌标准答案和轨迹。"""
    request = _request(tmp_path)
    first = request.train_evaluation.cases[0].model_copy(
        update={
            "previous_score": 1.0,
            "result": "agent_error",
            "tracelog": "TRACE_NOT_INLINE",
            "expected": "EXPECTED_NOT_INLINE",
            "feedback": "first line\nsecond line",
        }
    )
    second = first.model_copy(
        update={
            "id": "train-2",
            "score": 1.0,
            "previous_score": None,
            "feedback": "",
        }
    )
    evaluation = EvaluationResult(
        total_score=0.5,
        cases=(first, second),
        parent_comparison=ParentComparison(
            previous_candidate_id="candidate-old",
            current_candidate_id="candidate-new",
            previous_total_score=1,
            current_total_score=0.5,
            total_score_delta=-0.5,
            previous_case_count=2,
            current_case_count=2,
            improved_case_count=0,
            regressed_case_count=1,
            unchanged_case_count=1,
        ),
    )
    text = _request_text(
        request.model_copy(
            update={
                "train_evaluation": evaluation,
                "evidence_candidate_id": "candidate-new",
                "evidence_parent_candidate_id": "candidate-old",
                "diagnosis_brief": build_diagnosis_brief("candidate-new", evaluation),
            }
        )
    )
    assert "评测版本：candidate-new\n迭代版本：candidate-output" in text
    assert "参考评测\n## 诊断概要" in text
    assert "评测版本：candidate-new  总分：0.5000" in text
    assert "比较基线：candidate-old  总分：1.0000" in text
    assert "## 失败簇" in text and "cluster_1 · regression" in text
    assert "## 本轮退步 Case" in text and "- train-1：1 → 0" in text
    assert "## 当前通过 Case（回归保护对象）" in text and "train-2" in text
    assert "Case\t本次分数\t上次分数\t变化" in text
    assert "train-1\t0.0\t1.0\t-1" in text
    assert "train-2\t1.0\t—\t—" in text
    assert "历史总分" not in text and "历史比较" not in text
    assert "agent_error" not in text
    assert "TRACE_NOT_INLINE" not in text
    assert "EXPECTED_NOT_INLINE" not in text
    assert "first line" not in text
    assert "second line" not in text


def test_user_prompt_uses_only_evaluation_and_next_versions(tmp_path: Path) -> None:
    """仅展示评测版本和迭代版本，来源 ID 不进入提示词，父级仅作诊断基线。"""
    base = _request(tmp_path)
    request = base.model_copy(
        update={
            "base_candidate_id": "candidate-B",
            "changelog_source_candidate_id": "candidate-B",
            "evidence_candidate_id": "candidate-B",
            "evidence_parent_candidate_id": "candidate-P",
            "diagnosis_brief": build_diagnosis_brief(
                "candidate-B",
                base.train_evaluation,
                baseline_candidate_id="candidate-P",
            ),
        }
    )
    text = _request_text(request)
    assert "Workspace：.\n" in text
    assert str(request.workspace_dir) not in text
    assert f"Changelog Path（可读写）：{request.changelog_path}" in text
    assert "评测版本：candidate-B\n迭代版本：candidate-output" in text
    assert text.count("candidate-B") == 2
    assert text.count("candidate-output") == 1
    assert "比较基线：candidate-P  总分：—" in text
    assert "代码起点" not in text and "日志继承来源" not in text
    assert "missing verification" not in text


@pytest.mark.parametrize("mode", ["best", "latest"])
def test_restoration_prompt_identifies_reused_evidence(
    tmp_path: Path, mode: str
) -> None:
    request = _request(tmp_path).model_copy(
        update={
            "evidence_reused_from_candidate_id": "candidate-V",
            "rollback_changelog_source": mode,
        }
    )
    text = _request_text(request)
    assert "本候选创建时复制评测证据自：candidate-V" in text
    assert f"恢复日志模式：{mode}" in text
    assert "当前评分与轨迹读取本候选自己的结果文件" in text
    assert "并非本候选重新执行的结果" in text


def test_user_prompt_without_comparison_does_not_invent_baseline(
    tmp_path: Path,
) -> None:
    """Seed 没有基线时明确标空，不借用代码起点的分数。"""
    text = _request_text(_request(tmp_path))
    assert "比较基线：无  总分：—" in text
    assert "train-1\t0.0\t—\t—" in text
    assert "## 问题库" not in text


def test_user_prompt_lists_deferred_single_case_clusters(tmp_path: Path) -> None:
    """延后的单 case 簇只出现在问题库小节，不混入可处理失败簇。"""
    base = _request(tmp_path)
    brief = build_diagnosis_brief("candidate-20260901T000000", base.train_evaluation)
    library = update_problem_library(ProblemLibrary(), brief)
    actionable, deferred = split_actionable_clusters(brief, library)
    assert deferred != ()
    request = base.model_copy(
        update={
            "diagnosis_brief": actionable,
            "deferred_clusters": deferred,
            "problem_library": library,
        }
    )
    text = _request_text(request)
    assert "本轮无可处理失败簇" in text
    assert "全部通过" not in text
    assert "未通过 Case 1 个" in text
    assert "本轮暂缓：1 个 Case / 1 个簇" in text
    assert "## 问题库（单 case 簇积累中，本轮不处理）" in text
    assert "- 签名 `missing_verification` · total_failure · 已积累 1/2 个 case" in text
    assert "最近出现于 candidate-20260901T000000" in text


class _BlockingApplication(_Application):
    def __init__(self) -> None:
        super().__init__([])
        self.submitted = asyncio.Event()
        self.release = asyncio.Event()

    async def submit_message(
        self,
        ctx: SessionContext,
        request: SubmitMessageCommand,
    ) -> MessageSubmissionResult:
        result = await super().submit_message(ctx, request)
        self.submitted.set()
        return result

    async def snapshot(self, ctx: SessionContext) -> SessionSnapshot | None:
        assert ctx == self.context
        await self.release.wait()
        return _snapshot(SessionStatus.SUCCEEDED)


async def test_improver_rejects_concurrent_requests(tmp_path: Path) -> None:
    """并发 improve 不得覆盖请求级工具绑定。"""
    application = _BlockingApplication()
    improver = AgentSystemImprover(
        host=_Host(application).runtime,
        request_timeout_seconds=1,
        poll_interval_seconds=0,
        id_factory=lambda: "fixed-id",
    )
    first = asyncio.create_task(improver.improve(_request(tmp_path)))
    await application.submitted.wait()

    try:
        with pytest.raises(RuntimeError, match="不允许并发"):
            await asyncio.wait_for(
                improver.improve(_request(tmp_path / "second")),
                timeout=0.1,
            )
    finally:
        application.release.set()
        await first


def test_prompt_omits_inaccessible_rules_and_overlapping_read_only(
    tmp_path: Path,
) -> None:
    """隐藏策略与被隐藏的只读路径都不应出现在优化器输入。"""
    request = _request(tmp_path).model_copy(
        update={
            "read_only": ("data/", "secret/", ".internal/"),
            "inaccessible": ("secret/", "**/.*", "**/__pycache__"),
        }
    )
    text = (
        _request_text(request)
        .replace(str(request.workspace_dir), "")
        .replace(str(request.changelog_path), "")
    )
    assert "data/" in text
    for hidden in ("secret", ".internal", "inaccessible", "__pycache__", "**/.*"):
        assert hidden not in text
