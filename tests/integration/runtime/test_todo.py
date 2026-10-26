"""真实 Application/Session/Redis 消息链中的 Todo 与提醒。"""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from agentsystem.config.models import RuntimeConfig
from agentsystem.database.client import close_database_engine, create_database_engine
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import SubmitMessageCommand
from agentsystem.todo.sql.schema import runtime_todos
from litellm import ModelResponseStream
from server.app import create_api_app
from sqlalchemy import delete
from tests.contract.redis.conftest import delete_prefix
from tests.integration.runtime.helpers import (
    _agent_config,
    _context,
    _wait_for_success,
)
from tests.mysql_config import require_local_mysql_config
from tests.redis_config import local_redis_config
from tests.runtime_config import TEST_ASSET_LIMITS, TEST_ASSET_STORE
from tests.unit.llm.stream_helpers import text_stream, tool_stream

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_REDIS_URL"), reason="需要 TEST_REDIS_URL"
)


@pytest.mark.parametrize("backend", ["local", "redis", "db"])
@pytest.mark.parametrize("write_enabled", [True, False])
async def test_tools_reminder_and_query_through_application(
    tmp_path: Path, backend: str, write_enabled: bool
) -> None:
    prefix = "todo-e2e-" + uuid4().hex
    config = RuntimeConfig.model_validate(
        {
            "redis": local_redis_config(key_prefix=prefix),
            "asset": TEST_ASSET_LIMITS,
            "asset_store": TEST_ASSET_STORE,
            "todo": {"store": {"type": backend}},
            **({"database": require_local_mysql_config()} if backend == "db" else {}),
            "dispatcher": {"ready_block_ms": 10},
        }
    )
    prompts: list[str] = []
    step = 0

    async def completion(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal step
        prompts.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        available = json.dumps(kwargs.get("tools"))
        assert ("todo_write" in available) == write_enabled
        current = step
        step += 1
        if current >= 11:
            return text_stream("done", response_id=f"response-{current}")
        name = "todo_write" if write_enabled and current == 0 else "todo_read"
        arguments = (
            {"todos": [{"content": "live task <x>", "status": "in_progress"}]}
            if name == "todo_write"
            else {}
        )
        return tool_stream(
            [
                {
                    "index": 0,
                    "id": f"call-{current}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
            response_id=f"response-{current}",
        )

    agent = _agent_config("todo-agent").model_copy(
        update={
            "enabled_tools": ("todo_read", "todo_write")
            if write_enabled
            else ("todo_read",),
            "enabled_middlewares": ("todo-reminder",),
        }
    )
    redis = await create_redis_client(config.redis)
    host = await ServiceHost.create(
        runtime_config=config,
        project_root=tmp_path,
        consumer_id="todo-worker",
        redis_client=redis,
        agents=(
            AgentRuntimeSpec(
                agent_config=agent,
                base_prompt="base",
                llm_provider=LiteLLMProvider(completion=completion),
            ),
        ),
    )
    ctx = _context("todo-agent").model_copy(update={"session_id": prefix})
    try:
        await host.start()
        await host.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id=uuid4().hex,
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="go"),),
            ),
        )
        await _wait_for_success(host, ctx)
        assert len(prompts) == 12
        assert not any("<system-reminder>" in prompt for prompt in prompts[:11])
        assert ("<system-reminder>" in prompts[-1]) == write_enabled
        public = await host.application.list_messages(ctx)
        assert "<system-reminder>" not in str(public)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api_app(host.application)),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/v1/sessions/{ctx.session_id}/todos",
                params={
                    "app_id": ctx.app_id,
                    "user_id": ctx.user_id,
                    "agent_id": ctx.agent_id,
                },
            )
            assert response.status_code == 200
            assert len(response.json()["todos"]) == (1 if write_enabled else 0)
            response = await client.get(
                "/v1/sessions/not-created/todos",
                params={
                    "app_id": ctx.app_id,
                    "user_id": ctx.user_id,
                    "agent_id": ctx.agent_id,
                },
            )
            assert response.json() == {"todos": []}
    finally:
        await host.close()
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)
        if backend == "db":
            assert config.database is not None
            engine = await create_database_engine(config.database)
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        delete(runtime_todos).where(
                            runtime_todos.c.session_id == ctx.session_id
                        )
                    )
            finally:
                await close_database_engine(engine)
