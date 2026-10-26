"""注册并实现 ScheduleRun 执行状态追踪 Middleware。"""

from typing import cast

from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
    MiddlewareScope,
    RunMiddlewarePayload,
    RunResultMiddlewarePayload,
)
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.runtime.state import AgentState, SessionStatus
from agentsystem.schedule.models import ScheduleRunStatus
from agentsystem.schedule.service import ScheduleService

_MESSAGE_PREFIX = "schedule-run:"


class _ScheduleRunTracker:
    """将 Runtime 开始和终态投影到 ScheduleRun 的 Middleware 处理器。

    共享 ScheduleService，依据初始消息定位执行实例；开始追踪失败
    阻止执行，终态追踪注册为 fail-open。自身不缓存 Run 或后台补偿。
    """

    def __init__(self, service: ScheduleService) -> None:
        """绑定 Application 级 ScheduleService。

        Args:
            service: Application 装配的共享业务服务。
        """
        self._service = service

    async def before_run(self, context: MiddlewareContext) -> MiddlewareDecision:
        """在 LLM/Tool 之前批量绑定全部 Schedule 初始消息。

        Args:
            context: 当前 Middleware 调用及事件载荷。

        Returns:
            完成状态追踪后继续执行的决策。
        """
        payload = context.payload
        if not isinstance(payload, RunMiddlewarePayload):
            raise ValueError("BEFORE_RUN payload 类型错误")
        run_ids = tuple(
            message.message_id.removeprefix(_MESSAGE_PREFIX)
            for message in payload.messages
            if message.origin == "schedule"
            and message.message_id.startswith(_MESSAGE_PREFIX)
        )
        if run_ids:
            # MiddlewareContext guarantees these fields at run lifecycle hooks.
            started_at = cast(AgentState, context.state_snapshot).started_at
            if started_at is None:
                raise ValueError("BEFORE_RUN 缺少 started_at")
            await self._service.mark_runs_running(
                run_ids,
                runtime_run_id=cast(str, context.run_id),
                started_at=started_at,
            )
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    async def after_run(self, context: MiddlewareContext) -> MiddlewareDecision:
        """在 Runtime 终态保存后批量收敛 ScheduleRun。

        Args:
            context: 当前 Middleware 调用及事件载荷。

        Returns:
            完成状态追踪后继续执行的决策。
        """
        payload = context.payload
        if not isinstance(payload, RunResultMiddlewarePayload):
            raise ValueError("AFTER_RUN payload 类型错误")
        completed_at = cast(AgentState, context.state_snapshot).ended_at
        if completed_at is None:
            raise ValueError("AFTER_RUN 缺少 ended_at")
        error = payload.error
        await self._service.mark_runs_terminal(
            cast(str, context.run_id),
            status=_terminal_status(payload.status),
            completed_at=completed_at,
            final_message_id=(payload.message.message_id if payload.message else None),
            error_code=error.code if error else None,
            error_message=error.message if error else None,
        )
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)


def _terminal_status(status: SessionStatus) -> ScheduleRunStatus:
    mapping = {
        SessionStatus.SUCCEEDED: ScheduleRunStatus.SUCCEEDED,
        SessionStatus.FAILED: ScheduleRunStatus.FAILED,
        SessionStatus.CANCELED: ScheduleRunStatus.CANCELED,
    }
    try:
        return mapping[status]
    except KeyError as error:
        raise ValueError("AFTER_RUN 只允许 Runtime 终态") from error


def register(context: PluginSetupContext) -> None:
    """Scheduler 启用时注册开始与终态追踪 Middleware。

    Args:
        context: 插件装配上下文，提供依赖查询及注册入口。
    """
    service = context.get(ScheduleService)
    if service is None:
        return
    tracker = _ScheduleRunTracker(service)
    context.register_middleware(
        tracker.before_run,
        name="schedule-run-start-tracker",
        handler_id="run-start-tracker",
        point=MiddlewarePoint.BEFORE_RUN,
        scope=MiddlewareScope.GLOBAL,
        failure_mode=MiddlewareFailureMode.FAIL_CLOSED,
    )
    context.register_middleware(
        tracker.after_run,
        name="schedule-run-terminal-tracker",
        handler_id="run-terminal-tracker",
        point=MiddlewarePoint.AFTER_RUN,
        scope=MiddlewareScope.GLOBAL,
        failure_mode=MiddlewareFailureMode.FAIL_OPEN,
    )


__all__ = ["register"]
