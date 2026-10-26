"""仅属于 ScheduleManager 的 SQLAlchemy Core 表结构。"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql

schedule_metadata = MetaData()
_KEY = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")
_ID = String(255).with_variant(mysql.VARCHAR(255, collation="utf8mb4_bin"), "mysql")
_TIME = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")

schedule_tasks = Table(
    "runtime_schedule_tasks",
    schedule_metadata,
    Column("task_id", _ID, primary_key=True),
    Column("app_id", _ID, nullable=False),
    Column("user_id", _ID, nullable=False),
    Column("agent_id", _ID, nullable=False),
    Column("name", String(255), nullable=False),
    Column("prompt", _TEXT, nullable=False),
    Column("cron_expression", String(255), nullable=False),
    Column("task_type", String(32), nullable=False),
    Column("session_mode", String(32), nullable=False),
    Column("fixed_session_id", _ID),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", _TIME, nullable=False),
    Column("updated_at", _TIME, nullable=False),
    Column("deleted_at", _TIME),
)

# 长Unicode标识使用前缀索引控制MySQL索引长度；WHERE仍比较完整字段。
Index(
    "ix_schedule_tasks_owner_page",
    schedule_tasks.c.app_id,
    schedule_tasks.c.user_id,
    schedule_tasks.c.agent_id,
    schedule_tasks.c.updated_at,
    schedule_tasks.c.task_id,
    mysql_length={"app_id": 64, "user_id": 64, "agent_id": 64, "task_id": 64},
)
Index(
    "ix_schedule_tasks_scan",
    schedule_tasks.c.status,
    schedule_tasks.c.deleted_at,
    schedule_tasks.c.task_id,
)

schedule_runs = Table(
    "runtime_schedule_runs",
    schedule_metadata,
    Column("schedule_run_id", _ID, primary_key=True),
    Column("task_id", _ID, nullable=False),
    Column("occurrence_key", _TEXT, nullable=False),
    Column("occurrence_key_hash", _KEY, nullable=False),
    Column("trigger_kind", String(32), nullable=False),
    Column("scheduled_for", _TIME, nullable=False),
    Column("task_name_snapshot", String(255), nullable=False),
    Column("app_id_snapshot", _ID, nullable=False),
    Column("user_id_snapshot", _ID, nullable=False),
    Column("agent_id_snapshot", _ID, nullable=False),
    Column("prompt_snapshot", _TEXT, nullable=False),
    Column("session_id", _ID, nullable=False),
    Column("message_id", _ID, nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("dispatch_failed_at", _TIME),
    Column("dispatch_error_code", _ID),
    Column("dispatch_error_message", _TEXT),
    Column("runtime_run_id", _ID, index=True),
    Column("started_at", _TIME),
    Column("completed_at", _TIME),
    Column("final_message_id", _ID),
    Column("error_code", _ID),
    Column("error_message", _TEXT),
    Column("created_at", _TIME, nullable=False),
    Column("updated_at", _TIME, nullable=False),
    UniqueConstraint(
        "task_id",
        "occurrence_key_hash",
        name="uq_runtime_schedule_runs_occurrence",
    ),
)
Index(
    "ix_schedule_runs_owner_page",
    schedule_runs.c.app_id_snapshot,
    schedule_runs.c.user_id_snapshot,
    schedule_runs.c.agent_id_snapshot,
    schedule_runs.c.created_at,
    schedule_runs.c.schedule_run_id,
    mysql_length={
        "app_id_snapshot": 64,
        "user_id_snapshot": 64,
        "agent_id_snapshot": 64,
        "schedule_run_id": 64,
    },
)
Index(
    "ix_schedule_runs_task_page",
    schedule_runs.c.task_id,
    schedule_runs.c.created_at,
    schedule_runs.c.schedule_run_id,
    mysql_length={"task_id": 64, "schedule_run_id": 64},
)

__all__ = ["schedule_metadata", "schedule_runs", "schedule_tasks"]
