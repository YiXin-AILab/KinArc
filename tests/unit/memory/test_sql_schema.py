"""SQL 正文必须支持精确等号比较。"""

from agentsystem.memory.sql.schema import runtime_memories
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


def test_normal_columns_and_exact_text_comparison() -> None:
    ddl = str(CreateTable(runtime_memories).compile(dialect=mysql.dialect()))
    assert "LONGTEXT COLLATE utf8mb4_bin" in ddl
    assert "AUTO_INCREMENT" in ddl
    assert "UNIQUE (app_id, scope, user_id, agent_id)" in ddl
    assert set(runtime_memories.c.keys()) == {
        "id",
        "app_id",
        "scope",
        "user_id",
        "agent_id",
        "content",
        "created_at",
        "updated_at",
    }
