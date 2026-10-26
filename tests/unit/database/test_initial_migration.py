"""开源初始 schema 的版本、建表与回退保护测试。"""

from datetime import datetime
from pathlib import Path
from sqlite3 import Connection as SqliteConnection

import pytest
from agentsystem.database.schema import DATABASE_SCHEMA_REVISION
from agentsystem.memory.sql.schema import memory_metadata
from agentsystem.message.sql.schema import message_metadata
from agentsystem.schedule.sql.schema import schedule_metadata
from agentsystem.state.sql.schema import state_metadata
from agentsystem.todo.sql.schema import todo_metadata
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    create_engine,
    inspect,
)

METADATA = MetaData()
for component_metadata in (
    message_metadata,
    state_metadata,
    schedule_metadata,
    memory_metadata,
    todo_metadata,
):
    for component_table in component_metadata.tables.values():
        component_table.to_metadata(METADATA)
TABLES = METADATA.tables


def migration_config() -> Config:
    """返回使用仓库迁移目录的 Alembic 配置，不加载部署环境。"""
    return Config(Path(__file__).parents[3] / "alembic.ini")


def test_history_has_only_initial_revision() -> None:
    """全新数据库只执行一个无前置版本的初始迁移。"""
    scripts = ScriptDirectory.from_config(migration_config())
    revisions = list(scripts.walk_revisions())
    assert [item.revision for item in revisions] == ["0001_initial"]
    assert revisions[0].down_revision is None
    assert DATABASE_SCHEMA_REVISION == "0001_initial"


@pytest.mark.parametrize("populated_table", [None, *TABLES])
def test_initial_upgrade_and_safe_downgrade(populated_table: str | None) -> None:
    """建表与运行时 schema 一致，任意业务表非空时不得部分删除。"""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        driver = connection.connection.driver_connection
        assert isinstance(driver, SqliteConnection)
        driver.create_collation(
            "utf8mb4_bin", lambda first, second: (first > second) - (first < second)
        )
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        context = MigrationContext.configure(connection)
        assert context.get_current_revision() == "0001_initial"
        assert compare_metadata(context, METADATA) == []
        if populated_table is not None:
            table = TABLES[populated_table]
            values: dict[str, object] = {}
            for column in table.columns:
                if column.nullable or column.server_default is not None:
                    continue
                if isinstance(column.type, Boolean):
                    values[column.name] = False
                elif isinstance(column.type, Integer):
                    values[column.name] = 1
                elif isinstance(column.type, DateTime):
                    values[column.name] = datetime(2026, 1, 1)
                elif isinstance(column.type, LargeBinary):
                    values[column.name] = b"x" * 32
                else:
                    values[column.name] = "sample"
            connection.execute(table.insert().values(values))
            with pytest.raises(RuntimeError, match="拒绝 downgrade"):
                command.downgrade(config, "base")
            assert set(inspect(connection).get_table_names()) == {
                *TABLES,
                "alembic_version",
            }
            assert context.get_current_revision() == "0001_initial"
            connection.execute(table.delete())
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        assert context.get_current_revision() is None
    engine.dispose()
