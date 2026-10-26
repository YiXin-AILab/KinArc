"""MessageManager 模块专属 SQLAlchemy Core schema 测试。"""

from agentsystem.message.sql.schema import message_metadata, message_sessions, messages
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


def test_message_schema_has_only_module_owned_tables() -> None:
    """Message metadata 不得集中管理其他领域表。"""
    assert set(message_metadata.tables) == {
        "runtime_message_sessions",
        "runtime_messages",
        "runtime_conversations",
    }
    assert list(message_sessions.primary_key.columns.keys()) == ["scope_key"]
    assert list(messages.primary_key.columns.keys()) == [
        "session_scope_key",
        "message_key",
    ]


def test_message_payload_compiles_to_mysql_longblob() -> None:
    """权威 payload 必须支持超过普通 BLOB 上限的 Message。"""
    ddl = str(CreateTable(messages).compile(dialect=mysql.dialect()))

    assert "LONGBLOB" in ddl
    assert "UNIQUE (session_scope_key, position)" in ddl
    assert "CHECK (position >= 1)" in ddl
