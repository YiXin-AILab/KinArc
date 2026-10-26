"""真实 MySQL Engine 与 Alembic revision 合约。"""

from agentsystem.database.schema import (
    DATABASE_SCHEMA_REVISION,
    assert_database_schema_current,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_client_connects_and_migration_reaches_expected_revision(
    mysql_engine: AsyncEngine,
) -> None:
    """项目容器必须支持异步查询并处于当前 schema revision。"""
    async with mysql_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1

    await assert_database_schema_current(
        mysql_engine,
        expected_revision=DATABASE_SCHEMA_REVISION,
        timeout_seconds=5,
    )
