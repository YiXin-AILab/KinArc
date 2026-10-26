"""ScheduleRun Middleware 状态追踪测试。"""

from datetime import timedelta
from pathlib import Path

from agentsystem.common.errors import RunError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.message.models import Message, MessageVisibility, Role, TextBlock
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecisionType,
    MiddlewareHandler,
    MiddlewarePoint,
    RunMiddlewarePayload,
    RunResultMiddlewarePayload,
)
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.runtime.state import AgentState, SessionStatus
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.models import ScheduleRunQuery, ScheduleRunStatus
from agentsystem.schedule.plugins.schedule_run_middleware import register
from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.service import ScheduleService
from tests.contract.schedule_manager_contract import NOW, SCOPE, make_run, make_task

from .test_tools import Queue

_CONTEXT = SessionContext(
    app_id=SCOPE.app_id,
    user_id="user",
    agent_id=SCOPE.agent_id,
    session_id="session-1",
)


def _state(status: SessionStatus = SessionStatus.RUNNING) -> AgentState:
    is_terminal = status in {
        SessionStatus.SUCCEEDED,
        SessionStatus.FAILED,
        SessionStatus.CANCELED,
    }
    return AgentState(
        session_context=_CONTEXT,
        status=status,
        run_id="runtime-1",
        output_mode=OutputMode.COMPLETE,
        started_at=NOW,
        active_started_at=(NOW if status is SessionStatus.RUNNING else None),
        ended_at=(NOW + timedelta(seconds=2) if is_terminal else None),
        error=(
            RunError(code="execution_failed", message="执行失败")
            if status in {SessionStatus.FAILED, SessionStatus.CANCELED}
            else None
        ),
    )


def _context(
    *,
    point: MiddlewarePoint,
    payload: RunMiddlewarePayload | RunResultMiddlewarePayload,
    status: SessionStatus = SessionStatus.RUNNING,
) -> MiddlewareContext:
    return MiddlewareContext(
        invocation_id="middleware-1",
        point=point,
        ctx=_CONTEXT,
        run_id="runtime-1",
        trace_id="trace-1",
        state_snapshot=_state(status),
        payload=payload,
    )


def _handlers(
    manager: LocalScheduleManager,
) -> tuple[MiddlewareHandler, MiddlewareHandler]:
    service = ScheduleService(
        manager=manager,
        cron=CronSchedule("Asia/Shanghai"),
        clock=lambda: NOW,
        id_factory=lambda: "unused",
        queue=Queue(),
        operation_timeout_seconds=1,
    )
    services = PluginSetupServices()
    services.register(ScheduleService, service)
    services.freeze()
    contribution = load_plugin_contributions(
        (
            PluginSource(
                plugin_id="builtin.schedule.schedule-run-middleware",
                origin=PluginOrigin.BUILTIN,
                path=Path("schedule_run_middleware.py"),
                register=register,
            ),
        ),
        agent_id="agent",
        options={},
        services=services,
    )[0]
    before, after = contribution.middlewares
    return before.handler, after.handler


async def test_before_and_after_run_track_schedule_failure() -> None:
    """BEFORE_RUN 绑定 Runtime，AFTER_RUN 保存安全终态错误。"""
    manager = LocalScheduleManager(cron=CronSchedule("Asia/Shanghai"))
    task = make_task(task_id="task-1")
    await manager.create_task(task)
    run = make_run(
        task,
        schedule_run_id="schedule-run-1",
        message_id="schedule-run:schedule-run-1",
    )
    await manager.materialize_scheduled(candidate=task, run=run)
    before_run, after_run = _handlers(manager)
    message = Message(
        message_id=run.message_id,
        session_id=run.session_id,
        role=Role.USER,
        content=(TextBlock(text=run.prompt_snapshot),),
        origin="schedule",
        run_id="runtime-1",
        created_at=NOW,
        visibility=MessageVisibility.ALL,
    )

    before = await before_run(
        _context(
            point=MiddlewarePoint.BEFORE_RUN,
            payload=RunMiddlewarePayload(
                messages=(message,),
                output_mode=OutputMode.COMPLETE,
            ),
        )
    )
    assert before.type is MiddlewareDecisionType.CONTINUE
    running = (await manager.list_runs(SCOPE, ScheduleRunQuery())).items[0]
    assert running.status is ScheduleRunStatus.RUNNING
    assert running.runtime_run_id == "runtime-1"

    error = RunError(code="tool_failed", message="工具执行失败")
    after = await after_run(
        _context(
            point=MiddlewarePoint.AFTER_RUN,
            payload=RunResultMiddlewarePayload(
                status=SessionStatus.FAILED,
                error=error,
            ),
            status=SessionStatus.FAILED,
        )
    )
    assert after.type is MiddlewareDecisionType.CONTINUE
    failed = (await manager.list_runs(SCOPE, ScheduleRunQuery())).items[0]
    assert failed.status is ScheduleRunStatus.FAILED
    assert failed.error_code == "tool_failed"
    assert failed.error_message == "工具执行失败"


async def test_before_run_ignores_non_schedule_messages() -> None:
    """普通用户消息不访问 ScheduleManager。"""
    manager = LocalScheduleManager(cron=CronSchedule("Asia/Shanghai"))
    before_run, _ = _handlers(manager)
    message = Message(
        message_id="normal",
        session_id="session-1",
        role=Role.USER,
        content=(TextBlock(text="hello"),),
        origin="user",
        run_id="runtime-1",
        created_at=NOW,
        visibility=MessageVisibility.ALL,
    )

    decision = await before_run(
        _context(
            point=MiddlewarePoint.BEFORE_RUN,
            payload=RunMiddlewarePayload(
                messages=(message,),
                output_mode=OutputMode.COMPLETE,
            ),
        )
    )

    assert decision.type is MiddlewareDecisionType.CONTINUE
