"""单进程 LocalScheduleManager 实现。"""

import asyncio
from datetime import datetime

from agentsystem.common.errors import AdapterError
from agentsystem.schedule.manager import CronMatcher
from agentsystem.schedule.models import (
    MaterializeResult,
    ScheduleRun,
    ScheduleRunCursor,
    ScheduleRunPage,
    ScheduleRunQuery,
    ScheduleRunStatus,
    ScheduleScope,
    ScheduleSessionMode,
    ScheduleTask,
    ScheduleTaskCursor,
    ScheduleTaskPage,
    ScheduleTaskPatch,
    ScheduleTaskQuery,
    ScheduleTaskStatus,
    ScheduleTaskType,
    ScheduleTriggerKind,
)


class LocalScheduleManager:
    """用实例内存和 asyncio 单锁实现 ScheduleManager。

    依赖 CronMatcher 复核自动触发；任务、执行实例和唯一性索引在
    同一锁内变更。只适用于单进程事件循环，实例销毁后事实和索引丢失；
    不拥有外部连接或消息投递职责。
    """

    def __init__(self, *, cron: CronMatcher) -> None:
        """初始化无外部依赖的存储实例。

        Args:
            cron: 用于物化前复核当前分钟的 Cron 匹配器。
        """
        self._cron = cron
        self._lock = asyncio.Lock()
        self._tasks: dict[str, ScheduleTask] = {}
        self._runs: dict[str, ScheduleRun] = {}
        self._occurrences: dict[tuple[str, str], str] = {}
        self._message_ids: dict[str, str] = {}

    async def create_task(self, task: ScheduleTask) -> ScheduleTask:
        """创建唯一 Task。

        Args:
            task: 完整的任务事实。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务 ID 已存在或存储操作失败。
        """
        async with self._lock:
            if task.task_id in self._tasks:
                raise _error(
                    "schedule.create_task",
                    "schedule_task_conflict",
                    "定时任务已存在",
                )
            self._tasks[task.task_id] = task
            return task

    async def get_task(
        self,
        scope: ScheduleScope,
        task_id: str,
    ) -> ScheduleTask | None:
        """在当前作用域读取未删除 Task。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。

        Returns:
            范围内未删除的任务；不存在、已删除或越界时为 None。
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if (
                task is None
                or task.deleted_at is not None
                or not _in_scope(task, scope)
            ):
                return None
            return task

    async def list_tasks(
        self,
        scope: ScheduleScope,
        query: ScheduleTaskQuery,
    ) -> ScheduleTaskPage:
        """以稳定键集游标分页查询 Task。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按修改时间与 ID 降序排列的任务页及下一页边界。
        """
        async with self._lock:
            tasks = [
                task
                for task in self._tasks.values()
                if task.deleted_at is None
                and _in_scope(task, scope)
                and (query.status is None or task.status is query.status)
                and (query.task_type is None or task.task_type is query.task_type)
                and _before_task_cursor(task, query.cursor)
            ]
            tasks.sort(key=lambda item: (item.updated_at, item.task_id), reverse=True)
            page = tasks[: query.limit]
            next_cursor = None
            if len(tasks) > query.limit:
                last = page[-1]
                next_cursor = ScheduleTaskCursor(
                    updated_at=last.updated_at,
                    task_id=last.task_id,
                )
            return ScheduleTaskPage(items=tuple(page), next_cursor=next_cursor)

    async def scan_enabled_tasks(
        self,
        *,
        after_task_id: str | None,
        limit: int,
    ) -> tuple[ScheduleTask, ...]:
        """按 ID 升序返回 enabled Task 扫描页。

        Args:
            after_task_id: 上一批最后一个 Task ID，None 表示从头扫描。
            limit: 本次最多返回的记录数量。

        Returns:
            按 Task ID 升序排列的启用任务批次，空元组表示扫描结束。
        """
        async with self._lock:
            tasks = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.deleted_at is None
                    and task.status is ScheduleTaskStatus.ENABLED
                    and (after_task_id is None or task.task_id > after_task_id)
                ),
                key=lambda item: item.task_id,
            )
            return tuple(tasks[:limit])

    async def update_task(
        self,
        scope: ScheduleScope,
        task_id: str,
        *,
        expected_version: int,
        patch: ScheduleTaskPatch,
        updated_at: datetime,
        fixed_session_id: str | None,
    ) -> ScheduleTask:
        """通过 version CAS 修改 Task。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。
            expected_version: 调用方读取的任务版本，用于拒绝过期修改。
            patch: 任务修改内容，只影响未来物化的执行实例。
            updated_at: 本次修改时间。
            fixed_session_id: 切换固定模式时绑定的会话 ID。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务不存在、版本冲突或存储操作失败。
        """
        async with self._lock:
            current = self._task_for_mutation(scope, task_id, "schedule.update_task")
            _require_version(current, expected_version, "schedule.update_task")
            changes = patch.model_dump(exclude_unset=True)
            target_mode = changes.get("session_mode", current.session_mode)
            if target_mode is ScheduleSessionMode.FIXED:
                changes["fixed_session_id"] = (
                    fixed_session_id
                    if "session_mode" in changes
                    else current.fixed_session_id
                )
            else:
                changes["fixed_session_id"] = None
            changes["version"] = current.version + 1
            changes["updated_at"] = updated_at
            updated = current.model_copy(update=changes)
            updated = ScheduleTask.model_validate(updated.model_dump())
            self._tasks[task_id] = updated
            return updated

    async def delete_task(
        self,
        scope: ScheduleScope,
        task_id: str,
        *,
        expected_version: int,
        deleted_at: datetime,
    ) -> ScheduleTask:
        """通过 version CAS 软删除 Task。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。
            expected_version: 调用方读取的任务版本，用于拒绝过期修改。
            deleted_at: 本次软删除时间。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务不存在、版本冲突或存储操作失败。
        """
        async with self._lock:
            current = self._task_for_mutation(scope, task_id, "schedule.delete_task")
            _require_version(current, expected_version, "schedule.delete_task")
            deleted = current.model_copy(
                update={
                    "status": ScheduleTaskStatus.DISABLED,
                    "version": current.version + 1,
                    "updated_at": deleted_at,
                    "deleted_at": deleted_at,
                }
            )
            self._tasks[task_id] = deleted
            return deleted

    async def materialize_scheduled(
        self,
        *,
        candidate: ScheduleTask,
        run: ScheduleRun,
    ) -> MaterializeResult:
        """原子物化自动 occurrence。

        Args:
            candidate: 扫描时的 Task 快照，物化时需复核版本和状态。
            run: 执行实例及物化时的身份、输入快照。

        Returns:
            新建或既有的唯一实例，以及本调用是否拥有首次投递资格。

        Raises:
            AdapterError: 当前任务不再满足物化条件、唯一性冲突或存储失败。
        """
        self._cron.validate(candidate.cron_expression)
        if not self._cron.matches(candidate.cron_expression, run.scheduled_for):
            raise _state_error("schedule.materialize_scheduled")
        async with self._lock:
            key = (candidate.task_id, run.occurrence_key)
            existing_id = self._occurrences.get(key)
            if existing_id is not None:
                return MaterializeResult(
                    run=self._runs[existing_id],
                    was_created=False,
                )
            current = self._tasks.get(candidate.task_id)
            self._validate_scheduled_candidate(current, candidate, run)
            self._insert_run(run)
            if candidate.task_type is ScheduleTaskType.ONE_TIME:
                self._tasks[candidate.task_id] = candidate.model_copy(
                    update={
                        "status": ScheduleTaskStatus.COMPLETED,
                        "version": candidate.version + 1,
                        "updated_at": run.created_at,
                    }
                )
            return MaterializeResult(run=run, was_created=True)

    async def materialize_manual(
        self,
        *,
        task: ScheduleTask,
        run: ScheduleRun,
    ) -> MaterializeResult:
        """原子物化手动 occurrence。

        Args:
            task: 完整的任务事实。
            run: 执行实例及物化时的身份、输入快照。

        Returns:
            新建或既有的唯一实例，以及本调用是否拥有首次投递资格。

        Raises:
            AdapterError: 任务已删除、实例状态冲突或存储失败。
        """
        async with self._lock:
            key = (task.task_id, run.occurrence_key)
            existing_id = self._occurrences.get(key)
            if existing_id is not None:
                return MaterializeResult(
                    run=self._runs[existing_id],
                    was_created=False,
                )
            current = self._tasks.get(task.task_id)
            if current is None or current.deleted_at is not None:
                raise _state_error("schedule.materialize_manual")
            if (
                current.app_id != task.app_id
                or current.user_id != task.user_id
                or current.agent_id != task.agent_id
            ):
                raise _state_error("schedule.materialize_manual")
            if run.trigger_kind is not ScheduleTriggerKind.MANUAL:
                raise _state_error("schedule.materialize_manual")
            self._insert_run(run)
            return MaterializeResult(run=run, was_created=True)

    async def record_dispatch_failure(
        self,
        schedule_run_id: str,
        *,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        """在不改变 pending 状态的前提下记录投递诊断。

        Args:
            schedule_run_id: 目标 ScheduleRun ID。
            failed_at: 本次 Inbox 投递失败时间。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。

        Raises:
            AdapterError: 执行实例不存在、不处于 pending 或存储失败。
        """
        async with self._lock:
            run = self._runs.get(schedule_run_id)
            if run is None or run.status is not ScheduleRunStatus.PENDING:
                raise _state_error("schedule.record_dispatch_failure")
            self._runs[schedule_run_id] = run.model_copy(
                update={
                    "dispatch_failed_at": failed_at,
                    "dispatch_error_code": error_code,
                    "dispatch_error_message": error_message,
                    "updated_at": failed_at,
                }
            )

    async def mark_runs_running(
        self,
        schedule_run_ids: tuple[str, ...],
        *,
        runtime_run_id: str,
        started_at: datetime,
    ) -> None:
        """原子批量绑定 Runtime run。

        Args:
            schedule_run_ids: 需要绑定到同一 Runtime run 的执行实例 ID。
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            started_at: Runtime 实际开始时间。

        Raises:
            AdapterError: 实例不存在、状态或 Runtime 绑定冲突，或存储失败。
        """
        async with self._lock:
            runs = self._require_runs(schedule_run_ids, "schedule.mark_runs_running")
            for run in runs:
                if run.status is ScheduleRunStatus.RUNNING:
                    if run.runtime_run_id != runtime_run_id:
                        raise _state_error("schedule.mark_runs_running")
                elif run.status is not ScheduleRunStatus.PENDING:
                    raise _state_error("schedule.mark_runs_running")
            for run in runs:
                if run.status is ScheduleRunStatus.PENDING:
                    self._runs[run.schedule_run_id] = run.model_copy(
                        update={
                            "status": ScheduleRunStatus.RUNNING,
                            "runtime_run_id": runtime_run_id,
                            "started_at": started_at,
                            "dispatch_failed_at": None,
                            "dispatch_error_code": None,
                            "dispatch_error_message": None,
                            "updated_at": started_at,
                        }
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
        """批量收敛同一 Runtime run 关联的所有 ScheduleRun。

        Args:
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            status: 目标 Runtime 终态，只允许成功、失败或取消。
            completed_at: Runtime 终态时间。
            final_message_id: 最终回复消息 ID，无回复时为 None。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。

        Raises:
            AdapterError: 终态无效、状态冲突或存储失败。
        """
        if status not in _TERMINAL_STATUSES:
            raise _state_error("schedule.mark_runs_terminal")
        async with self._lock:
            runs = [
                run
                for run in self._runs.values()
                if run.runtime_run_id == runtime_run_id
            ]
            for run in runs:
                if run.status in _TERMINAL_STATUSES:
                    if run.status is not status:
                        raise _state_error("schedule.mark_runs_terminal")
                elif run.status is not ScheduleRunStatus.RUNNING:
                    raise _state_error("schedule.mark_runs_terminal")
            for run in runs:
                if run.status is ScheduleRunStatus.RUNNING:
                    updated = run.model_copy(
                        update={
                            "status": status,
                            "completed_at": completed_at,
                            "final_message_id": final_message_id,
                            "error_code": error_code,
                            "error_message": error_message,
                            "updated_at": completed_at,
                        }
                    )
                    self._runs[run.schedule_run_id] = ScheduleRun.model_validate(
                        updated.model_dump()
                    )

    async def list_runs(
        self,
        scope: ScheduleScope,
        query: ScheduleRunQuery,
    ) -> ScheduleRunPage:
        """以稳定键集游标分页查询 Run 历史。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按创建时间与 ID 降序排列的历史页及下一页边界。
        """
        async with self._lock:
            runs = [
                run
                for run in self._runs.values()
                if run.app_id_snapshot == scope.app_id
                and run.user_id_snapshot == scope.user_id
                and run.agent_id_snapshot == scope.agent_id
                and (query.task_id is None or run.task_id == query.task_id)
                and (query.status is None or run.status is query.status)
                and _before_run_cursor(run, query.cursor)
            ]
            runs.sort(
                key=lambda item: (item.created_at, item.schedule_run_id),
                reverse=True,
            )
            page = runs[: query.limit]
            next_cursor = None
            if len(runs) > query.limit:
                last = page[-1]
                next_cursor = ScheduleRunCursor(
                    created_at=last.created_at,
                    schedule_run_id=last.schedule_run_id,
                )
            return ScheduleRunPage(items=tuple(page), next_cursor=next_cursor)

    def _task_for_mutation(
        self,
        scope: ScheduleScope,
        task_id: str,
        operation: str,
    ) -> ScheduleTask:
        current = self._tasks.get(task_id)
        if (
            current is None
            or current.deleted_at is not None
            or not _in_scope(current, scope)
        ):
            raise _error(operation, "schedule_task_not_found", "定时任务不存在")
        return current

    def _validate_scheduled_candidate(
        self,
        current: ScheduleTask | None,
        candidate: ScheduleTask,
        run: ScheduleRun,
    ) -> None:
        if (
            current is None
            or current.deleted_at is not None
            or current.status is not ScheduleTaskStatus.ENABLED
            or current.version != candidate.version
            or current.app_id != candidate.app_id
            or current.user_id != candidate.user_id
            or current.agent_id != candidate.agent_id
            or current.cron_expression != candidate.cron_expression
            or run.trigger_kind is not ScheduleTriggerKind.SCHEDULED
        ):
            raise _state_error("schedule.materialize_scheduled")

    def _insert_run(self, run: ScheduleRun) -> None:
        key = (run.task_id, run.occurrence_key)
        if run.schedule_run_id in self._runs or run.message_id in self._message_ids:
            raise _error(
                "schedule.materialize",
                "schedule_run_conflict",
                "定时任务执行实例已存在",
            )
        self._runs[run.schedule_run_id] = run
        self._occurrences[key] = run.schedule_run_id
        self._message_ids[run.message_id] = run.schedule_run_id

    def _require_runs(
        self,
        run_ids: tuple[str, ...],
        operation: str,
    ) -> tuple[ScheduleRun, ...]:
        runs = tuple(self._runs[run_id] for run_id in run_ids if run_id in self._runs)
        if len(runs) != len(run_ids):
            raise _state_error(operation)
        return runs


_TERMINAL_STATUSES = {
    ScheduleRunStatus.SUCCEEDED,
    ScheduleRunStatus.FAILED,
    ScheduleRunStatus.CANCELED,
}


def _in_scope(task: ScheduleTask, scope: ScheduleScope) -> bool:
    return (
        task.app_id == scope.app_id
        and task.user_id == scope.user_id
        and task.agent_id == scope.agent_id
    )


def _before_task_cursor(
    task: ScheduleTask,
    cursor: ScheduleTaskCursor | None,
) -> bool:
    return cursor is None or (task.updated_at, task.task_id) < (
        cursor.updated_at,
        cursor.task_id,
    )


def _before_run_cursor(run: ScheduleRun, cursor: ScheduleRunCursor | None) -> bool:
    return cursor is None or (run.created_at, run.schedule_run_id) < (
        cursor.created_at,
        cursor.schedule_run_id,
    )


def _require_version(task: ScheduleTask, expected: int, operation: str) -> None:
    if task.version != expected:
        raise _error(
            operation,
            "schedule_version_conflict",
            "定时任务版本冲突",
        )


def _state_error(operation: str) -> AdapterError:
    return _error(operation, "schedule_state_conflict", "定时任务状态冲突")


def _error(operation: str, code: str, message: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code=code,
        message=message,
    )


__all__ = ["LocalScheduleManager"]
