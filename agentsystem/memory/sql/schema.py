"""Memory 独占的普通关系表字段。"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql

memory_metadata = MetaData()
_COLLATION = "utf8mb4_bin"
runtime_memories = Table(
    "runtime_memories",
    memory_metadata,
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
