"""定时任务领域模型测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.schedule.models import (
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleScope,
    ScheduleSessionMode,
    ScheduleTask,
    ScheduleTaskPatch,
    ScheduleTaskStatus,
    ScheduleTaskType,
    ScheduleTriggerKind,
)
from pydantic import ValidationError

_NOW = datetime(2026, 8, 18, tzinfo=UTC)


def _task(**updates: object) -> ScheduleTask:
    values: dict[str, object] = {
        "task_id": "task-1",
        "app_id": "app",
        "user_id": "user",
        "agent_id": "agent",
        "name": "日报",
        "prompt": "总结昨天工作",
        "cron_expression": "0 9 * * *",
        "task_type": ScheduleTaskType.RECURRING,
        "session_mode": ScheduleSessionMode.ISOLATED,
        "status": ScheduleTaskStatus.ENABLED,
        "version": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(updates)
    return ScheduleTask.model_validate(values)


def test_fixed_task_requires_fixed_session_id() -> None:
    """fixed Task 必须保存管理 Tool 的当前会话。"""
    with pytest.raises(ValidationError, match="fixed_session_id"):
        _task(session_mode=ScheduleSessionMode.FIXED)


def test_isolated_task_rejects_fixed_session_id() -> None:
    """isolated Task 不得残留 fixed session。"""
    with pytest.raises(ValidationError, match="fixed_session_id"):
        _task(fixed_session_id="session-1")


def test_completed_status_is_reserved_for_one_time_task() -> None:
    """completed 只表示 one-time Task 已物化。"""
    with pytest.raises(ValidationError, match="one_time"):
        _task(status=ScheduleTaskStatus.COMPLETED)


def test_update_patch_requires_at_least_one_field() -> None:
    """update 不接受空 patch。"""
    with pytest.raises(ValidationError, match="至少一个"):
        ScheduleTaskPatch()


def test_running_run_requires_runtime_binding() -> None:
    """running Run 必须记录 Runtime run 和开始时间。"""
    with pytest.raises(ValidationError, match="runtime_run_id"):
        ScheduleRun(
            schedule_run_id="schedule-run-1",
            task_id="task-1",
            occurrence_key="scheduled:2026-08-18T01:00Z",
            trigger_kind=ScheduleTriggerKind.SCHEDULED,
            scheduled_for=_NOW,
            task_name_snapshot="日报",
            app_id_snapshot="app",
            user_id_snapshot="user",
            agent_id_snapshot="agent",
            prompt_snapshot="总结昨天工作",
            session_id="session-1",
            message_id="schedule-run:schedule-run-1",
            status=ScheduleRunStatus.RUNNING,
            created_at=_NOW,
            updated_at=_NOW,
        )


def test_task_is_immutable() -> None:
    """已返回 Task 是不可变运行事实。"""
    task = _task()

    with pytest.raises(ValidationError, match="frozen"):
        task.__setattr__("name", "changed")


@pytest.mark.parametrize("field", ["task_id", "app_id", "user_id", "agent_id"])
def test_task_rejects_trailing_space_identity(field: str) -> None:
    """所有存储使用一致的身份限制。"""
    with pytest.raises(ValidationError, match="trailing space"):
        _task(**{field: "id "})


def test_schedule_scope_and_cursor_input_reject_trailing_space() -> None:
    """查询入口也不能用尾部空格访问另一个身份。"""
    with pytest.raises(ValidationError, match="trailing space"):
        ScheduleScope(app_id="app", user_id="user ", agent_id="agent")
