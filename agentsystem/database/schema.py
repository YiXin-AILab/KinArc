"""数据库 migration revision 的有限等待启动检查。"""

import asyncio

from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

DATABASE_SCHEMA_REVISION = "0001_initial"


class DatabaseSchemaVersionError(RuntimeError):
    """表示启动时无法读取或不匹配的 Alembic revision。

    用于阻止不兼容数据库进入服务；不负责执行迁移或修复表结构。
    """


async def assert_database_schema_current(
    engine: AsyncEngine,
    *,
    expected_revision: str,
    timeout_seconds: float,
) -> None:
    """读取 Alembic revision，不匹配时阻止 Runtime 接受任务。

    Args:
        engine: 应用持有并负责关闭的共享 AsyncEngine。
        expected_revision: 当前 Runtime 要求的 Alembic revision。
        timeout_seconds: 整个版本读取操作的等待上限，单位为秒。

    Raises:
        DatabaseSchemaVersionError: 版本读取失败、超时或实际版本不匹配。
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as connection:
                actual_revision = await connection.run_sync(_current_revision)
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        raise DatabaseSchemaVersionError("无法检查 database schema revision") from error
    if actual_revision != expected_revision:
        raise DatabaseSchemaVersionError(
            "database schema revision 不匹配："
            f"expected={expected_revision} actual={actual_revision}"
        )


def _current_revision(connection: Connection) -> str | None:
    """通过同步桥接读取当前 Alembic revision。"""
    return MigrationContext.configure(connection).get_current_revision()
