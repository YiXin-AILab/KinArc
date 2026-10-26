"""真实 Redis 下 SSE 全链路、跨节点多订阅者与断点续传验收。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    DispatcherConfig,
    ModelParamsConfig,
    PublicStreamConfig,
    RuntimeConfig,
)
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.outbound.redis_event_bus import RedisSessionEventBus
from agentsystem.redis.client import close_redis_client, create_redis_client
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import SessionSnapshot, SubmitMessageCommand
from httpx import ASGITransport, AsyncClient, Response
from litellm import ModelResponseStream
from server.app import create_runtime_api_app
from tests.contract.redis.conftest import delete_prefix
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)

_IDENTITY = {"app_id": "app", "user_id": "user", "agent_id": "stream-agent"}
_SESSION_PATH = "/v1/sessions/sse-session"


async def _completion(**_: object) -> AsyncIterator[ModelResponseStream]:
    async def chunks() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id="private-response",
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"reasoning_content": "先分析"},
                }
            ],
        )
        yield ModelResponseStream(
            id="private-response",
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"content": "最终回答"},
                }
            ],
        )
        yield ModelResponseStream(
            id="private-response",
            model="test-model",
            choices=[{"index": 0, "finish_reason": "stop", "delta": {}}],
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )

    return chunks()


def _agent() -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        agent_config=AgentConfig(
            agent_id="stream-agent",
            agent_name="Stream Agent",
            prompt="unused.md",
            output_mode=OutputMode.STREAM,
            outbound=AgentOutboundConfig(type="stream"),
            llm_model=ModelParamsConfig(model="openai/test-model"),
        ),
        base_prompt="你是测试 Agent。",
        llm_provider=LiteLLMProvider(completion=_completion),
    )


async def test_service_sse_read_outlives_base_redis_socket_timeout() -> None:
    """Service 创建的 SSE client 必须覆盖配置的 XREAD 阻塞窗口。"""
    prefix = f"sse-timeout-{uuid4().hex}"
    redis_config = local_redis_config(
        key_prefix=prefix,
        socket_timeout_seconds=0.05,
    )
    config = RuntimeConfig(
        redis=redis_config,
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
        public_stream=PublicStreamConfig(block_ms=100),
    )
    cleanup = await create_redis_client(redis_config)
    service = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(_agent(),),
        consumer_id="sse-timeout-worker",
    )
    context = SessionContext(
        **_IDENTITY,
        session_id="sse-timeout-session",
    )

    await service.start()
    try:
        await service.application.submit_message(
            context,
            SubmitMessageCommand(
                message_id="sse-timeout-message",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="开始"),),
            ),
        )
        async with asyncio.timeout(3):
            while True:
                snapshot = await service.application.snapshot(context)
                if snapshot is not None and snapshot.status.value == "succeeded":
                    break
                await asyncio.sleep(0.01)
        event_bus = RedisSessionEventBus(
            cleanup,
            cleanup,
            prefix=prefix,
            config=config.public_stream,
        )
        inspection = await event_bus.inspect(context, None)
        assert inspection.last_cursor is not None
        subscription = await service.application.open_event_stream(
            context,
            inspection.last_cursor.render(),
        )

        batch = await subscription.read_next()

        assert batch.records == ()
    finally:
        await service.close()
        await delete_prefix(cleanup, prefix)
        await close_redis_client(cleanup)


async def test_sse_replay_metrics_multi_subscriber_and_cross_node_resume() -> None:
    """两节点读取相同事件；任意 cursor 可从另一节点无缺失续传。"""
    prefix = f"sse-integration-{uuid4().hex}"
    config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
        public_stream=PublicStreamConfig(
            block_ms=10,
            heartbeat_seconds=0.01,
            max_connection_seconds=30,
        ),
    )
    writer_redis = await create_redis_client(config.redis)
    writer_events = await create_redis_client(config.redis)
    reader_redis = await create_redis_client(config.redis)
    reader_events = await create_redis_client(config.redis)
    writer = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(_agent(),),
        consumer_id="sse-writer",
        redis_client=writer_redis,
        event_stream_redis_client=writer_events,
    )
    reader = await ServiceHost.create(
        runtime_config=config,
        project_root=TEST_PROJECT_ROOT,
        agents=(_agent(),),
        consumer_id="sse-reader",
        redis_client=reader_redis,
        event_stream_redis_client=reader_events,
    )
    writer_app = create_runtime_api_app(writer)
    reader_app = create_runtime_api_app(reader)
    await writer.start()
    try:
        async with (
            AsyncClient(
                transport=ASGITransport(app=writer_app),
                base_url="http://writer.test",
            ) as writer_client,
            AsyncClient(
                transport=ASGITransport(app=reader_app),
                base_url="http://reader.test",
            ) as reader_client,
        ):
            submitted = await writer_client.post(
                f"{_SESSION_PATH}/messages",
                json={
                    **_IDENTITY,
                    "message_id": "message-1",
                    "content": [{"type": "text", "text": "开始"}],
                },
            )
            assert submitted.status_code == 202
            assert submitted.json() == {
                "session_id": "sse-session",
                "message_id": "message-1",
            }
            snapshot = await _wait_for_terminal(writer_client)

            submitted_again = await writer_client.post(
                f"{_SESSION_PATH}/messages",
                json={
                    **_IDENTITY,
                    "message_id": "message-2",
                    "content": [{"type": "text", "text": "继续"}],
                    "mode": "steer",
                },
            )
            assert submitted_again.status_code == 202
            await _wait_for_terminal(writer_client)

            async with asyncio.timeout(1):
                first = await writer_client.get(
                    f"{_SESSION_PATH}/events",
                    params=_IDENTITY,
                )
                second = await reader_client.get(
                    f"{_SESSION_PATH}/events",
                    params=_IDENTITY,
                )
            first_frames = _event_frames(first)
            second_frames = _event_frames(second)

            assert [item["id"] for item in first_frames] == [
                item["id"] for item in second_frames
            ]
            event_types = [item["event"] for item in first_frames]
            assert event_types.count("message.started") == 2
            assert event_types.count("run.completed") == 2
            started_indexes = [
                index
                for index, event_type in enumerate(event_types)
                if event_type == "message.started"
            ]
            terminal_indexes = [
                index
                for index, event_type in enumerate(event_types)
                if event_type == "run.completed"
            ]
            run_started_indexes = [
                index
                for index, event_type in enumerate(event_types)
                if event_type == "run.started"
            ]
            assert len(run_started_indexes) == 2
            assert run_started_indexes[0] < started_indexes[0] < terminal_indexes[0]
            assert (
                terminal_indexes[0]
                < run_started_indexes[1]
                < started_indexes[1]
                < terminal_indexes[1]
            )
            started_ids = [
                item["data"]["payload"]["message"]["message_id"]
                for item in first_frames
                if item["event"] == "message.started"
            ]
            assert started_ids == ["message-1", "message-2"]
            assert "message.block.delta" in event_types
            thinking = [
                item
                for item in first_frames
                if item["event"] == "message.block.delta"
                and item["data"]["payload"]["block_type"] == "thinking"
            ]
            assert thinking[0]["data"]["payload"]["delta"] == "先分析"
            terminal = [
                item for item in first_frames if item["event"] == "run.completed"
            ][-1]
            assert terminal["event"] == "run.completed"
            assert terminal["data"]["payload"]["usage"]["total_tokens"] == 3
            assert terminal["data"]["payload"]["timing"]["elapsed_ms"] >= 0
            assert terminal["data"]["payload"]["timing"]["active_ms"] >= 0
            assert snapshot.usage is not None
            assert snapshot.usage.total_tokens == 3

            midpoint = first_frames[len(first_frames) // 2]["id"]
            async with asyncio.timeout(1):
                resumed = await reader_client.get(
                    f"{_SESSION_PATH}/events",
                    params=_IDENTITY,
                    headers={"Last-Event-ID": midpoint},
                )
            resumed_frames = _event_frames(resumed)
            expected = first_frames[len(first_frames) // 2 + 1 :]
            assert [item["id"] for item in resumed_frames] == [
                item["id"] for item in expected
            ]
    finally:
        await writer.close()
        await reader.close()
        await delete_prefix(writer_redis, prefix)
        for client in (writer_redis, writer_events, reader_redis, reader_events):
            await close_redis_client(client)


async def _wait_for_terminal(client: AsyncClient) -> SessionSnapshot:
    """轮询权威快照直至 run 成功。"""
    async with asyncio.timeout(3):
        while True:
            response = await client.get(_SESSION_PATH, params=_IDENTITY)
            if response.status_code == 200 and response.json()["status"] == "succeeded":
                return SessionSnapshot.model_validate(response.json())
            await asyncio.sleep(0.01)


def _event_frames(response: Response) -> list[dict[str, Any]]:
    """解析测试响应中的持久化 SSE event frame，忽略 comment。"""
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
