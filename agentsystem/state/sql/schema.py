"""仅属于 StateManager 的 SQLAlchemy Core 表结构。"""

from sqlalchemy import Column, LargeBinary, MetaData, Table, Text
from sqlalchemy.dialects import mysql

state_metadata = MetaData()
_KEY_TYPE = LargeBinary(32).with_variant(mysql.BINARY(32), "mysql")
_PAYLOAD_TYPE = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")

states = Table(
    "runtime_states",
    state_metadata,
    Column("scope_key", _KEY_TYPE, primary_key=True),
    Column("app_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("payload", _PAYLOAD_TYPE, nullable=False),
    Column("payload_sha256", _KEY_TYPE, nullable=False),
)

__all__ = ["state_metadata", "states"]
