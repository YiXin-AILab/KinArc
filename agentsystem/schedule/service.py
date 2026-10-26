"""定时任务 CRUD、历史、立即执行与共用投递服务。"""

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, ValidationError

from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import SessionContext, StrictModel
from agentsystem.inbox.models import InboundMessage, InboundMode
from agentsystem.inbox.protocol import Inbox
from agentsystem.message.models import TextBlock
from agentsystem.schedule.manager import CronMatcher, ScheduleManager
from agentsystem.schedule.models import (
    DispatchResult,
    ManualRunResult,
    ScheduleRun,
    ScheduleRunCursor,
    ScheduleRunQuery,
    ScheduleRunStatus,
    ScheduleScope,
    ScheduleSessionMode,
    ScheduleTask,
    ScheduleTaskCursor,
    ScheduleTaskPatch,
    ScheduleTaskQuery,
    ScheduleTaskStatus,
    ScheduleTaskType,
    ScheduleTriggerKind,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduleTaskListResult:
    """Tool 可直接输出的 Task 列表。

    Attributes:
        items: 当前页任务。
        cursor: 已编码的下一页游标，无后续页时为 None。
    """

    items: tuple[ScheduleTask, ...]
    cursor: str | None


@dataclass(frozen=True, slots=True)
class ScheduleRunListResult:
    """Tool 可直接输出的 Run 历史。

    Attributes:
        items: 当前页执行历史。
        cursor: 已编码的下一页游标，无后续页时为 None。
    """

    items: tuple[ScheduleRun, ...]
    cursor: str | None


class _TaskCursorPayload(StrictModel):
    """Task 分页令牌的严格序列化载荷，携带版本、类型与排序边界。"""

    v: Literal[1]
    kind: Literal["task"]
    updated_at: AwareDatetime
    task_id: str


class _RunCursorPayload(StrictModel):
    """Run 分页令牌的严格序列化载荷，防止与 Task 游标混用。"""

    v: Literal[1]
    kind: Literal["run"]
    created_at: AwareDatetime
    schedule_run_id: str


class ScheduleService:
    """协调定时任务业务规则、权威存储和一次 Inbox 投递。

    从可信上下文绑定应用与 Agent 范围，依赖注入的 Manager、Cron、
    时钟及 ID 工厂；工具游标在此编码和校验。资源由 Application
    拥有，服务不启动扫描器、不执行 Agent，也不自动重投未知投递结果。
    """

    def __init__(
        self,
        *,
        manager: ScheduleManager,
        cron: CronMatcher,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        queue: Inbox,
        operation_timeout_seconds: float,
    ) -> None:
        """初始化可确定性测试的业务服务。

        Args:
            manager: 唯一权威 ScheduleManager。
            cron: 部署时区下的 Cron 校验与匹配器。
            clock: 可注入的当前时间函数。
            id_factory: 生成 Task、Run 或独立会话 ID 的工厂。
            queue: 接收普通运行输入的现有 Inbox。
            operation_timeout_seconds: 外部操作的有限超时秒数。
        """
        self._manager = manager
        self._cron = cron
        self._clock = clock
        self._id_factory = id_factory
        self._queue = queue
        self._operation_timeout_seconds = operation_timeout_seconds

    async def create_task(
        self,
        ctx: SessionContext,
        *,
        name: str,
        prompt: str,
        cron_expression: str,
        task_type: ScheduleTaskType = ScheduleTaskType.RECURRING,
        session_mode: ScheduleSessionMode = ScheduleSessionMode.FIXED,
    ) -> ScheduleTask:
        """使用当前 Tool 上下文创建 Task。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            name: 供用户识别的任务名称。
            prompt: 触发时作为普通输入投递的文本。
            cron_expression: 部署时区下的标准五段 Cron 表达式。
            task_type: 周期或首次自动物化后完成的一次性任务。
            session_mode: 固定会话或每次独立会话的策略。

        Returns:
            操作完成后的任务事实。
        """
        _validate_cron(self._cron, cron_expression)
        now = _utc(self._clock())
        task = ScheduleTask(
            task_id=self._id_factory(),
            app_id=ctx.app_id,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            name=name,
            prompt=prompt,
            cron_expression=cron_expression,
            task_type=task_type,
            session_mode=session_mode,
            fixed_session_id=(
                ctx.session_id if session_mode is ScheduleSessionMode.FIXED else None
            ),
            status=ScheduleTaskStatus.ENABLED,
            version=1,
            created_at=now,
            updated_at=now,
        )
        return await self._manager.create_task(task)

    async def list_tasks(
        self,
        ctx: SessionContext,
        *,
        status: ScheduleTaskStatus | None = None,
        task_type: ScheduleTaskType | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ScheduleTaskListResult:
        """查询当前 app + user + agent 的 Task。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            status: 任务状态筛选或修改值，None 表示未指定。
            task_type: 周期或首次自动物化后完成的一次性任务。
            cursor: 上一页返回的分页边界，None 表示首页。
            limit: 本次最多返回的记录数量。

        Returns:
            任务页和编码后的下一页游标。
        """
        page = await self._manager.list_tasks(
            _scope(ctx),
            ScheduleTaskQuery(
                status=status,
                task_type=task_type,
                cursor=_decode_task_cursor(cursor),
                limit=limit,
            ),
        )
        return ScheduleTaskListResult(
            items=page.items,
            cursor=_encode_task_cursor(page.next_cursor),
        )

    async def update_task(
        self,
        ctx: SessionContext,
        *,
        task_id: str,
        expected_version: int,
        patch: ScheduleTaskPatch,
    ) -> ScheduleTask:
        """修改未来 occurrence 使用的 Task 配置。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            task_id: 目标定时任务 ID。
            expected_version: 调用方读取的任务版本，用于拒绝过期修改。
            patch: 任务修改内容，只影响未来物化的执行实例。

        Returns:
            操作完成后的任务事实。
        """
        if patch.cron_expression is not None:
            _validate_cron(self._cron, patch.cron_expression)
        return await self._manager.update_task(
            _scope(ctx),
            task_id,
            expected_version=expected_version,
            patch=patch,
            updated_at=_utc(self._clock()),
            fixed_session_id=(
                ctx.session_id
                if patch.session_mode is ScheduleSessionMode.FIXED
                else None
            ),
        )

    async def delete_task(
        self,
        ctx: SessionContext,
        *,
        task_id: str,
        expected_version: int,
    ) -> ScheduleTask:
        """软删除 Task 但保留历史。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            task_id: 目标定时任务 ID。
            expected_version: 调用方读取的任务版本，用于拒绝过期修改。

        Returns:
            操作完成后的任务事实。
        """
        return await self._manager.delete_task(
            _scope(ctx),
            task_id,
            expected_version=expected_version,
            deleted_at=_utc(self._clock()),
        )

    async def history(
        self,
        ctx: SessionContext,
        *,
        task_id: str | None = None,
        status: ScheduleRunStatus | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ScheduleRunListResult:
        """查询当前 app + user + agent 的 Run 历史。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            task_id: 目标定时任务 ID。
            status: 执行状态筛选，None 表示不筛选。
            cursor: 上一页返回的分页边界，None 表示首页。
            limit: 本次最多返回的记录数量。

        Returns:
            执行历史页和编码后的下一页游标。
        """
        page = await self._manager.list_runs(
            _scope(ctx),
            ScheduleRunQuery(
                task_id=task_id,
                status=status,
                cursor=_decode_run_cursor(cursor),
                limit=limit,
            ),
        )
        return ScheduleRunListResult(
            items=page.items,
            cursor=_encode_run_cursor(page.next_cursor),
        )

    async def run_now(
        self,
        ctx: SessionContext,
        *,
        task_id: str,
        invocation_key: str,
    ) -> ManualRunResult:
        """物化并尝试投递一次手动 Run。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            task_id: 目标定时任务 ID。
            invocation_key: 手动调用的稳定去重标识。

        Returns:
            唯一手动执行实例及投递或去重结果。
        """
        task = await self._manager.get_task(_scope(ctx), task_id)
        if task is None:
            raise AdapterError(
                operation="schedule.run_now",
                code="schedule_task_not_found",
                message="定时任务不存在",
            )
        run = self._manual_run(task, invocation_key)
        result = await self._manager.materialize_manual(task=task, run=run)
        if not result.was_created:
            return ManualRunResult(
                run=result.run,
                dispatch_result=DispatchResult.ALREADY_MATERIALIZED,
            )
        dispatch_result = await self.dispatch_run(result.run)
        return ManualRunResult(run=result.run, dispatch_result=dispatch_result)

    async def dispatch_run(self, run: ScheduleRun) -> DispatchResult:
        """对已物化 Run 进行唯一次 MessageQueue push。

        Args:
            run: 执行实例及物化时的身份、输入快照。

        Returns:
            本次投递结果，失败不会直接标记 Runtime 执行失败。
        """
        ctx = SessionContext(
            app_id=run.app_id_snapshot,
            user_id=run.user_id_snapshot,
            agent_id=run.agent_id_snapshot,
            session_id=run.session_id,
        )
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                await self._queue.push(ctx, _schedule_message(run))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_unexpected_exception(
                logger,
                "schedule_dispatch_failed",
                error,
                operation="schedule.message_queue.push",
                schedule_run_id=run.schedule_run_id,
            )
            await self._record_dispatch_failure_best_effort(run.schedule_run_id)
            return DispatchResult.FAILED
        return DispatchResult.ACCEPTED

    async def mark_runs_running(
        self,
        schedule_run_ids: tuple[str, ...],
        *,
        runtime_run_id: str,
        started_at: datetime,
    ) -> None:
        """由 Runtime Middleware 批量标记 ScheduleRun 开始执行。

        Args:
            schedule_run_ids: 需要绑定到同一 Runtime run 的执行实例 ID。
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            started_at: Runtime 实际开始时间。
        """
        await self._manager.mark_runs_running(
            schedule_run_ids,
            runtime_run_id=runtime_run_id,
            started_at=started_at,
        )

    async def mark_runs_terminal(
        self,
        runtime_run_id: str,
        *,
        status: ScheduleRunStatus,
        completed_at: datetime,
        final_message_id: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """由 Runtime Middleware 批量收敛 ScheduleRun 终态。

        Args:
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            status: 目标 Runtime 终态，只允许成功、失败或取消。
            completed_at: Runtime 终态时间。
            final_message_id: 最终回复消息 ID，无回复时为 None。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。
        """
        await self._manager.mark_runs_terminal(
            runtime_run_id,
            status=status,
            completed_at=completed_at,
            final_message_id=final_message_id,
            error_code=error_code,
            error_message=error_message,
        )

    def _manual_run(self, task: ScheduleTask, invocation_key: str) -> ScheduleRun:
        now = _utc(self._clock())
        run_id = self._id_factory()
        session_id = (
            task.fixed_session_id
            if task.session_mode is ScheduleSessionMode.FIXED
            else self._id_factory()
        )
        if session_id is None:
            raise ValueError("fixed 任务缺少会话")
        return ScheduleRun(
            schedule_run_id=run_id,
            task_id=task.task_id,
            occurrence_key=f"manual:{invocation_key}",
            trigger_kind=ScheduleTriggerKind.MANUAL,
            scheduled_for=now,
            task_name_snapshot=task.name,
            app_id_snapshot=task.app_id,
            user_id_snapshot=task.user_id,
            agent_id_snapshot=task.agent_id,
            prompt_snapshot=task.prompt,
            session_id=session_id,
            message_id=f"schedule-run:{run_id}",
            status=ScheduleRunStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

    async def _record_dispatch_failure_best_effort(
        self,
        schedule_run_id: str,
    ) -> None:
        """有限等待诊断写入；诊断失败在此记录一次，取消继续传播。"""
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                await self._manager.record_dispatch_failure(
                    schedule_run_id,
                    failed_at=_utc(self._clock()),
                    error_code="schedule_dispatch_failed",
                    error_message="定时任务消息投递失败",
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_unexpected_exception(
                logger,
                "schedule_dispatch_diagnostic_failed",
                error,
                operation="schedule.record_dispatch_failure",
                schedule_run_id=schedule_run_id,
            )


def _scope(ctx: SessionContext) -> ScheduleScope:
    return ScheduleScope(app_id=ctx.app_id, user_id=ctx.user_id, agent_id=ctx.agent_id)


def _validate_cron(cron: CronMatcher, expression: str) -> None:
    try:
        cron.validate(expression)
    except ValueError as error:
        raise ToolError(
            code="invalid_cron_expression",
            message=("只允许标准五段 Cron，字段顺序为：分钟 小时 日 月 星期"),
        ) from error


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("定时任务时间必须包含时区")
    return value.astimezone(UTC)


def _schedule_message(run: ScheduleRun) -> InboundMessage:
    return InboundMessage(
        message_id=run.message_id,
        session_id=run.session_id,
        content=(TextBlock(text=run.prompt_snapshot),),
        origin="schedule",
        mode=InboundMode.QUEUE,
        created_at=run.created_at,
    )


def _encode_task_cursor(cursor: ScheduleTaskCursor | None) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor(
        _TaskCursorPayload(
            v=1,
            kind="task",
            updated_at=cursor.updated_at,
            task_id=cursor.task_id,
        )
    )


def _encode_run_cursor(cursor: ScheduleRunCursor | None) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor(
        _RunCursorPayload(
            v=1,
            kind="run",
            created_at=cursor.created_at,
            schedule_run_id=cursor.schedule_run_id,
        )
    )


def _encode_cursor(payload: StrictModel) -> str:
    raw = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_task_cursor(value: str | None) -> ScheduleTaskCursor | None:
    if value is None:
        return None
    try:
        payload = _TaskCursorPayload.model_validate(_decode_cursor(value))
    except (
        ValueError,
        ValidationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise _cursor_error() from error
    return ScheduleTaskCursor(
        updated_at=payload.updated_at,
        task_id=payload.task_id,
    )


def _decode_run_cursor(value: str | None) -> ScheduleRunCursor | None:
    if value is None:
        return None
    try:
        payload = _RunCursorPayload.model_validate(_decode_cursor(value))
    except (
        ValueError,
        ValidationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise _cursor_error() from error
    return ScheduleRunCursor(
        created_at=payload.created_at,
        schedule_run_id=payload.schedule_run_id,
    )


def _decode_cursor(value: str) -> object:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise _cursor_error() from error
    return json.loads(raw.decode())


def _cursor_error() -> ToolError:
    return ToolError(
        code="schedule_cursor_invalid",
        message="定时任务分页游标无效",
    )


__all__ = ["ScheduleRunListResult", "ScheduleService", "ScheduleTaskListResult"]
