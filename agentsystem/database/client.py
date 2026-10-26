"""Application 所有的 SQLAlchemy AsyncEngine 生命周期。"""

import asyncio
from contextlib import suppress

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentsystem.config.models import MysqlDatabaseConfig


class DatabaseConnectionError(RuntimeError):
    """表示 Engine 启动探活失败，供应用启动边界识别。

    原始连接或超时异常通过异常链保留；取消不会转换为此异常。
    """


def create_database_url(config: MysqlDatabaseConfig) -> URL:
    """从拆分字段安全构造 asyncmy URL，不手工拼接凭证。

    Args:
        config: 已校验的数据库连接及连接池配置。

    Returns:
        保留结构化凭证字段的 mysql+asyncmy URL，不执行连接。
    """
    return URL.create(
        "mysql+asyncmy",
        username=config.username,
        password=(
            config.password.get_secret_value() if config.password is not None else None
        ),
        host=config.host,
        port=config.port,
        database=config.name,
    )


async def create_database_engine(config: MysqlDatabaseConfig) -> AsyncEngine:
    """创建共享 Engine，并在有限时间内执行一次连接探活。

    Args:
        config: 已校验的数据库连接及连接池配置。

    Returns:
        探活成功的共享 Engine，调用方负责最终关闭。

    Raises:
        DatabaseConnectionError: 连接探活失败或超时；失败时先释放连接池。
    """
    engine = create_async_engine(
        create_database_url(config),
        pool_pre_ping=True,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout_seconds,
        connect_args={"connect_timeout": config.connect_timeout_seconds},
    )
    try:
        async with asyncio.timeout(config.connect_timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except asyncio.CancelledError:
        with suppress(BaseException):
            await engine.dispose()
        raise
    except BaseException as error:
        with suppress(BaseException):
            await engine.dispose()
        raise DatabaseConnectionError("数据库连接验证失败") from error
    return engine


async def close_database_engine(engine: AsyncEngine) -> None:
    """等待共享 Engine 关闭全部连接池资源。

    Args:
        engine: 应用持有并负责关闭的共享 AsyncEngine。
    """
    await engine.dispose()
