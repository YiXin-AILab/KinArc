"""验证定时任务SQL存储采用可查询的业务字段列。"""

from agentsystem.schedule.models import ScheduleRun, ScheduleTask
from agentsystem.schedule.sql.schema import schedule_runs, schedule_tasks


def test_all_domain_fields_have_columns_without_payload() -> None:
    """任务和历史的业务字段必须独立持久化，不能藏在payload中。"""
    assert set(schedule_tasks.c.keys()) == set(ScheduleTask.model_fields)
    assert set(schedule_runs.c.keys()) == set(ScheduleRun.model_fields) | {
        "occurrence_key_hash"
    }
