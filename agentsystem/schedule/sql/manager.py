"""基于 SQLAlchemy Core AsyncEngine 的 ScheduleManager。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import ColumnElement, RowMapping, and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
from agentsystem.schedule.sql.keys import occurrence_key
from agentsystem.schedule.sql.schema import schedule_runs, schedule_tasks

_TERMINAL = {
    ScheduleRunStatus.SUCCEEDED,
    ScheduleRunStatus.FAILED,
    ScheduleRunStatus.CANCELED,
}


class SqlScheduleManager:
    """通过 SQLAlchemy Core 事务保存 Task、Run 和唯一性索引。

    复用调用方 AsyncEngine，以行锁和版本校验保护任务修改；
    运行实例由唯一约束去重。依赖注入的 CronMatcher，不创建表、
    迁移或关闭连接池；有限操作超时和数据库错误在 adapter 边界转换。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        cron: CronMatcher,
        operation_timeout_seconds: float,
    ) -> None:
        """绑定共享 Engine、Cron 匹配器和有限操作超时。

        Args:
            engine: Application 创建和关闭的共享 AsyncEngine。
            cron: 部署时区下的 Cron 校验与匹配器。
            operation_timeout_seconds: 外部操作的有限超时秒数。
        """
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds 必须大于 0")
        self._engine = engine
        self._cron = cron
        self._timeout = operation_timeout_seconds

    async def create_task(self, task: ScheduleTask) -> ScheduleTask:
        """创建唯一 Task。

        Args:
            task: 完整的任务事实。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务 ID 已存在或存储操作失败。
        """

        async def operation(connection: AsyncConnection) -> ScheduleTask:
            """在保存点内插入唯一任务，将重复键映射为任务冲突。"""
            savepoint = await connection.begin_nested()
            try:
                await connection.execute(
                    insert(schedule_tasks).values(**_task_values(task))
                )
                await savepoint.commit()
            except IntegrityError as error:
                await savepoint.rollback()
                raise _error(
                    "schedule.create_task",
                    "schedule_task_conflict",
                    "定时任务已存在",
                ) from error
            return task

        return await self._write("schedule.create_task", operation)

    async def get_task(self, scope: ScheduleScope, task_id: str) -> ScheduleTask | None:
        """在当前作用域读取未删除 Task。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。

        Returns:
            范围内未删除的任务；不存在、已删除或越界时为 None。
        """

        async def operation(connection: AsyncConnection) -> ScheduleTask | None:
            """读取任务并过滤已删除或不属于查询范围的记录。"""
            task = await self._read_task(connection, task_id, scope=scope)
            if (
                task is None
                or task.deleted_at is not None
                or not _in_scope(task, scope)
            ):
                return None
            return task

        return await self._read("schedule.get_task", operation)

    async def list_tasks(
        self, scope: ScheduleScope, query: ScheduleTaskQuery
    ) -> ScheduleTaskPage:
        """以稳定键集游标分页查询 Task。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按修改时间与 ID 降序排列的任务页及下一页边界。
        """

        async def operation(connection: AsyncConnection) -> ScheduleTaskPage:
            """筛选范围内未删除任务，按稳定降序边界截取分页。"""
            statement = select(schedule_tasks).where(
                *_owner_conditions(scope), schedule_tasks.c.deleted_at.is_(None)
            )
            if query.status is not None:
                statement = statement.where(
                    schedule_tasks.c.status == query.status.value
                )
            if query.task_type is not None:
                statement = statement.where(
                    schedule_tasks.c.task_type == query.task_type.value
                )
            if query.cursor is not None:
                time = _sql_time(query.cursor.updated_at)
                statement = statement.where(
                    or_(
                        schedule_tasks.c.updated_at < time,
                        and_(
                            schedule_tasks.c.updated_at == time,
                            schedule_tasks.c.task_id < query.cursor.task_id,
                        ),
                    )
                )
            rows = (
                await connection.execute(
                    statement.order_by(
                        schedule_tasks.c.updated_at.desc(),
                        schedule_tasks.c.task_id.desc(),
                    ).limit(query.limit + 1)
                )
            ).mappings()
            tasks = [_decode_task(row) for row in rows]
            page = tasks[: query.limit]
            cursor = None
            if len(tasks) > query.limit:
                last = page[-1]
                cursor = ScheduleTaskCursor(
                    updated_at=last.updated_at, task_id=last.task_id
                )
            return ScheduleTaskPage(items=tuple(page), next_cursor=cursor)

        return await self._read("schedule.list_tasks", operation)

    async def scan_enabled_tasks(
        self, *, after_task_id: str | None, limit: int
    ) -> tuple[ScheduleTask, ...]:
        """按 Task ID 升序扫描启用任务。

        Args:
            after_task_id: 上一批最后一个 Task ID，None 表示从头扫描。
            limit: 本次最多返回的记录数量。

        Returns:
            按 Task ID 升序排列的启用任务批次，空元组表示扫描结束。
        """

        async def operation(connection: AsyncConnection) -> tuple[ScheduleTask, ...]:
            """读取启用任务并按 Task ID 升序截取扫描批次。"""
            statement = select(schedule_tasks).where(
                schedule_tasks.c.deleted_at.is_(None),
                schedule_tasks.c.status == ScheduleTaskStatus.ENABLED.value,
            )
            if after_task_id is not None:
                statement = statement.where(schedule_tasks.c.task_id > after_task_id)
            rows = (
                await connection.execute(
                    statement.order_by(schedule_tasks.c.task_id.asc()).limit(limit)
                )
            ).mappings()
            return tuple(_decode_task(row) for row in rows)

        return await self._read("schedule.scan_tasks", operation)

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
        """在行锁内通过 version CAS 修改 Task。

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

        async def operation(connection: AsyncConnection) -> ScheduleTask:
            """持有任务行锁校验版本，再保存验证后的完整修改。"""
            current = await self._task_for_mutation(connection, scope, task_id)
            if current.version != expected_version:
                raise _version_error("schedule.update_task")
            changes = patch.model_dump(exclude_unset=True)
            mode = changes.get("session_mode", current.session_mode)
            changes["fixed_session_id"] = (
                fixed_session_id
                if mode is ScheduleSessionMode.FIXED and "session_mode" in changes
                else current.fixed_session_id
                if mode is ScheduleSessionMode.FIXED
                else None
            )
            changes.update(version=current.version + 1, updated_at=updated_at)
            result = ScheduleTask.model_validate(
                current.model_copy(update=changes).model_dump()
            )
            await self._replace_task(connection, result)
            return result

        return await self._write("schedule.update_task", operation)

    async def delete_task(
        self,
        scope: ScheduleScope,
        task_id: str,
        *,
        expected_version: int,
        deleted_at: datetime,
    ) -> ScheduleTask:
        """在行锁内通过 version CAS 软删除 Task。

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

        async def operation(connection: AsyncConnection) -> ScheduleTask:
            """持有任务行锁校验版本，保存停用与软删除事实。"""
            current = await self._task_for_mutation(connection, scope, task_id)
            if current.version != expected_version:
                raise _version_error("schedule.delete_task")
            result = current.model_copy(
                update={
                    "status": ScheduleTaskStatus.DISABLED,
                    "version": current.version + 1,
                    "updated_at": deleted_at,
                    "deleted_at": deleted_at,
                }
            )
            await self._replace_task(connection, result)
            return result

        return await self._write("schedule.delete_task", operation)

    async def materialize_scheduled(
        self, *, candidate: ScheduleTask, run: ScheduleRun
    ) -> MaterializeResult:
        """原子物化自动 occurrence 并终结 one-time Task。

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

        async def operation(connection: AsyncConnection) -> MaterializeResult:
            """锁定任务与 occurrence，去重插入并完成一次性任务。"""
            existing = await self._read_occurrence(connection, run)
            if existing is not None:
                return MaterializeResult(run=existing, was_created=False)
            current = await self._read_task(
                connection, candidate.task_id, for_update=True
            )
            existing = await self._read_occurrence(connection, run, for_update=True)
            if existing is not None:
                return MaterializeResult(run=existing, was_created=False)
            _validate_scheduled(current, candidate, run)
            await connection.execute(insert(schedule_runs).values(**_run_values(run)))
            if candidate.task_type is ScheduleTaskType.ONE_TIME:
                await self._replace_task(
                    connection,
                    candidate.model_copy(
                        update={
                            "status": ScheduleTaskStatus.COMPLETED,
                            "version": candidate.version + 1,
                            "updated_at": run.created_at,
                        }
                    ),
                )
            return MaterializeResult(run=run, was_created=True)

        return await self._write("schedule.materialize_scheduled", operation)

    async def materialize_manual(
        self, *, task: ScheduleTask, run: ScheduleRun
    ) -> MaterializeResult:
        """原子物化手动 occurrence，允许停用任务。

        Args:
            task: 完整的任务事实。
            run: 执行实例及物化时的身份、输入快照。

        Returns:
            新建或既有的唯一实例，以及本调用是否拥有首次投递资格。

        Raises:
            AdapterError: 任务已删除、实例状态冲突或存储失败。
        """

        async def operation(connection: AsyncConnection) -> MaterializeResult:
            """复核未删除任务并唯一插入手动实例，已有实例直接返回。"""
            existing = await self._read_occurrence(connection, run)
            if existing is not None:
                return MaterializeResult(run=existing, was_created=False)
            current = await self._read_task(connection, task.task_id, for_update=True)
            existing = await self._read_occurrence(connection, run, for_update=True)
            if existing is not None:
                return MaterializeResult(run=existing, was_created=False)
            if (
                current is None
                or current.deleted_at is not None
                or current.app_id != task.app_id
                or current.user_id != task.user_id
                or current.agent_id != task.agent_id
                or run.trigger_kind is not ScheduleTriggerKind.MANUAL
            ):
                raise _state_error("schedule.materialize_manual")
            await connection.execute(insert(schedule_runs).values(**_run_values(run)))
            return MaterializeResult(run=run, was_created=True)

        return await self._write("schedule.materialize_manual", operation)

    async def record_dispatch_failure(
        self,
        schedule_run_id: str,
        *,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        """保持 pending 并记录投递失败诊断。

        Args:
            schedule_run_id: 目标 ScheduleRun ID。
            failed_at: 本次 Inbox 投递失败时间。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。

        Raises:
            AdapterError: 执行实例不存在、不处于 pending 或存储失败。
        """

        async def operation(connection: AsyncConnection) -> None:
            """锁定 pending 实例后仅更新投递诊断，保留业务状态。"""
            run = await self._require_run(connection, schedule_run_id, for_update=True)
            if run.status is not ScheduleRunStatus.PENDING:
                raise _state_error("schedule.record_dispatch_failure")
            await self._replace_run(
                connection,
                run.model_copy(
                    update={
                        "dispatch_failed_at": failed_at,
                        "dispatch_error_code": error_code,
                        "dispatch_error_message": error_message,
                        "updated_at": failed_at,
                    }
                ),
            )

        await self._write("schedule.record_dispatch_failure", operation)

    async def mark_runs_running(
        self,
        schedule_run_ids: tuple[str, ...],
        *,
        runtime_run_id: str,
        started_at: datetime,
    ) -> None:
        """原子批量绑定 Runtime run 并进入 running。

        Args:
            schedule_run_ids: 需要绑定到同一 Runtime run 的执行实例 ID。
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            started_at: Runtime 实际开始时间。

        Raises:
            AdapterError: 实例不存在、状态或 Runtime 绑定冲突，或存储失败。
        """

        async def operation(connection: AsyncConnection) -> None:
            """先锁定并验证整批实例，再绑定 Runtime run 并推进状态。"""
            runs = [
                await self._require_run(connection, item, for_update=True)
                for item in schedule_run_ids
            ]
            for run in runs:
                if run.status is ScheduleRunStatus.RUNNING:
                    if run.runtime_run_id != runtime_run_id:
                        raise _state_error("schedule.mark_runs_running")
                elif run.status is not ScheduleRunStatus.PENDING:
                    raise _state_error("schedule.mark_runs_running")
            for run in runs:
                if run.status is ScheduleRunStatus.PENDING:
                    await self._replace_run(
                        connection,
                        run.model_copy(
                            update={
                                "status": ScheduleRunStatus.RUNNING,
                                "runtime_run_id": runtime_run_id,
                                "started_at": started_at,
                                "dispatch_failed_at": None,
                                "dispatch_error_code": None,
                                "dispatch_error_message": None,
                                "updated_at": started_at,
                            }
                        ),
                    )

        await self._write("schedule.mark_runs_running", operation)

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
        """原子批量收敛同一 Runtime run 的 ScheduleRun。

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
        if status not in _TERMINAL:
            raise _state_error("schedule.mark_runs_terminal")

        async def operation(connection: AsyncConnection) -> None:
            """锁定同一 Runtime run 的全部实例，验证后批量保存终态。"""
            rows = (
                await connection.execute(
                    select(schedule_runs)
                    .where(schedule_runs.c.runtime_run_id == runtime_run_id)
                    .with_for_update()
                )
            ).mappings()
            runs = [_decode_run(row) for row in rows]
            for run in runs:
                if run.runtime_run_id != runtime_run_id:
                    raise _corrupt("schedule.mark_runs_terminal")
                if run.status in _TERMINAL:
                    if run.status is not status:
                        raise _state_error("schedule.mark_runs_terminal")
                elif run.status is not ScheduleRunStatus.RUNNING:
                    raise _state_error("schedule.mark_runs_terminal")
            for run in runs:
                if run.status is ScheduleRunStatus.RUNNING:
                    changed = run.model_copy(
                        update={
                            "status": status,
                            "completed_at": completed_at,
                            "final_message_id": final_message_id,
                            "error_code": error_code,
                            "error_message": error_message,
                            "updated_at": completed_at,
                        }
                    )
                    await self._replace_run(
                        connection,
                        ScheduleRun.model_validate(changed.model_dump()),
                    )

        await self._write("schedule.mark_runs_terminal", operation)

    async def list_runs(
        self, scope: ScheduleScope, query: ScheduleRunQuery
    ) -> ScheduleRunPage:
        """以稳定键集游标分页查询执行历史。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按创建时间与 ID 降序排列的历史页及下一页边界。
        """

        async def operation(connection: AsyncConnection) -> ScheduleRunPage:
            """按范围和筛选条件读取历史，并截取稳定键集分页。"""
            statement = select(schedule_runs).where(
                schedule_runs.c.app_id_snapshot == scope.app_id,
                schedule_runs.c.user_id_snapshot == scope.user_id,
                schedule_runs.c.agent_id_snapshot == scope.agent_id,
            )
            if query.task_id is not None:
                statement = statement.where(schedule_runs.c.task_id == query.task_id)
            if query.status is not None:
                statement = statement.where(
                    schedule_runs.c.status == query.status.value
                )
            if query.cursor is not None:
                time = _sql_time(query.cursor.created_at)
                statement = statement.where(
                    or_(
                        schedule_runs.c.created_at < time,
                        and_(
                            schedule_runs.c.created_at == time,
                            schedule_runs.c.schedule_run_id
                            < query.cursor.schedule_run_id,
                        ),
                    )
                )
            rows = (
                await connection.execute(
                    statement.order_by(
                        schedule_runs.c.created_at.desc(),
                        schedule_runs.c.schedule_run_id.desc(),
                    ).limit(query.limit + 1)
                )
            ).mappings()
            runs = [_decode_run(row) for row in rows]
            page = runs[: query.limit]
            cursor = None
            if len(runs) > query.limit:
                last = page[-1]
                cursor = ScheduleRunCursor(
                    created_at=last.created_at,
                    schedule_run_id=last.schedule_run_id,
                )
            return ScheduleRunPage(items=tuple(page), next_cursor=cursor)

        return await self._read("schedule.list_runs", operation)

    async def _task_for_mutation(
        self, connection: AsyncConnection, scope: ScheduleScope, task_id: str
    ) -> ScheduleTask:
        task = await self._read_task(connection, task_id, for_update=True, scope=scope)
        if task is None or task.deleted_at is not None or not _in_scope(task, scope):
            raise _error(
                "schedule.mutate_task",
                "schedule_task_not_found",
                "定时任务不存在",
            )
        return task

    async def _read_task(
        self,
        connection: AsyncConnection,
        task_id: str,
        *,
        for_update: bool = False,
        scope: ScheduleScope | None = None,
    ) -> ScheduleTask | None:
        statement = select(schedule_tasks).where(schedule_tasks.c.task_id == task_id)
        if scope is not None:
            statement = statement.where(*_owner_conditions(scope))
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        task = _decode_task(row)
        if task.task_id != task_id:
            raise _corrupt("schedule.read_task")
        return task

    async def _read_occurrence(
        self,
        connection: AsyncConnection,
        run: ScheduleRun,
        *,
        for_update: bool = False,
    ) -> ScheduleRun | None:
        statement = select(schedule_runs).where(
            schedule_runs.c.task_id == run.task_id,
            schedule_runs.c.occurrence_key_hash
            == occurrence_key(run.task_id, run.occurrence_key),
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        existing = _decode_run(row)
        if (
            existing.task_id != run.task_id
            or existing.occurrence_key != run.occurrence_key
        ):
            raise _corrupt("schedule.read_occurrence")
        return existing

    async def _require_run(
        self,
        connection: AsyncConnection,
        run_id: str,
        *,
        for_update: bool,
    ) -> ScheduleRun:
        statement = select(schedule_runs).where(
            schedule_runs.c.schedule_run_id == run_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            raise _state_error("schedule.read_run")
        run = _decode_run(row)
        if run.schedule_run_id != run_id:
            raise _corrupt("schedule.read_run")
        return run

    @staticmethod
    async def _replace_task(connection: AsyncConnection, task: ScheduleTask) -> None:
        await connection.execute(
            update(schedule_tasks)
            .where(
                schedule_tasks.c.task_id == task.task_id,
                schedule_tasks.c.app_id == task.app_id,
                schedule_tasks.c.user_id == task.user_id,
                schedule_tasks.c.agent_id == task.agent_id,
                schedule_tasks.c.version == task.version - 1,
            )
            .values(**_task_values(task))
        )

    @staticmethod
    async def _replace_run(connection: AsyncConnection, run: ScheduleRun) -> None:
        await connection.execute(
            update(schedule_runs)
            .where(schedule_runs.c.schedule_run_id == run.schedule_run_id)
            .values(**_run_values(run))
        )

    async def _read[ResultT](
        self,
        operation: str,
        callback: Callable[[AsyncConnection], Awaitable[ResultT]],
    ) -> ResultT:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    return await callback(connection)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout(operation) from error
        except AdapterError:
            raise
        except SQLAlchemyError as error:
            raise _storage(operation) from error

    async def _write[ResultT](
        self,
        operation: str,
        callback: Callable[[AsyncConnection], Awaitable[ResultT]],
    ) -> ResultT:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    return await callback(connection)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _timeout(operation) from error
        except AdapterError:
            raise
        except IntegrityError as error:
            raise _error(
                operation,
                "schedule_run_conflict",
                "定时任务执行实例已存在",
            ) from error
        except SQLAlchemyError as error:
            raise _storage(operation) from error


def _sql_time(value: datetime) -> datetime:
    """以无时区UTC保存DATETIME，读取时恢复UTC语义。"""
    return value.astimezone(UTC).replace(tzinfo=None)


def _column_values(value: ScheduleTask | ScheduleRun) -> dict[str, object]:
    return {
        name: _sql_time(item) if isinstance(item, datetime) else item
        for name, item in value.model_dump().items()
    }


def _task_values(task: ScheduleTask) -> dict[str, object]:
    return _column_values(task)


def _run_values(run: ScheduleRun) -> dict[str, object]:
    return {
        **_column_values(run),
        "occurrence_key_hash": occurrence_key(run.task_id, run.occurrence_key),
    }


def _decode_values(row: RowMapping) -> dict[str, object]:
    return {
        str(name): value.replace(tzinfo=UTC) if isinstance(value, datetime) else value
        for name, value in row.items()
        if name != "occurrence_key_hash"
    }


def _decode_task(row: RowMapping) -> ScheduleTask:
    try:
        return ScheduleTask.model_validate(_decode_values(row))
    except ValidationError as error:
        raise _corrupt("schedule.decode_task") from error


def _decode_run(row: RowMapping) -> ScheduleRun:
    try:
        run = ScheduleRun.model_validate(_decode_values(row))
    except ValidationError as error:
        raise _corrupt("schedule.decode_run") from error
    if bytes(row["occurrence_key_hash"]) != occurrence_key(
        run.task_id, run.occurrence_key
    ):
        raise _corrupt("schedule.decode_run")
    return run


def _owner_conditions(scope: ScheduleScope) -> tuple[ColumnElement[bool], ...]:
    return (
        schedule_tasks.c.app_id == scope.app_id,
        schedule_tasks.c.user_id == scope.user_id,
        schedule_tasks.c.agent_id == scope.agent_id,
    )


def _validate_scheduled(
    current: ScheduleTask | None, candidate: ScheduleTask, run: ScheduleRun
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


def _in_scope(task: ScheduleTask, scope: ScheduleScope) -> bool:
    return (
        task.app_id == scope.app_id
        and task.user_id == scope.user_id
        and task.agent_id == scope.agent_id
    )


def _version_error(operation: str) -> AdapterError:
    return _error(operation, "schedule_version_conflict", "定时任务版本冲突")


def _state_error(operation: str) -> AdapterError:
    return _error(operation, "schedule_state_conflict", "定时任务状态冲突")


def _corrupt(operation: str) -> AdapterError:
    return _error(operation, "schedule_storage_corrupt", "SQL 定时任务数据损坏")


def _timeout(operation: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        code="schedule_storage_timeout",
        message="数据库定时任务操作超时",
        retryable=True,
    )


def _storage(operation: str) -> AdapterError:
    return AdapterError(
        operation=operation,
        message="数据库定时任务操作失败",
        retryable=True,
    )


def _error(operation: str, code: str, message: str) -> AdapterError:
    return AdapterError(operation=operation, code=code, message=message)


__all__ = ["SqlScheduleManager"]
