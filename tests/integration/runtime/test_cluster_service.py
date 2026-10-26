"""原生 Redis Cluster 上的 API→Dispatcher→SSE 完整链路。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import pytest
from agentsystem.common.models import OutputMode
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    DispatcherConfig,
    ModelParamsConfig,
    PublicStreamConfig,
    RuntimeConfig,
)
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from httpx import ASGITransport, AsyncClient, Response
from litellm import ModelResponseStream
from redis.asyncio.cluster import RedisCluster
from server.app import create_runtime_api_app
from tests.contract.redis.conftest import delete_cluster_prefix
from tests.redis_config import cluster_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream


def _agent(agent_id: str) -> AgentRuntimeSpec:
    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        return text_stream(
            f"answer-{agent_id}",
            response_id=f"response-{agent_id}",
        )

    return AgentRuntimeSpec(
        agent_config=AgentConfig(
            agent_id=agent_id,
            agent_name=f"{agent_id} name",
            prompt="unused.md",
            output_mode=OutputMode.STREAM,
            outbound=AgentOutboundConfig(type="stream"),
            llm_model=ModelParamsConfig(model="openai/test-model"),
        ),
        base_prompt=f"你是 {agent_id}。",
        llm_provider=LiteLLMProvider(completion=completion),
    )


async def test_cluster_runtime_executes_two_agents_and_resumes_sse() -> None:
    """两 Agent 应跨 Cluster slot 执行，并可从 SSE cursor 无缺失续读。"""
    prefix = f"cluster-runtime-{uuid4().hex}"
    redis_config = cluster_redis_config(key_prefix=prefix)
    if redis_config is None:
        pytest.skip("需要 TEST_REDIS_CLUSTER_NODES")
    cleanup = cast(RedisCluster, await create_redis_client(redis_config))
    service = await ServiceHost.create(
        project_root=TEST_PROJECT_ROOT,
        runtime_config=RuntimeConfig(
            redis=redis_config,
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.1,
            ),
            public_stream=PublicStreamConfig(
                block_ms=10,
                heartbeat_seconds=0.01,
                max_connection_seconds=0.05,
            ),
        ),
        agents=(_agent("agent-a"), _agent("agent-b")),
        consumer_id="cluster-runtime-worker",
    )
    app = create_runtime_api_app(service)

    await service.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://cluster-runtime.test",
        ) as client:
            identities = {
                agent_id: {
                    "app_id": "app",
                    "user_id": "user",
                    "agent_id": agent_id,
                }
                for agent_id in ("agent-a", "agent-b")
            }
            for agent_id, identity in identities.items():
                response = await client.post(
                    f"/v1/sessions/session-{agent_id}/messages",
                    json={
                        **identity,
                        "message_id": f"message-{agent_id}",
                        "content": [{"type": "text", "text": f"question-{agent_id}"}],
                    },
                )
                assert response.status_code == 202

            await asyncio.gather(
                *(
                    _wait_for_success(client, agent_id, identity)
                    for agent_id, identity in identities.items()
                )
            )
            all_frames: dict[str, list[dict[str, Any]]] = {}
            for agent_id, identity in identities.items():
                events = await client.get(
                    f"/v1/sessions/session-{agent_id}/events",
                    params=identity,
                )
                frames = _event_frames(events)
                all_frames[agent_id] = frames
                assert frames[-1]["event"] == "run.completed"
                messages = await client.get(
                    f"/v1/sessions/session-{agent_id}/messages",
                    params=identity,
                )
                assert [item["content"] for item in messages.json()["messages"]] == [
                    f"question-{agent_id}",
                    f"answer-{agent_id}",
                ]

            frames = all_frames["agent-a"]
            midpoint = len(frames) // 2
            resumed = await client.get(
                "/v1/sessions/session-agent-a/events",
                params=identities["agent-a"],
                headers={"Last-Event-ID": frames[midpoint]["id"]},
            )
            assert [item["id"] for item in _event_frames(resumed)] == [
                item["id"] for item in frames[midpoint + 1 :]
            ]
    finally:
        await service.close()
        await delete_cluster_prefix(cleanup, prefix)
        await close_redis_client(cleanup)


async def _wait_for_success(
    client: AsyncClient,
    agent_id: str,
    identity: dict[str, str],
) -> None:
    async with asyncio.timeout(3):
        while True:
            response = await client.get(
                f"/v1/sessions/session-{agent_id}",
                params=identity,
            )
            if response.status_code == 200 and response.json()["status"] == "succeeded":
                return
            await asyncio.sleep(0.01)


def _event_frames(response: Response) -> list[dict[str, Any]]:
    assert response.status_code == 200
    frames: list[dict[str, Any]] = []
    for raw_frame in response.text.split("\n\n"):
        if not raw_frame or raw_frame.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in raw_frame.splitlines())
        if fields.get("event") == "stream.rotate":
            continue
        frames.append(
            {
                "id": fields["id"],
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        )
    return frames
