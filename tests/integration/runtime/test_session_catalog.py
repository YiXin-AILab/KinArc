"""真实存储和完整 ServiceHost 装配的对话接口端到端验证。"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    DispatcherConfig,
    ManagerAdaptersConfig,
    RuntimeConfig,
    SqlManagerAdapterConfig,
)
from agentsystem.database.client import close_database_engine, create_database_engine
from agentsystem.message.manager import MessageManager
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.message.sql.keys import session_scope_key
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.message.sql.schema import conversations, message_sessions, messages
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from server.app import create_api_app
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.redis.conftest import delete_prefix
from tests.integration.runtime.helpers import _agent_config
from tests.integration.runtime.test_sql_message_service import _upgrade
from tests.mysql_config import require_local_mysql_config
from tests.redis_config import local_redis_config
from tests.runtime_config import TEST_ASSET_LIMITS, TEST_ASSET_STORE, TEST_PROJECT_ROOT
from tests.unit.service.test_titles import TitleProvider


@pytest.fixture(params=["redis", "sql"])
async def catalog_storage(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[RuntimeConfig, AsyncEngine | None]]:
    """独立 namespace；SQL 仅在显式提供测试环境时连接。"""
    engine = None
    config = RuntimeConfig(
        redis=local_redis_config(key_prefix=f"catalog-e2e-{uuid4().hex}"),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(ready_block_ms=10, heartbeat_interval_seconds=0.1),
    )
    if request.param == "sql":
        database = require_local_mysql_config()
        config = RuntimeConfig(
            **(
                config.model_dump()
                | {
                    "database": database,
                    "managers": ManagerAdaptersConfig(
                        message=SqlManagerAdapterConfig()
                    ),
                }
            )
        )
        engine = await create_database_engine(database)
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
    try:
        yield config, engine
    finally:
        if engine is not None:
            await close_database_engine(engine)


async def test_catalog_http_with_real_host_and_storage(
    catalog_storage: tuple[RuntimeConfig, AsyncEngine | None],
) -> None:
    """标题接口持久化结果，模型复用生效且不改变消息和执行快照。"""
    config, engine = catalog_storage
    client = await create_redis_client(config.redis)
    provider = TitleProvider("自动生成标题")
    ctx = SessionContext(
        app_id=uuid4().hex, user_id="user", agent_id="agent", session_id="session"
    )
    host = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            AgentRuntimeSpec(
                agent_config=_agent_config("agent"),
                base_prompt="内部系统提示",
                llm_provider=provider,
            ),
        ),
        consumer_id="catalog-worker",
        redis_client=client,
        database_engine=engine,
    )
    manager: MessageManager = (
        SqlMessageManager(engine, operation_timeout_seconds=5)
        if engine is not None
        else RedisMessageManager(client, prefix=config.redis.key_prefix)
    )
    try:
        await host.start()
        transport = httpx.ASGITransport(app=create_api_app(host.application))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            scope = {
                "app_id": ctx.app_id,
                "user_id": ctx.user_id,
                "agent_id": ctx.agent_id,
            }
            response = await http.post(
                "/v1/sessions/session/messages",
                json={
                    **scope,
                    "message_id": "first",
                    "content": [{"type": "text", "text": "中" * 17}],
                },
            )
            assert response.status_code == 202
            async with asyncio.timeout(5):
                while True:
                    snapshot = await host.application.snapshot(ctx)
                    if (
                        snapshot is not None
                        and snapshot.status is SessionStatus.SUCCEEDED
                    ):
                        break
                    await asyncio.sleep(0.01)
            stored = await manager.list_messages(ctx)
            listed = await http.get("/v1/sessions", params=scope)
            assert listed.status_code == 200
            assert listed.json()["sessions"][0]["title"] == "中" * 16 + "..."
            renamed = await http.post(
                "/v1/sessions/session", json={**scope, "title": "人工"}
            )
            assert renamed.status_code == 200
            result = await http.post("/v1/sessions/session/title/generate", json=scope)
            assert (
                result.status_code == 200 and result.json()["title"] == "自动生成标题"
            )
            info = await manager.get_session(ctx)
            assert info is not None and info.title == "自动生成标题"
            assert len(provider.requests) == 2
            assert "内部系统提示" not in str(provider.requests[-1].messages)
            assert provider.requests[-1].tools == ()
            assert await manager.list_messages(ctx) == stored
            assert await host.application.snapshot(ctx) == snapshot
            deleted = await http.post("/v1/sessions/session/delete", json=scope)
            assert deleted.status_code == 204 and deleted.content == b""
            assert (await http.get("/v1/sessions", params=scope)).json()[
                "sessions"
            ] == []
            assert await manager.list_messages(ctx) == stored
            assert (
                await http.post("/v1/sessions/session/title/generate", json=scope)
            ).status_code == 404
            for _ in range(2):
                assert (
                    await http.post(
                        "/v1/sessions/session/delete",
                        json={**scope, "hard_delete": True},
                    )
                ).status_code == 204
            assert await manager.list_messages(ctx) == ()
            assert (
                await http.post(
                    "/v1/sessions/session/messages",
                    json={
                        **scope,
                        "message_id": "late",
                        "content": [{"type": "text", "text": "复活"}],
                    },
                )
            ).status_code == 409
    finally:
        await host.close()
        if engine is not None:
            key = session_scope_key(ctx)
            async with engine.begin() as connection:
                await connection.execute(
                    delete(messages).where(messages.c.session_scope_key == key)
                )
                await connection.execute(
                    delete(message_sessions).where(message_sessions.c.scope_key == key)
                )
                await connection.execute(
                    delete(conversations).where(conversations.c.scope_key == key)
                )
        await delete_prefix(client, config.redis.key_prefix)
        await close_redis_client(client)
