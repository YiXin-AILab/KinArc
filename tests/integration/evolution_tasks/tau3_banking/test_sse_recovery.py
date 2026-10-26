"""显式 opt-in：真实 Redis、Runtime 与 HTTP 协议的跨回合过期恢复。"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    DispatcherConfig,
    PublicStreamConfig,
    RuntimeConfig,
)
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.outbound.redis_event_keys import PublicEventRedisKeys
from agentsystem.redis.client import (
    await_redis,
    close_redis_client,
    create_redis_client,
)
from agentsystem.service.host import ServiceHost
from evolution_tasks.tau3_banking._internal.adapter import AgentsystemHttpSession
from litellm import ModelResponseStream
from server.app import create_runtime_api_app
from tests.contract.redis.conftest import delete_prefix
from tests.integration.runtime.test_sse_service import _agent, _completion
from tests.redis_config import local_redis_config
from tests.runtime_config import TEST_ASSET_LIMITS, TEST_ASSET_STORE, TEST_PROJECT_ROOT


async def test_adapter_recovers_expired_redis_epoch_without_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧终态 TTL 到期后仍在同一 session 继续，每条输入只触发一次 run。"""
    if not os.environ.get("TEST_REDIS_URL"):
        pytest.skip("需要显式 TEST_REDIS_URL")
    prefix = f"tau3-sse-recovery-{uuid4().hex}"
    config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(ready_block_ms=10, heartbeat_interval_seconds=0.1),
        public_stream=PublicStreamConfig(
            terminal_grace_seconds=1,
            block_ms=10,
            heartbeat_seconds=0.01,
        ),
    )
    model_calls = 0

    async def completion(**kwargs: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal model_calls
        model_calls += 1
        return await _completion(**kwargs)

    redis = await create_redis_client(config.redis)
    host = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            replace(_agent(), llm_provider=LiteLLMProvider(completion=completion)),
        ),
        consumer_id="tau3-sse-recovery",
    )
    ctx = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="stream-agent",
        session_id="case",
    )
    keys = PublicEventRedisKeys(prefix)
    loop = asyncio.get_running_loop()
    expired_responses = 0
    submissions = 0
    await host.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_runtime_api_app(host)),
        ) as client:

            async def dispatch(request: httpx.Request) -> httpx.Response:
                nonlocal expired_responses, submissions
                if request.method == "POST" and request.url.path.endswith("/messages"):
                    submissions += 1
                response = await client.send(request)
                if response.status_code == 400:
                    assert response.json()["code"] == "cursor_expired"
                    expired_responses += 1
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=response.content,
                )

            def transport(request: httpx.Request) -> httpx.Response:
                return asyncio.run_coroutine_threadsafe(dispatch(request), loop).result(
                    10
                )

            client_type = httpx.Client
            monkeypatch.setattr(
                "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
                lambda **kwargs: client_type(
                    transport=httpx.MockTransport(transport), **kwargs
                ),
            )
            session = AgentsystemHttpSession(
                base_url="http://runtime.test",
                app_id=ctx.app_id,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                timeout_seconds=20,
            )
            first = await asyncio.to_thread(session.submit, "first")
            async with asyncio.timeout(3):
                # 一条命令观察双 key，避免多次读取恰好跨过 TTL 到期点。
                while await await_redis(
                    redis.exists(keys.stream(ctx), keys.metadata(ctx))
                ):
                    await asyncio.sleep(0.02)
            second = await asyncio.to_thread(session.submit, "second")
            history = await host.application.list_messages(ctx)

            assert first == second == "最终回答"
            assert expired_responses == 1
            assert submissions == model_calls == 2
            assert len(history.messages) == 4
            assert (
                sum(e["type"] == "run.completed" for e in session.candidate_events) == 2
            )
    finally:
        await host.close()
        await delete_prefix(redis, prefix)
        await close_redis_client(redis)
