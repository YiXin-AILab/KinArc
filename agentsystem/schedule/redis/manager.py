"""使用 Redis Hash、Sorted Set 和 Lua 的 ScheduleManager。"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from agentsystem.common.errors import AdapterError
from agentsystem.redis.client import RedisClient, await_redis
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
from agentsystem.schedule.redis.keys import ScheduleRedisKeys

_SCRIPT_DIR = Path(__file__).with_name("scripts")
_CREATE_TASK = (_SCRIPT_DIR / "create_task.lua").read_text(encoding="utf-8")
_UPDATE_TASK = (_SCRIPT_DIR / "update_task.lua").read_text(encoding="utf-8")
_MATERIALIZE = (_SCRIPT_DIR / "materialize_schedule_run.lua").read_text(
    encoding="utf-8"
)
_COMPARE_SET_RUN = (_SCRIPT_DIR / "compare_set_run.lua").read_text(encoding="utf-8")
_BATCH_SET_RUNS = (_SCRIPT_DIR / "batch_set_runs.lua").read_text(encoding="utf-8")
_TERMINAL = {
    ScheduleRunStatus.SUCCEEDED,
    ScheduleRunStatus.FAILED,
    ScheduleRunStatus.CANCELED,
}
type RedisArgument = bytes | str | int | float


class RedisScheduleManager:
    """使用共享 Redis client 和模块内 Lua 实现 ScheduleManager。

    Task/Run 及 occurrence 索引属于同一 hash slot，条件写入与批量
    更新在脚本中原子完成。Cron 由注入匹配器校验，I/O 受有限超时
    约束；Application 管理连接生命周期，adapter 不投递 Inbox。
    """

    def __init__(
        self,
        client: RedisClient,
        *,
        prefix: str,
        cron: CronMatcher,
        operation_timeout_seconds: float,
    ) -> None:
        """绑定共享 client、Schedule namespace 与有限 timeout。

        Args:
            client: Application 创建和关闭的共享异步 Redis client。
            prefix: Application 分配的 Redis 命名空间。
            cron: 部署时区下的 Cron 校验与匹配器。
            operation_timeout_seconds: 外部操作的有限超时秒数。
        """
        self._client = client
        self._keys = ScheduleRedisKeys(prefix)
        self._cron = cron
        self._timeout = operation_timeout_seconds

    async def create_task(self, task: ScheduleTask) -> ScheduleTask:
        """创建任务。

        Args:
            task: 完整的任务事实。

        Returns:
            操作完成后的任务事实。

        Raises:
            AdapterError: 任务 ID 已存在或存储操作失败。
        """
        result = await self._eval(
            _CREATE_TASK,
            (self._keys.tasks_data, self._keys.tasks_enabled),
            (task.task_id, _encode(task), _enabled(task)),
        )
        if result != 1:
            raise _error(
                "schedule.create_task", "schedule_task_conflict", "定时任务已存在"
            )
        return task

    async def get_task(
        self,
        scope: ScheduleScope,
        task_id: str,
    ) -> ScheduleTask | None:
        """按作用域读取任务。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            task_id: 目标定时任务 ID。

        Returns:
            范围内未删除的任务；不存在、已删除或越界时为 None。
        """
        task = await self._read_task(task_id)
        if task is None or task.deleted_at is not None or not _in_scope(task, scope):
            return None
        return task

    async def list_tasks(
        self,
        scope: ScheduleScope,
        query: ScheduleTaskQuery,
    ) -> ScheduleTaskPage:
        """分页查询任务。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按修改时间与 ID 降序排列的任务页及下一页边界。
        """
        tasks = [
            task
            for task in await self._all_tasks()
            if task.deleted_at is None
            and _in_scope(task, scope)
            and (query.status is None or task.status is query.status)
            and (query.task_type is None or task.task_type is query.task_type)
            and _before_task(task, query.cursor)
        ]
        tasks.sort(key=lambda item: (item.updated_at, item.task_id), reverse=True)
        page = tasks[: query.limit]
        cursor = None
        if len(tasks) > query.limit:
            last = page[-1]
            cursor = ScheduleTaskCursor(
                updated_at=last.updated_at, task_id=last.task_id
            )
        return ScheduleTaskPage(items=tuple(page), next_cursor=cursor)

    async def scan_enabled_tasks(
        self,
        *,
        after_task_id: str | None,
        limit: int,
    ) -> tuple[ScheduleTask, ...]:
        """以 task ID 升序扫描可触发任务。

        Args:
            after_task_id: 上一批最后一个 Task ID，None 表示从头扫描。
            limit: 本次最多返回的记录数量。

        Returns:
            按 Task ID 升序排列的启用任务批次，空元组表示扫描结束。
        """
        raw_ids = cast(
            list[bytes],
            await self._call(self._client.zrange(self._keys.tasks_enabled, 0, -1)),
        )
        ids = [item.decode() for item in raw_ids]
        selected = [
            item for item in ids if after_task_id is None or item > after_task_id
        ]
        tasks = [await self._read_task(task_id) for task_id in selected[:limit]]
        return tuple(task for task in tasks if task is not None)

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
        """通过 version CAS 修改任务。

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
        current = await self._task_for_mutation(scope, task_id, "schedule.update_task")
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
        updated = ScheduleTask.model_validate(
            current.model_copy(update=changes).model_dump()
        )
        await self._replace_task(current, updated, "schedule.update_task")
        return updated

    async def delete_task(
        self,
        scope: ScheduleScope,
        task_id: str,
        *,
        expected_version: int,
        deleted_at: datetime,
    ) -> ScheduleTask:
        """通过 version CAS 软删除任务。

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
        current = await self._task_for_mutation(scope, task_id, "schedule.delete_task")
        if current.version != expected_version:
            raise _version_error("schedule.delete_task")
        deleted = current.model_copy(
            update={
                "status": ScheduleTaskStatus.DISABLED,
                "version": current.version + 1,
                "updated_at": deleted_at,
                "deleted_at": deleted_at,
            }
        )
        await self._replace_task(current, deleted, "schedule.delete_task")
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
        if run.trigger_kind is not ScheduleTriggerKind.SCHEDULED:
            raise _state_error("schedule.materialize_scheduled")
        completed = ""
        if candidate.task_type is ScheduleTaskType.ONE_TIME:
            completed = _encode(
                candidate.model_copy(
                    update={
                        "status": ScheduleTaskStatus.COMPLETED,
                        "version": candidate.version + 1,
                        "updated_at": run.created_at,
                    }
                )
            )
        return await self._materialize(candidate, run, completed)

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
        current = await self._read_task(task.task_id)
        if current is None or current.deleted_at is not None:
            raise _state_error("schedule.materialize_manual")
        if run.trigger_kind is not ScheduleTriggerKind.MANUAL:
            raise _state_error("schedule.materialize_manual")
        return await self._materialize(current, run, "")

    async def record_dispatch_failure(
        self,
        schedule_run_id: str,
        *,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        """尽力记录 MQ 投递失败诊断。

        Args:
            schedule_run_id: 目标 ScheduleRun ID。
            failed_at: 本次 Inbox 投递失败时间。
            error_code: 失败诊断代码。
            error_message: 失败诊断说明。

        Raises:
            AdapterError: 执行实例不存在、不处于 pending 或存储失败。
        """
        run = await self._require_run(schedule_run_id)
        if run.status is not ScheduleRunStatus.PENDING:
            raise _state_error("schedule.record_dispatch_failure")
        updated = run.model_copy(
            update={
                "dispatch_failed_at": failed_at,
                "dispatch_error_code": error_code,
                "dispatch_error_message": error_message,
                "updated_at": failed_at,
            }
        )
        await self._replace_run(run, updated, "schedule.record_dispatch_failure")

    async def mark_runs_running(
        self,
        schedule_run_ids: tuple[str, ...],
        *,
        runtime_run_id: str,
        started_at: datetime,
    ) -> None:
        """批量绑定 Runtime run 并进入 running。

        Args:
            schedule_run_ids: 需要绑定到同一 Runtime run 的执行实例 ID。
            runtime_run_id: Runtime 持锁领取输入后生成的内部 run ID。
            started_at: Runtime 实际开始时间。

        Raises:
            AdapterError: 实例不存在、状态或 Runtime 绑定冲突，或存储失败。
        """
        runs = tuple([await self._require_run(run_id) for run_id in schedule_run_ids])
        updates: list[tuple[ScheduleRun, ScheduleRun]] = []
        for run in runs:
            if run.status is ScheduleRunStatus.RUNNING:
                if run.runtime_run_id != runtime_run_id:
                    raise _state_error("schedule.mark_runs_running")
                continue
            if run.status is not ScheduleRunStatus.PENDING:
                raise _state_error("schedule.mark_runs_running")
            updates.append(
                (
                    run,
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
            )
        await self._replace_runs(updates, "schedule.mark_runs_running")

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
        """批量收敛共享 Runtime run 的 ScheduleRun。

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
        runs = [
            run
            for run in await self._all_runs()
            if run.runtime_run_id == runtime_run_id
        ]
        updates: list[tuple[ScheduleRun, ScheduleRun]] = []
        for run in runs:
            if run.status in _TERMINAL:
                if run.status is not status:
                    raise _state_error("schedule.mark_runs_terminal")
                continue
            if run.status is not ScheduleRunStatus.RUNNING:
                raise _state_error("schedule.mark_runs_terminal")
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
            updates.append((run, ScheduleRun.model_validate(updated.model_dump())))
        await self._replace_runs(updates, "schedule.mark_runs_terminal")

    async def list_runs(
        self,
        scope: ScheduleScope,
        query: ScheduleRunQuery,
    ) -> ScheduleRunPage:
        """分页查询执行历史。

        Args:
            scope: 应用、用户与 Agent 的完整归属范围。
            query: 筛选条件、分页边界和数量限制。

        Returns:
            按创建时间与 ID 降序排列的历史页及下一页边界。
        """
        runs = [
            run
            for run in await self._all_runs()
            if run.app_id_snapshot == scope.app_id
            and run.user_id_snapshot == scope.user_id
            and run.agent_id_snapshot == scope.agent_id
            and (query.task_id is None or run.task_id == query.task_id)
            and (query.status is None or run.status is query.status)
            and _before_run(run, query.cursor)
        ]
        runs.sort(
            key=lambda item: (item.created_at, item.schedule_run_id), reverse=True
        )
        page = runs[: query.limit]
        cursor = None
        if len(runs) > query.limit:
            last = page[-1]
            cursor = ScheduleRunCursor(
                created_at=last.created_at,
                schedule_run_id=last.schedule_run_id,
            )
        return ScheduleRunPage(items=tuple(page), next_cursor=cursor)

    async def _materialize(
        self,
        task: ScheduleTask,
        run: ScheduleRun,
        completed_payload: str,
    ) -> MaterializeResult:
        field = _occurrence_field(run.task_id, run.occurrence_key)
        raw = await self._eval(
            _MATERIALIZE,
            (
                self._keys.tasks_data,
                self._keys.runs_data,
                self._keys.occurrences,
                self._keys.tasks_enabled,
            ),
            (
                task.task_id,
                _encode(task),
                field,
                run.schedule_run_id,
                _encode(run),
                completed_payload,
            ),
        )
        code, raw_id = cast(list[object], raw)
        run_id = _text(raw_id)
        if int(cast(int, code)) == 1:
            return MaterializeResult(run=run, was_created=True)
        if int(cast(int, code)) == 0:
            existing = await self._require_run(run_id)
            if (
                existing.task_id != run.task_id
                or existing.occurrence_key != run.occurrence_key
            ):
                raise _error(
                    "schedule.materialize",
                    "schedule_storage_corrupt",
                    "Redis occurrence 冲突",
                )
            return MaterializeResult(run=existing, was_created=False)
        raise _state_error("schedule.materialize")

    async def _replace_task(
        self,
        previous: ScheduleTask,
        current: ScheduleTask,
        operation: str,
    ) -> None:
        result = await self._eval(
            _UPDATE_TASK,
            (self._keys.tasks_data, self._keys.tasks_enabled),
            (current.task_id, _encode(previous), _encode(current), _enabled(current)),
        )
        if result != 1:
            raise _version_error(operation)

    async def _replace_run(
        self,
        previous: ScheduleRun,
        current: ScheduleRun,
        operation: str,
    ) -> None:
        result = await self._eval(
            _COMPARE_SET_RUN,
            (self._keys.runs_data,),
            (current.schedule_run_id, _encode(previous), _encode(current)),
        )
        if result != 1:
            raise _state_error(operation)

    async def _replace_runs(
        self,
        updates: list[tuple[ScheduleRun, ScheduleRun]],
        operation: str,
    ) -> None:
        if not updates:
            return
        arguments: list[RedisArgument] = [len(updates)]
        for previous, current in updates:
            arguments.extend(
                (current.schedule_run_id, _encode(previous), _encode(current))
            )
        result = await self._eval(
            _BATCH_SET_RUNS,
            (self._keys.runs_data,),
            tuple(arguments),
        )
        if result != 1:
            raise _state_error(operation)

    async def _task_for_mutation(
        self,
        scope: ScheduleScope,
        task_id: str,
        operation: str,
    ) -> ScheduleTask:
        task = await self._read_task(task_id)
        if task is None or task.deleted_at is not None or not _in_scope(task, scope):
            raise _error(operation, "schedule_task_not_found", "定时任务不存在")
        return task

    async def _read_task(self, task_id: str) -> ScheduleTask | None:
        raw = await self._call(self._client.hget(self._keys.tasks_data, task_id))
        return None if raw is None else _decode_task(raw)

    async def _require_run(self, run_id: str) -> ScheduleRun:
        raw = await self._call(self._client.hget(self._keys.runs_data, run_id))
        if raw is None:
            raise _state_error("schedule.read_run")
        return _decode_run(raw)

    async def _all_tasks(self) -> tuple[ScheduleTask, ...]:
        values = cast(
            list[bytes], await self._call(self._client.hvals(self._keys.tasks_data))
        )
        return tuple(_decode_task(value) for value in values)

    async def _all_runs(self) -> tuple[ScheduleRun, ...]:
        values = cast(
            list[bytes], await self._call(self._client.hvals(self._keys.runs_data))
        )
        return tuple(_decode_run(value) for value in values)

    async def _eval(
        self,
        script: str,
        keys: tuple[str, ...],
        arguments: tuple[RedisArgument, ...],
    ) -> object:
        return await self._call(self._client.eval(script, len(keys), *keys, *arguments))

    async def _call[ResultT](self, value: Awaitable[ResultT] | ResultT) -> ResultT:
        try:
            async with asyncio.timeout(self._timeout):
                return await await_redis(value)
        except TimeoutError as error:
            raise _error(
                "schedule.redis", "schedule_storage_timeout", "Redis 定时任务操作超时"
            ) from error


