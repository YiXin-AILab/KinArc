"""真实 Redis 下从 HTTP 提交到公开终态的端到端测试。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    DispatcherConfig,
    ModelParamsConfig,
    RuntimeConfig,
)
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from httpx import ASGITransport, AsyncClient
from litellm import ModelResponseStream
from server.app import create_runtime_api_app
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream


async def test_http_submission_reaches_sanitized_public_result() -> None:
    """同一 HTTP Service 根据 agent_id 完成两条隔离运行链路。"""
    prefix = f"e2e-api-{uuid4().hex}"
    redis = await create_redis_client(local_redis_config())

    def agent(agent_id: str, answer: str) -> AgentRuntimeSpec:
        async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
            return text_stream(
                answer,
                response_id=f"private-{agent_id}-response-id",
            )

        return AgentRuntimeSpec(
            agent_config=AgentConfig(
                agent_id=agent_id,
                agent_name=f"{agent_id} name",
                prompt="unused.yaml",
                outbound=AgentOutboundConfig(
                    type="log",
                ),
                llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
            ),
            base_prompt=f"你是 {agent_id}。",
            llm_provider=LiteLLMProvider(completion=completion),
        )

    service = await ServiceHost.create(
        project_root=TEST_PROJECT_ROOT,
        runtime_config=RuntimeConfig(
            redis=local_redis_config(key_prefix=prefix),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.1,
            ),
        ),
        agents=(
            agent("agent-a", "answer-a"),
            agent("agent-b", "answer-b"),
        ),
        redis_client=redis,
        consumer_id="e2e-worker",
    )
    app = create_runtime_api_app(service)

    await service.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            identities = {
                agent_id: {"app_id": "app", "user_id": "user", "agent_id": agent_id}
                for agent_id in ("agent-a", "agent-b")
            }
            submitted = [
                await client.post(
                    f"/v1/sessions/session-{agent_id}/messages",
                    json={
                        **identity,
                        "message_id": f"message-{agent_id}",
                        "content": [{"type": "text", "text": "执行端到端测试"}],
                    },
                )
                for agent_id, identity in identities.items()
            ]
            missing = await client.post(
                "/v1/sessions/session-missing/messages",
                json={
                    "app_id": "app",
                    "user_id": "user",
                    "agent_id": "missing",
                    "message_id": "message-missing",
                    "content": [{"type": "text", "text": "不应入队"}],
                },
            )
            assert [response.status_code for response in submitted] == [202, 202]
            assert [response.json() for response in submitted] == [
                {
                    "session_id": "session-agent-a",
                    "message_id": "message-agent-a",
                },
                {
                    "session_id": "session-agent-b",
                    "message_id": "message-agent-b",
                },
            ]
            assert missing.status_code == 404
            assert missing.json() == {
                "code": "agent_not_found",
                "message": "Agent 不存在",
                "error_id": None,
            }

            snapshots = {
                agent_id: await _wait_for_success(client, agent_id, identity)
                for agent_id, identity in identities.items()
            }

            assert snapshots["agent-a"]["result"]["content"] == "answer-a"
            assert snapshots["agent-b"]["result"]["content"] == "answer-b"
            messages = await client.get(
                "/v1/sessions/session-agent-a/messages",
                params=identities["agent-a"],
            )
            public_messages = messages.json()["messages"]
            assert [item["role"] for item in public_messages] == [
                "user",
                "assistant",
            ]
            assert public_messages[0]["message_id"] == "message-agent-a"
            assert public_messages[-1]["content"] == "answer-a"
            serialized = str(snapshots)
            assert "run_id" not in serialized and "trace_id" not in serialized
            assert "private-agent" not in serialized
    finally:
        await service.close()
        await close_redis_client(redis)


async def test_http_cancel_interrupts_model_and_exposes_canceled_snapshot() -> None:
    """HTTP → Pub/Sub → Run Task → AgentSession 应形成完整 CANCELED 链路。"""
    prefix = f"e2e-cancel-{uuid4().hex}"
    redis = await create_redis_client(local_redis_config())
    model_started = asyncio.Event()
    stream_closed = asyncio.Event()

    class BlockingStream:
        def __aiter__(self) -> "BlockingStream":
            return self

        async def __anext__(self) -> ModelResponseStream:
            model_started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            stream_closed.set()

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        return BlockingStream()

    service = await ServiceHost.create(
        project_root=TEST_PROJECT_ROOT,
        runtime_config=RuntimeConfig(
            redis=local_redis_config(key_prefix=prefix),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.1,
            ),
        ),
        agents=(
            AgentRuntimeSpec(
                agent_config=AgentConfig(
                    agent_id="agent",
                    agent_name="Cancel Agent",
                    prompt="unused.yaml",
                    outbound=AgentOutboundConfig(type="log"),
                    llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
                ),
                base_prompt="等待取消。",
                llm_provider=LiteLLMProvider(completion=completion),
            ),
        ),
        redis_client=redis,
        consumer_id="e2e-cancel-worker",
    )
    app = create_runtime_api_app(service)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}

    await service.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://runtime.test",
        ) as client:
            submitted = await client.post(
                "/v1/sessions/cancel-session/messages",
                json={
                    **identity,
                    "message_id": "message",
                    "content": [{"type": "text", "text": "请等待"}],
                },
            )
            assert submitted.status_code == 202
            await asyncio.wait_for(model_started.wait(), timeout=2)

            canceled = await client.post(
                "/v1/sessions/cancel-session/cancel",
                json=identity,
            )
            assert canceled.status_code == 202
            assert canceled.json() == {
                "session_id": "cancel-session",
                "status": "accepted",
            }
            snapshot = await _wait_for_status(
                client,
                "cancel-session",
                identity,
                "canceled",
            )
            assert snapshot["result"]["error_code"] == "run_canceled"
            assert "run_id" not in str(snapshot)
            await asyncio.wait_for(stream_closed.wait(), timeout=1)
    finally:
        await service.close()
        await close_redis_client(redis)


async def _wait_for_success(
    client: AsyncClient,
    agent_id: str,
    identity: dict[str, str],
) -> dict[str, Any]:
    """等待指定 Agent Session 进入成功终态。"""
    async with asyncio.timeout(3):
        while True:
            response = await client.get(
                f"/v1/sessions/session-{agent_id}",
                params=identity,
            )
            if response.status_code == 200:
                snapshot: dict[str, Any] = response.json()
                if snapshot["status"] == "succeeded":
                    return snapshot
            await asyncio.sleep(0.01)


async def _wait_for_status(
    client: AsyncClient,
    session_id: str,
    identity: dict[str, str],
    expected: str,
) -> dict[str, Any]:
    """等待指定 Session 进入目标状态并返回公开快照。"""
    async with asyncio.timeout(3):
        while True:
            response = await client.get(
                f"/v1/sessions/{session_id}",
                params=identity,
            )
            if response.status_code == 200:
                snapshot: dict[str, Any] = response.json()
                if snapshot["status"] == expected:
                    return snapshot
            await asyncio.sleep(0.01)
