"""开源首版完整数据库结构；此快照不得随运行时 schema 自动变化。"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects import mysql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METADATA = MetaData()

_KEY_TYPE = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")

conversations = Table(
    "runtime_conversations",
    _METADATA,
    Column("scope_key", _KEY_TYPE, primary_key=True),
    Column("user_key", _KEY_TYPE, nullable=False),
    Column("agent_key", _KEY_TYPE, nullable=False),
    Column("app_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("title", String(200), nullable=False),
    Column("title_version", BigInteger, nullable=False),
    Column("is_deleted", Boolean, nullable=False, server_default="0"),
    Column(
        "created_at",
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=False,
    ),
    Column(
        "sort_key",
        String(91).with_variant(
            mysql.VARCHAR(91, charset="ascii", collation="ascii_bin"), "mysql"
        ),
        nullable=False,
    ),
    CheckConstraint(
        "title_version >= 1", name="ck_runtime_conversations_title_version"
    ),
    Index("ix_runtime_conversations_user", "user_key", "is_deleted", "sort_key"),
    Index("ix_runtime_conversations_agent", "agent_key", "is_deleted", "sort_key"),
)

message_sessions = Table(
    "runtime_message_sessions",
    _METADATA,
    Column("scope_key", _KEY_TYPE, primary_key=True),
    Column("app_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("next_position", BigInteger, nullable=False),
    Column("latest_compaction_message_id", Text),
    CheckConstraint(
        "next_position >= 1",
        name="ck_runtime_message_sessions_next_position",
    ),
)

_PAYLOAD_TYPE = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")

messages = Table(
    "runtime_messages",
    _METADATA,
    Column(
        "session_scope_key",
        _KEY_TYPE,
        ForeignKey("runtime_message_sessions.scope_key", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("message_key", _KEY_TYPE, primary_key=True),
    Column("message_id", Text, nullable=False),
    Column("position", BigInteger, nullable=False),
    Column("payload", _PAYLOAD_TYPE, nullable=False),
    Column("payload_sha256", _KEY_TYPE, nullable=False),
    Column("origin", String(32), nullable=False),
    CheckConstraint("position >= 1", name="ck_runtime_messages_position"),
    UniqueConstraint(
        "session_scope_key",
        "position",
        name="uq_runtime_messages_session_position",
    ),
)

states = Table(
    "runtime_states",
    _METADATA,
    Column("scope_key", _KEY_TYPE, primary_key=True),
    Column("app_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("payload", _PAYLOAD_TYPE, nullable=False),
    Column("payload_sha256", _KEY_TYPE, nullable=False),
)

_ID = String(255).with_variant(mysql.VARCHAR(255, collation="utf8mb4_bin"), "mysql")
_TIME = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")

schedule_tasks = Table(
    "runtime_schedule_tasks",
    _METADATA,
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
    _METADATA,
    Column("schedule_run_id", _ID, primary_key=True),
    Column("task_id", _ID, nullable=False),
    Column("occurrence_key", _TEXT, nullable=False),
    Column("occurrence_key_hash", _KEY_TYPE, nullable=False),
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

_COLLATION = "utf8mb4_bin"
runtime_memories = Table(
    "runtime_memories",
    _METADATA,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("app_id", String(191, collation=_COLLATION), nullable=False),
    Column("scope", String(16, collation=_COLLATION), nullable=False),
    Column("user_id", String(191, collation=_COLLATION), nullable=False),
    Column("agent_id", String(191, collation=_COLLATION), nullable=False),
    Column(
        "content",
        Text().with_variant(mysql.LONGTEXT(collation=_COLLATION), "mysql"),
        nullable=False,
    ),
    Column(
        "created_at",
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=False,
    ),
    UniqueConstraint(
        "app_id", "scope", "user_id", "agent_id", name="uq_runtime_memories_identity"
    ),
    mysql_charset="utf8mb4",
)

runtime_todos = Table(
    "runtime_todos",
    _METADATA,
    *(
        Column(name, String(191, collation="utf8mb4_bin"), primary_key=True)
        for name in ("app_id", "user_id", "agent_id", "session_id")
    ),
    Column(
        "todos_json", Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False
    ),
    Column(
        "created_at",
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
        nullable=False,
    ),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_row_format="DYNAMIC",
)


def upgrade() -> None:
    """在空白数据库中直接建立最终表、索引和约束。"""
    _METADATA.create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    """仅在所有业务表均为空时回退，先完成检查再执行任何 DDL。

    Raises:
        RuntimeError: 任意业务表含有不可丢弃的数据或删除标记。
    """
    connection = op.get_bind()
    for table in _METADATA.sorted_tables:
        query = select(1).select_from(table).limit(1)
        if connection.execute(query).first() is not None:
            raise RuntimeError(f"{table.name} 表非空，拒绝 downgrade")
    _METADATA.drop_all(connection, checkfirst=False)
