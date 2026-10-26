"""数据库 schema revision 启动检查测试。"""

from typing import Any, cast

import pytest
from agentsystem.database.schema import (
    DATABASE_SCHEMA_REVISION,
    DatabaseSchemaVersionError,
    assert_database_schema_current,
)
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_schema_check_accepts_expected_revision() -> None:
    """当前 revision 与代码一致时允许 Runtime 继续启动。"""
    engine = _SchemaEngine(DATABASE_SCHEMA_REVISION)

    await assert_database_schema_current(
        cast(AsyncEngine, engine),
        expected_revision=DATABASE_SCHEMA_REVISION,
        timeout_seconds=1,
    )

    assert engine.run_sync_count == 1


async def test_schema_check_rejects_missing_or_stale_revision() -> None:
    """缺失 migration 必须 fail fast，且错误只包含 revision。"""
    engine = _SchemaEngine(None)

    with pytest.raises(DatabaseSchemaVersionError) as captured:
        await assert_database_schema_current(
            cast(AsyncEngine, engine),
            expected_revision=DATABASE_SCHEMA_REVISION,
            timeout_seconds=1,
        )

    assert DATABASE_SCHEMA_REVISION in str(captured.value)
    assert "actual=None" in str(captured.value)
    assert "://" not in str(captured.value)


class _SchemaConnection:
    def __init__(self, engine: "_SchemaEngine") -> None:
        self._engine = engine

    async def run_sync(self, function: object) -> str | None:
        del function
        self._engine.run_sync_count += 1
        return self._engine.revision


class _SchemaConnectionContext:
    def __init__(self, engine: "_SchemaEngine") -> None:
        self._connection = _SchemaConnection(engine)

    async def __aenter__(self) -> _SchemaConnection:
        return self._connection

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: Any,
    ) -> None:
        del error_type, error, traceback


class _SchemaEngine:
    def __init__(self, revision: str | None) -> None:
        self.revision = revision
        self.run_sync_count = 0

    def connect(self) -> _SchemaConnectionContext:
        return _SchemaConnectionContext(self)
