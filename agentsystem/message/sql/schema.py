"""仅属于 MessageManager 的 SQLAlchemy Core 表结构。"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql

message_metadata = MetaData()
_KEY_TYPE = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")

conversations = Table(
    "runtime_conversations",
    message_metadata,
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
    message_metadata,
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
    message_metadata,
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

__all__ = ["message_metadata", "message_sessions", "messages"]
