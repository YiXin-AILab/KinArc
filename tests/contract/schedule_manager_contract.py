"""ScheduleManager 三种权威存储共享行为合约。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.schedule.manager import ScheduleManager
from agentsystem.schedule.models import (
    ScheduleRun,
    ScheduleRunQuery,
    ScheduleRunStatus,
    ScheduleScope,
    ScheduleSessionMode,
    ScheduleTask,
    ScheduleTaskPatch,
    ScheduleTaskQuery,
    ScheduleTaskStatus,
    ScheduleTaskType,
    ScheduleTriggerKind,
)

NOW = datetime(2026, 8, 18, 1, tzinfo=UTC)
SCOPE = ScheduleScope(app_id="app", user_id="user", agent_id="agent")


def make_task(**updates: object) -> ScheduleTask:
    """构造合约使用的 Task。"""
    values: dict[str, object] = {
        "task_id": f"task-{uuid4().hex}",
        "app_id": SCOPE.app_id,
        "user_id": "user",
        "agent_id": SCOPE.agent_id,
        "name": "日报",
        "prompt": "总结昨天工作",
        "cron_expression": "0 9 * * *",
        "task_type": ScheduleTaskType.RECURRING,
        "session_mode": ScheduleSessionMode.ISOLATED,
        "status": ScheduleTaskStatus.ENABLED,
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return ScheduleTask.model_validate(values)


def make_run(task: ScheduleTask, **updates: object) -> ScheduleRun:
    """从 Task 快照构造 pending Run。"""
    run_id = f"run-{uuid4().hex}"
    values: dict[str, object] = {
        "schedule_run_id": run_id,
        "task_id": task.task_id,
        "occurrence_key": "scheduled:2026-08-18T01:00Z",
        "trigger_kind": ScheduleTriggerKind.SCHEDULED,
        "scheduled_for": NOW,
        "task_name_snapshot": task.name,
        "app_id_snapshot": task.app_id,
        "user_id_snapshot": task.user_id,
        "agent_id_snapshot": task.agent_id,
        "prompt_snapshot": task.prompt,
        "session_id": f"session-{uuid4().hex}",
        "message_id": f"schedule-run:{run_id}",
        "status": ScheduleRunStatus.PENDING,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return ScheduleRun.model_validate(values)


class ScheduleManagerContract:
    """每个 ScheduleManager adapter 必须通过的行为。"""

    async def test_other_user_cannot_read_mutate_or_view_deleted_history(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """相同应用和Agent中的另一用户不能通过已知标识越界访问。"""
        task = make_task()
        await schedule_manager.create_task(task)
        run = await schedule_manager.materialize_scheduled(
            candidate=task, run=make_run(task)
        )
        other = SCOPE.model_copy(update={"user_id": "other-user"})
        assert await schedule_manager.get_task(other, task.task_id) is None
        assert not (await schedule_manager.list_tasks(other, ScheduleTaskQuery())).items
        assert not (
            await schedule_manager.list_runs(
                other, ScheduleRunQuery(task_id=task.task_id)
            )
        ).items
        with pytest.raises(AdapterError) as raised:
            await schedule_manager.update_task(
                other,
                task.task_id,
                expected_version=1,
                patch=ScheduleTaskPatch(name="越界"),
                updated_at=NOW,
                fixed_session_id=None,
            )
        assert raised.value.code == "schedule_task_not_found"
        with pytest.raises(AdapterError) as raised:
            await schedule_manager.delete_task(
                other, task.task_id, expected_version=1, deleted_at=NOW
            )
        assert raised.value.code == "schedule_task_not_found"
        await schedule_manager.delete_task(
            SCOPE, task.task_id, expected_version=1, deleted_at=NOW
        )
        assert (await schedule_manager.list_runs(SCOPE, ScheduleRunQuery())).items == (
            run.run,
        )
        assert not (await schedule_manager.list_runs(other, ScheduleRunQuery())).items

    async def test_create_get_and_scope(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """创建后只能在 app + agent 作用域内读取。"""
        task = make_task()

        assert await schedule_manager.create_task(task) == task
        assert await schedule_manager.get_task(SCOPE, task.task_id) == task
        assert (
            await schedule_manager.get_task(
                ScheduleScope(app_id="other", user_id="user", agent_id="agent"),
                task.task_id,
            )
            is None
        )

    async def test_duplicate_task_conflicts(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """重复 task ID 必须返回稳定冲突。"""
        task = make_task()
        await schedule_manager.create_task(task)

        with pytest.raises(AdapterError, match="已存在") as raised:
            await schedule_manager.create_task(task)

        assert raised.value.code == "schedule_task_conflict"

    async def test_task_list_uses_descending_keyset_page(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """Task 列表按 updated_at 和 ID 稳定倒序。"""
        first = make_task(task_id="task-a", updated_at=NOW)
        second = make_task(task_id="task-b", updated_at=NOW + timedelta(minutes=1))
        await schedule_manager.create_task(first)
        await schedule_manager.create_task(second)

        page = await schedule_manager.list_tasks(
            SCOPE,
            ScheduleTaskQuery(limit=1),
        )
        assert page.items == (second,)
        assert page.next_cursor is not None

        next_page = await schedule_manager.list_tasks(
            SCOPE,
            ScheduleTaskQuery(limit=1, cursor=page.next_cursor),
        )
        assert next_page.items == (first,)
        assert next_page.next_cursor is None

    async def test_update_uses_version_cas_and_delete_is_soft(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """修改和软删除共享 version CAS。"""
        task = make_task()
        await schedule_manager.create_task(task)
        updated = await schedule_manager.update_task(
            SCOPE,
            task.task_id,
            expected_version=1,
            patch=ScheduleTaskPatch(name="新日报"),
            updated_at=NOW + timedelta(minutes=1),
            fixed_session_id=None,
        )
        assert updated.name == "新日报"
        assert updated.version == 2

        with pytest.raises(AdapterError) as raised:
            await schedule_manager.update_task(
                SCOPE,
                task.task_id,
                expected_version=1,
                patch=ScheduleTaskPatch(name="stale"),
                updated_at=NOW + timedelta(minutes=2),
                fixed_session_id=None,
            )
        assert raised.value.code == "schedule_version_conflict"

        deleted = await schedule_manager.delete_task(
            SCOPE,
            task.task_id,
            expected_version=2,
            deleted_at=NOW + timedelta(minutes=2),
        )
        assert deleted.status is ScheduleTaskStatus.DISABLED
        assert deleted.deleted_at is not None
        assert deleted.version == 3

    async def test_same_occurrence_materializes_one_run(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """相同 occurrence 的重复物化返回同一 Run。"""
        task = make_task()
        await schedule_manager.create_task(task)
        run = make_run(task)

        first = await schedule_manager.materialize_scheduled(candidate=task, run=run)
        second = await schedule_manager.materialize_scheduled(
            candidate=task,
            run=make_run(task),
        )

        assert first.was_created is True
        assert second.was_created is False
        assert second.run.schedule_run_id == first.run.schedule_run_id

    async def test_concurrent_one_time_materialization_has_one_winner(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """one-time Task 物化与 completed 更新必须原子。"""
        task = make_task(task_type=ScheduleTaskType.ONE_TIME)
        await schedule_manager.create_task(task)

        results = await asyncio.gather(
            *(
                schedule_manager.materialize_scheduled(
                    candidate=task,
                    run=make_run(task),
                )
                for _ in range(20)
            )
        )

        assert sum(result.was_created for result in results) == 1
        assert len({result.run.schedule_run_id for result in results}) == 1
        stored = await schedule_manager.get_task(SCOPE, task.task_id)
        assert stored is not None
        assert stored.status is ScheduleTaskStatus.COMPLETED
        assert stored.version == 2

    async def test_manual_run_allows_disabled_but_rejects_deleted(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """停用不禁止手动执行，软删除禁止。"""
        task = make_task(status=ScheduleTaskStatus.DISABLED)
        await schedule_manager.create_task(task)
        manual = make_run(
            task,
            occurrence_key="manual:runtime:tool-call",
            trigger_kind=ScheduleTriggerKind.MANUAL,
            scheduled_for=NOW + timedelta(seconds=30),
        )

        result = await schedule_manager.materialize_manual(task=task, run=manual)
        assert result.was_created is True

        deleted = await schedule_manager.delete_task(
            SCOPE,
            task.task_id,
            expected_version=1,
            deleted_at=NOW + timedelta(minutes=1),
        )
        with pytest.raises(AdapterError) as raised:
            await schedule_manager.materialize_manual(
                task=deleted,
                run=make_run(deleted),
            )
        assert raised.value.code == "schedule_state_conflict"

    async def test_dispatch_running_and_terminal_tracking(
        self,
        schedule_manager: ScheduleManager,
    ) -> None:
        """投递诊断与 Middleware 状态转换持久化到历史。"""
        task = make_task()
        await schedule_manager.create_task(task)
        created = await schedule_manager.materialize_scheduled(
            candidate=task,
            run=make_run(task),
        )
        await schedule_manager.record_dispatch_failure(
            created.run.schedule_run_id,
            failed_at=NOW + timedelta(seconds=1),
            error_code="schedule_dispatch_failed",
            error_message="定时任务消息投递失败",
        )
        await schedule_manager.mark_runs_running(
            (created.run.schedule_run_id,),
            runtime_run_id="runtime-1",
            started_at=NOW + timedelta(seconds=2),
        )
        await schedule_manager.mark_runs_terminal(
            "runtime-1",
            status=ScheduleRunStatus.FAILED,
            completed_at=NOW + timedelta(seconds=3),
            final_message_id=None,
            error_code="execution_failed",
            error_message="执行失败",
        )

        page = await schedule_manager.list_runs(SCOPE, ScheduleRunQuery())
        assert len(page.items) == 1
        stored = page.items[0]
        assert stored.status is ScheduleRunStatus.FAILED
        assert stored.runtime_run_id == "runtime-1"
        assert stored.dispatch_error_code is None
        assert stored.error_code == "execution_failed"