def _encode(value: ScheduleTask | ScheduleRun) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_task(value: bytes | str) -> ScheduleTask:
    try:
        return ScheduleTask.model_validate_json(value)
    except ValidationError as error:
        raise _error(
            "schedule.decode_task", "schedule_storage_corrupt", "Redis Task 数据损坏"
        ) from error


def _decode_run(value: bytes | str) -> ScheduleRun:
    try:
        return ScheduleRun.model_validate_json(value)
    except ValidationError as error:
        raise _error(
            "schedule.decode_run", "schedule_storage_corrupt", "Redis Run 数据损坏"
        ) from error


def _enabled(task: ScheduleTask) -> str:
    return (
        "1"
        if task.status is ScheduleTaskStatus.ENABLED and task.deleted_at is None
        else "0"
    )


def _occurrence_field(task_id: str, occurrence_key: str) -> str:
    raw = f"{task_id}\0{occurrence_key}".encode()
    return hashlib.sha256(raw).hexdigest()


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _in_scope(task: ScheduleTask, scope: ScheduleScope) -> bool:
    return (
        task.app_id == scope.app_id
        and task.user_id == scope.user_id
        and task.agent_id == scope.agent_id
    )


def _before_task(task: ScheduleTask, cursor: ScheduleTaskCursor | None) -> bool:
    return cursor is None or (task.updated_at, task.task_id) < (
        cursor.updated_at,
        cursor.task_id,
    )


def _before_run(run: ScheduleRun, cursor: ScheduleRunCursor | None) -> bool:
    return cursor is None or (run.created_at, run.schedule_run_id) < (
        cursor.created_at,
        cursor.schedule_run_id,
    )


def _version_error(operation: str) -> AdapterError:
    return _error(operation, "schedule_version_conflict", "定时任务版本冲突")


def _state_error(operation: str) -> AdapterError:
    return _error(operation, "schedule_state_conflict", "定时任务状态冲突")


def _error(operation: str, code: str, message: str) -> AdapterError:
    return AdapterError(operation=operation, code=code, message=message)


__all__ = ["RedisScheduleManager"]
