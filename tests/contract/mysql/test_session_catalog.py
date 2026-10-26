"""真实 MySQL 对话目录共享合约。"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.message.sql.schema import conversations
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.session_catalog_contract import SessionCatalogContract


@pytest.fixture
def catalog_context() -> SessionContext:
    return SessionContext(
        app_id=uuid4().hex, user_id=uuid4().hex, agent_id="agent", session_id="s"
    )


@pytest.fixture
async def catalog(
    mysql_engine: AsyncEngine, catalog_context: SessionContext
) -> AsyncIterator[MessageManager]:
    try:
        yield SqlMessageManager(mysql_engine, operation_timeout_seconds=5)
    finally:
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    (conversations.c.app_id == catalog_context.app_id)
                    | (conversations.c.user_id == catalog_context.user_id)
                )
            )


class TestSqlSessionCatalog(SessionCatalogContract):
    """在真实 MySQL 上执行目录行为合约。"""
