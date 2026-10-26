"""真实 MySQL 合约测试的 Engine 与 migration fixture。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agentsystem.database.client import close_database_engine, create_database_engine
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.mysql_config import require_local_mysql_config

PROJECT_ROOT = Path(__file__).parents[3]


def _upgrade(connection: Connection) -> None:
    """在测试 Engine 的同步桥接连接上运行 Alembic upgrade。"""
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


@pytest.fixture
async def mysql_engine() -> AsyncIterator[AsyncEngine]:
    """建立真实 MySQL Engine，并以 Alembic 初始化 schema。"""
    engine = await create_database_engine(require_local_mysql_config())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
        yield engine
    finally:
        await close_database_engine(engine)
