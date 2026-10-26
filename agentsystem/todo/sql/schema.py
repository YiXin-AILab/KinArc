"""一份清单一行，四个身份字段联合主键。"""

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text
from sqlalchemy.dialects import mysql

todo_metadata = MetaData()
runtime_todos = Table(
    "runtime_todos",
    todo_metadata,
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
