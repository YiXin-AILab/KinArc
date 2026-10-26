"""ServiceHost 使用 Redis/SQL 可替换权威存储的真实集成测试。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    DispatcherConfig,
    ManagerAdaptersConfig,
    ModelParamsConfig,
    RedisManagerAdapterConfig,
    RuntimeConfig,
    SqlManagerAdapterConfig,
)
from agentsystem.database.client import close_database_engine, create_database_engine
from agentsystem.database.schema import DatabaseSchemaVersionError
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.message.models import MessageVisibility, Role, TextBlock
from agentsystem.message.redis.keys import MessageRedisKeys
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.message.sql.keys import session_scope_key
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.message.sql.schema import message_sessions, messages
from agentsystem.redis.client import (
    await_redis,
    close_redis_client,
    create_redis_client,
)
from agentsystem.runtime.state import SessionStatus
from agentsystem.service import host as service_module
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import SubmitMessageCommand
from agentsystem.state.redis.keys import StateRedisKeys
from agentsystem.state.sql.keys import state_scope_key
from agentsystem.state.sql.manager import SqlStateManager
from agentsystem.state.sql.schema import states
from alembic import command
from alembic.config import Config
from litellm import ModelResponseStream
from redis.asyncio.cluster import RedisCluster
from sqlalchemy import delete
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.redis.conftest import delete_cluster_prefix, delete_prefix
from tests.mysql_config import local_mysql_config, require_local_mysql_config
from tests.redis_config import cluster_redis_config, local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream

PROJECT_ROOT = Path(__file__).parents[3]


def _upgrade(connection: Connection) -> None:
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


@pytest.fixture
async def integration_mysql_engine() -> AsyncIterator[AsyncEngine]:
    """为混合存储测试提供已迁移的真实 MySQL Engine。"""
    engine = await create_database_engine(require_local_mysql_config())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
        yield engine
    finally:
        await close_database_engine(engine)


@pytest.mark.parametrize(
    ("message_type", "state_type"),
    [("sql", "redis"), ("redis", "sql"), ("sql", "sql")],
)
@pytest.mark.parametrize("redis_mode", ["standalone", "cluster"])
async def test_runtime_supports_each_sql_authority_combination(
    integration_mysql_engine: AsyncEngine,
    message_type: str,
    state_type: str,
    redis_mode: str,
) -> None:
    """主链路分别使用所选 Message/State 权威，不进行双写。"""
    prefix = f"sql-authority-service-{uuid4().hex}"
    database = local_mysql_config()
    selectors = {
        "redis": RedisManagerAdapterConfig(),
        "sql": SqlManagerAdapterConfig(),
    }
    redis_config = (
        cluster_redis_config(key_prefix=prefix)
        if redis_mode == "cluster"
        else local_redis_config(key_prefix=prefix)
    )
    if redis_config is None:
        pytest.skip("需要 TEST_REDIS_CLUSTER_NODES")
    config = RuntimeConfig(
        redis=redis_config,
        database=database,
        managers=ManagerAdaptersConfig(
            message=selectors[message_type],
            state=selectors[state_type],
        ),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    redis = await create_redis_client(config.redis)
    context = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="sql-agent",
        session_id=f"sql-session-{uuid4().hex}",
    )

    captured_requests: list[dict[str, object]] = []

    async def completion(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        captured_requests.append(dict(kwargs))
        return text_stream("sql-answer", response_id="sql-response")

    service = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            AgentRuntimeSpec(
                agent_config=AgentConfig(
                    agent_id=context.agent_id,
                    agent_name="SQL Agent",
                    prompt="unused.md",
                    outbound=AgentOutboundConfig(type="log"),
                    llm_model=ModelParamsConfig(model="openai/test-model"),
                ),
                base_prompt="SQL agent",
                llm_provider=LiteLLMProvider(completion=completion),
            ),
        ),
        consumer_id="sql-worker",
        redis_client=redis,
        database_engine=integration_mysql_engine,
    )
    try:
        await service.start()
        await service.application.submit_message(
            context,
            SubmitMessageCommand(
                message_id="system-1",
                role=InboundRole.SYSTEM,
                origin="workflow.refund",
                content=(TextBlock(text="refund completed"),),
            ),
        )
        async with asyncio.timeout(3):
            while True:
                snapshot = await service.application.snapshot(context)
                if snapshot is not None and snapshot.status is SessionStatus.SUCCEEDED:
                    break
                await asyncio.sleep(0.01)

        if message_type == "sql":
            stored_messages = await SqlMessageManager(
                integration_mysql_engine,
                operation_timeout_seconds=5.0,
            ).list_messages(context)
            assert not await await_redis(
                redis.exists(MessageRedisKeys(prefix).data(context))
            )
        else:
            stored_messages = await RedisMessageManager(
                redis,
                prefix=prefix,
            ).list_messages(context)
        assert [message.role for message in stored_messages] == [
            Role.SYSTEM,
            Role.SYSTEM,
            Role.ASSISTANT,
        ]
        assert [message.visibility for message in stored_messages] == [
            MessageVisibility.MODEL,
            MessageVisibility.ALL,
            MessageVisibility.ALL,
        ]
        assert stored_messages[0].run_id is None
        assert stored_messages[1].message_id == "system-1"
        assert stored_messages[1].origin == "workflow.refund"
        assert stored_messages[2].content == (TextBlock(text="sql-answer"),)
        provider_messages = cast(
            list[dict[str, object]],
            captured_requests[0]["messages"],
        )
        assert [message["role"] for message in provider_messages] == [
            "system",
            "system",
        ]
        assert snapshot is not None
        assert snapshot.status is SessionStatus.SUCCEEDED
        if state_type == "sql":
            sql_state = await SqlStateManager(
                integration_mysql_engine,
                operation_timeout_seconds=5.0,
            ).get(context)
            assert sql_state is not None
            assert sql_state.status is snapshot.status
            assert sql_state.final_message_id == stored_messages[-1].message_id
            assert not await await_redis(
                redis.exists(StateRedisKeys(prefix).state(context))
            )
    finally:
        await service.close()
        scope_key = session_scope_key(context)
        async with integration_mysql_engine.begin() as connection:
            await connection.execute(
                delete(messages).where(messages.c.session_scope_key == scope_key)
            )
            await connection.execute(
                delete(message_sessions).where(
                    message_sessions.c.scope_key == scope_key
                )
            )
            await connection.execute(
                delete(states).where(states.c.scope_key == state_scope_key(context))
            )
        if isinstance(redis, RedisCluster):
            await delete_cluster_prefix(redis, prefix)
        else:
            await delete_prefix(redis, prefix)
        await close_redis_client(redis)


async def test_service_disposes_owned_database_engine_once(
    monkeypatch: pytest.MonkeyPatch,
    integration_mysql_engine: AsyncEngine,
) -> None:
    """Runtime 创建的 Engine 只在 Service close 时释放一次。"""
    prefix = f"sql-owned-engine-{uuid4().hex}"
    config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        database=local_mysql_config(),
        managers=ManagerAdaptersConfig(
            message=SqlManagerAdapterConfig(),
        ),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
    )
    redis = await create_redis_client(config.redis)
    closed: list[AsyncEngine] = []

    async def fake_create(_config: object) -> AsyncEngine:
        return integration_mysql_engine

    async def fake_close(engine: AsyncEngine) -> None:
        closed.append(engine)

    monkeypatch.setattr(service_module, "create_database_engine", fake_create)
    monkeypatch.setattr(service_module, "close_database_engine", fake_close)
    service = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(),
        consumer_id="owned-engine-worker",
        redis_client=redis,
    )
    try:
        await service.close()
        await service.close()
    finally:
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)

    assert closed == [integration_mysql_engine]


async def test_schema_check_failure_closes_owned_engine(
    monkeypatch: pytest.MonkeyPatch,
    integration_mysql_engine: AsyncEngine,
) -> None:
    """revision 不匹配在装配前失败，并释放本次创建的 Engine。"""
    prefix = f"sql-schema-failure-{uuid4().hex}"
    config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        database=local_mysql_config(),
        managers=ManagerAdaptersConfig(
            message=SqlManagerAdapterConfig(),
        ),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
    )
    redis = await create_redis_client(config.redis)
    closed: list[AsyncEngine] = []

    async def fake_create(_config: object) -> AsyncEngine:
        return integration_mysql_engine

    async def fail_schema(*_args: object, **_kwargs: object) -> None:
        raise DatabaseSchemaVersionError("revision mismatch")

    async def fake_close(engine: AsyncEngine) -> None:
        closed.append(engine)

    monkeypatch.setattr(service_module, "create_database_engine", fake_create)
    monkeypatch.setattr(
        service_module,
        "assert_database_schema_current",
        fail_schema,
    )
    monkeypatch.setattr(service_module, "close_database_engine", fake_close)
    try:
        with pytest.raises(DatabaseSchemaVersionError, match="revision mismatch"):
            await ServiceHost.create(
                runtime_config=config,
                project_root=TEST_PROJECT_ROOT,
                agents=(),
                consumer_id="schema-failure-worker",
                redis_client=redis,
            )
    finally:
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)

    assert closed == [integration_mysql_engine]
