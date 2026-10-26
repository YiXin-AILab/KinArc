"""AsyncEngine 创建与连接验证单元测试。"""

from collections.abc import Callable
from typing import Any, cast

import pytest
from agentsystem.config.models import MysqlDatabaseConfig
from agentsystem.database.client import (
    DatabaseConnectionError,
    close_database_engine,
    create_database_engine,
    create_database_url,
)
from sqlalchemy.ext.asyncio import AsyncEngine


def mysql_config(*, password: str = "p@ss:/word") -> MysqlDatabaseConfig:
    """返回包含特殊字符密码的数据库配置。"""
    return MysqlDatabaseConfig(
        host="db.internal",
        port=3306,
        name="agentsystem",
        username="runtime",
        password=password,
        connect_timeout_seconds=3,
        pool_size=8,
        max_overflow=4,
        pool_timeout_seconds=2,
        operation_timeout_seconds=7,
    )


def test_database_url_does_not_require_manual_password_escaping() -> None:
    """URL.create 必须原样接收密码并只在渲染时编码。"""
    url = create_database_url(mysql_config())

    assert url.drivername == "mysql+asyncmy"
    assert url.password == "p@ss:/word"
    assert url.database == "agentsystem"
    assert "p%40ss%3A%2Fword" in url.render_as_string(hide_password=False)


async def test_create_engine_forwards_pool_limits_and_pings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Engine 工厂必须配置连接池并在返回前完成真实查询边界。"""
    engine = _FakeEngine()
    captured: dict[str, object] = {}

    def fake_create_async_engine(
        url: object,
        **options: object,
    ) -> AsyncEngine:
        captured["url"] = url
        captured.update(options)
        return cast(AsyncEngine, engine)

    monkeypatch.setattr(
        "agentsystem.database.client.create_async_engine",
        fake_create_async_engine,
    )

    created = await create_database_engine(mysql_config())

    assert created is cast(AsyncEngine, engine)
    assert engine.execute_count == 1
    assert captured["pool_pre_ping"] is True
    assert captured["pool_size"] == 8
    assert captured["max_overflow"] == 4
    assert captured["pool_timeout"] == 2
    assert captured["connect_args"] == {"connect_timeout": 3.0}


async def test_create_engine_disposes_and_sanitizes_ping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动探活失败必须关闭池且不泄漏原始异常或地址。"""
    engine = _FakeEngine(execute_error=OSError("db.internal secret detail"))
    monkeypatch.setattr(
        "agentsystem.database.client.create_async_engine",
        cast(Callable[..., AsyncEngine], lambda *args, **kwargs: engine),
    )

    with pytest.raises(DatabaseConnectionError) as captured:
        await create_database_engine(mysql_config())

    assert engine.disposed is True
    assert "db.internal" not in str(captured.value)
    assert "p@ss:/word" not in str(captured.value)


async def test_close_database_engine_disposes_pool() -> None:
    """显式关闭必须等待 Engine 释放连接池。"""
    engine = _FakeEngine()

    await close_database_engine(cast(AsyncEngine, engine))

    assert engine.disposed is True


class _FakeConnection:
    def __init__(self, engine: "_FakeEngine") -> None:
        self._engine = engine

    async def execute(self, statement: object) -> None:
        del statement
        self._engine.execute_count += 1
        if self._engine.execute_error is not None:
            raise self._engine.execute_error


class _FakeConnectionContext:
    def __init__(self, engine: "_FakeEngine") -> None:
        self._connection = _FakeConnection(engine)

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: Any,
    ) -> None:
        del error_type, error, traceback


class _FakeEngine:
    def __init__(self, *, execute_error: BaseException | None = None) -> None:
        self.execute_error = execute_error
        self.execute_count = 0
        self.disposed = False

    def connect(self) -> _FakeConnectionContext:
        return _FakeConnectionContext(self)

    async def dispose(self) -> None:
        self.disposed = True
