"""从严格 RuntimeConfig 构建异步 Alembic migration 环境。"""

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import RuntimeConfig
from agentsystem.database.client import create_database_url
from agentsystem.memory.sql.schema import memory_metadata
from agentsystem.message.sql.schema import message_metadata
from agentsystem.schedule.sql.schema import schedule_metadata
from agentsystem.state.sql.schema import state_metadata
from agentsystem.todo.sql.schema import todo_metadata

_ALEMBIC_CONFIG = context.config
if _ALEMBIC_CONFIG.config_file_name is not None:
    fileConfig(
        _ALEMBIC_CONFIG.config_file_name,
        disable_existing_loggers=False,
    )

target_metadata = (
    message_metadata,
    state_metadata,
    schedule_metadata,
    memory_metadata,
    todo_metadata,
)


def _runtime_config_path() -> Path:
    arguments = context.get_x_argument(as_dictionary=True)
    return Path(arguments.get("runtime_config", "config/runtime.yaml"))


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """为迁移单独创建无连接池复用的 Engine，并在结束时释放资源。"""
    runtime_config = load_yaml_config(_runtime_config_path(), RuntimeConfig)
    database = runtime_config.database
    if database is None:
        raise RuntimeError("Alembic migration 必须配置 database")
    engine = create_async_engine(
        create_database_url(database),
        poolclass=NullPool,
        connect_args={"connect_timeout": database.connect_timeout_seconds},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """使用外部同步连接或自建异步 Engine 在线执行迁移。

    Alembic 提供 connection 时复用其事务环境；否则读取 RuntimeConfig，
    启动异步迁移并关闭临时 Engine。此入口不支持 offline SQL 渲染。

    Raises:
        RuntimeError: 未配置数据库，或当前线程已有运行中的事件循环。
    """
    supplied_connection = _ALEMBIC_CONFIG.attributes.get("connection")
    if isinstance(supplied_connection, Connection):
        _do_run_migrations(supplied_connection)
        return
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    raise RuntimeError("不支持可能渲染凭证的 Alembic offline 模式")
run_migrations_online()
