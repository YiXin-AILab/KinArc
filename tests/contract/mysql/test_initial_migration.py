"""初始迁移在真实 MySQL 上必须与运行时最终 schema 一致。"""

from agentsystem.memory.sql.schema import memory_metadata
from agentsystem.message.sql.schema import message_metadata
from agentsystem.schedule.sql.schema import schedule_metadata
from agentsystem.state.sql.schema import state_metadata
from agentsystem.todo.sql.schema import todo_metadata
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, String, text
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_initial_schema_matches_runtime(mysql_engine: AsyncEngine) -> None:
    """真实建表结果包含全部字段、索引、约束和 5.7 兼容排序规则。"""

    def check_schema(connection: Connection) -> None:
        # MySQL 5.7 接受但不保存 CHECK；不能将其反射缺失当作迁移漏建。
        supports_checks = (connection.dialect.server_version_info or ()) >= (8, 0, 16)

        def include_object(
            obj: object,
            name: str | None,
            type_: str,
            reflected: bool,
            compare_to: object,
        ) -> bool:
            return supports_checks or type_ != "check_constraint"

        context = MigrationContext.configure(
            connection, opts={"include_object": include_object}
        )
        assert context.get_current_revision() == "0001_initial"
        metadata = MetaData()
        for component in (
            message_metadata,
            state_metadata,
            schedule_metadata,
            memory_metadata,
            todo_metadata,
        ):
            for table in component.tables.values():
                table.to_metadata(metadata)
        assert compare_metadata(context, metadata) == []
        columns = connection.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND (TABLE_NAME IN ('runtime_memories', 'runtime_todos') "
                "OR TABLE_NAME LIKE 'runtime_schedule_%') "
                "AND COLLATION_NAME IS NOT NULL"
            )
        ).all()
        collations = {(table, column): value for table, column, value in columns}
        for table in metadata.tables.values():
            for column in table.columns:
                column_type = column.type.dialect_impl(mysql.dialect())
                if (
                    isinstance(column_type, String)
                    and column_type.collation == "utf8mb4_bin"
                ):
                    assert collations[table.name, column.name] == "utf8mb4_bin"

    async with mysql_engine.connect() as connection:
        await connection.run_sync(check_schema)
