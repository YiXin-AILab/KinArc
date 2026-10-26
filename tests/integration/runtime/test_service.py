"""ServiceHost 多 Agent 装配与真实 Redis 生命周期测试。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import pytest
from agentsystem.common.models import JsonValue, OutputMode, SessionContext
from agentsystem.config.models import (
    AgentOutboundConfig,
    DispatcherConfig,
    RuntimeConfig,
)
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.outbound.redis_event_bus import RedisSessionEventBus
from agentsystem.redis.client import (
    RedisClient,
    await_redis,
    close_redis_client,
    create_redis_client,
)
from agentsystem.runtime.events import AgentEventEnvelope
from agentsystem.runtime.loop import AgentLoop
from agentsystem.service import host as service_module
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import SubmitMessageCommand
from litellm import ModelResponseStream
from opentelemetry.sdk.trace import TracerProvider
from tests.contract.redis.conftest import delete_prefix
from tests.integration.runtime.helpers import _agent_config, _context, _wait_for_success
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream


async def test_service_routes_two_agents_through_one_dispatcher() -> None:
    """同一 Service 按 agent_id 使用隔离模型，并共享 Redis/Dispatcher。"""
    calls: list[str] = []

    def provider(agent_id: str, answer: str) -> LiteLLMProvider:
        async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
            calls.append(agent_id)
            return text_stream(answer, response_id=f"response-{agent_id}")

        return LiteLLMProvider(completion=completion)

    prefix = f"multi-agent-service-{uuid4().hex}"
    runtime_config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    redis = await create_redis_client(runtime_config.redis)
    service = await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            AgentRuntimeSpec(
                agent_config=_agent_config("agent-a"),
                base_prompt="你是 Agent A。",
                llm_provider=provider("agent-a", "answer-a"),
            ),
            AgentRuntimeSpec(
                agent_config=_agent_config("agent-b"),
                base_prompt="你是 Agent B。",
                llm_provider=provider("agent-b", "answer-b"),
            ),
        ),
        redis_client=redis,
        consumer_id="multi-agent-worker",
    )

    await service.start()
    try:
        for agent_id in ("agent-a", "agent-b"):
            ctx = _context(agent_id)
            await service.application.submit_message(
                ctx,
                SubmitMessageCommand(
                    message_id=f"message-{agent_id}",
                    role=InboundRole.USER,
                    origin="user",
                    content=(TextBlock(text="开始"),),
                ),
            )
        first = await _wait_for_success(service, _context("agent-a"))
        second = await _wait_for_success(service, _context("agent-b"))
    finally:
        await service.close()
        await close_redis_client(redis)

    assert first.result is not None and first.result.content == "answer-a"
    assert second.result is not None and second.result.content == "answer-b"
    assert sorted(calls) == ["agent-a", "agent-b"]
    assert service.agent_ids == ("agent-a", "agent-b")


async def test_service_builds_an_isolated_loop_for_each_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 Agent 的每个 run 都获得独立 AgentLoop。"""
    created_loops: list[object] = []
    real_loop = AgentLoop

    def capture_loop(**kwargs: Any) -> object:
        loop = real_loop(**kwargs)
        created_loops.append(loop)
        return loop

    monkeypatch.setattr(service_module, "AgentLoop", capture_loop)

    prefix = f"isolated-loop-{uuid4().hex}"

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        return text_stream(
            "完成",
            response_id=f"response-{uuid4().hex}",
        )

    runtime_config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    redis = await create_redis_client(runtime_config.redis)
    service = await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            AgentRuntimeSpec(
                agent_config=_agent_config("isolated-agent"),
                base_prompt="测试 loop 隔离。",
                llm_provider=LiteLLMProvider(completion=completion),
            ),
        ),
        redis_client=redis,
        consumer_id="isolated-loop-worker",
    )
    ctx = _context("isolated-agent")

    await service.start()
    try:
        await service.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="message-1",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="第一次"),),
            ),
        )
        await _wait_for_success(service, ctx)
        await service.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="message-2",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="第二次"),),
            ),
        )
        async with asyncio.timeout(3):
            while len(created_loops) < 2:
                await asyncio.sleep(0.01)
    finally:
        await service.close()
        await close_redis_client(redis)

    assert len(created_loops) == 2
    assert created_loops[0] is not created_loops[1]


async def test_service_rejects_removed_callback_and_closes_owned_resources_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分装配失败必须聚合错误并单次关闭全部自有资源。"""
    runtime_config = RuntimeConfig(
        redis=local_redis_config(key_prefix=f"assembly-failure-{uuid4().hex}"),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
    )
    closed_redis: list[object] = []
    closed_assets = 0

    class CountingTracerProvider(TracerProvider):
        def __init__(self) -> None:
            super().__init__()
            self.force_flush_calls = 0
            self.shutdown_calls = 0

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            assert timeout_millis > 0
            self.force_flush_calls += 1
            return True

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    provider = CountingTracerProvider()
    original_close_redis = close_redis_client
    original_close_assets = service_module._close_assets

    async def close_redis_once(client: RedisClient) -> None:
        closed_redis.append(client)
        await original_close_redis(client)

    async def close_assets_once(
        assets: service_module._AssetInfrastructure,
    ) -> None:
        nonlocal closed_assets
        closed_assets += 1
        await original_close_assets(assets)

    monkeypatch.setattr(service_module, "close_redis_client", close_redis_once)
    monkeypatch.setattr(service_module, "_close_assets", close_assets_once)

    with pytest.raises(
        ExceptionGroup,
        match="Agent Runtime 装配失败",
    ) as captured:
        await ServiceHost.create(
            runtime_config=runtime_config,
            project_root=TEST_PROJECT_ROOT,
            agents=(
                AgentRuntimeSpec(
                    agent_config=_agent_config("valid-agent"),
                    base_prompt="valid",
                ),
                AgentRuntimeSpec(
                    agent_config=_agent_config(
                        "broken-agent",
                        outbound_type="callback",
                    ),
                    base_prompt="broken",
                ),
            ),
            tracer_provider=provider,
            consumer_id="assembly-failure-worker",
        )

    assert [str(error) for error in captured.value.exceptions] == [
        "Agent 装配失败：broken-agent (KeyError)"
    ]
    assert len(closed_redis) == 2
    assert len({id(client) for client in closed_redis}) == 2
    assert closed_assets == 1
    assert provider.shutdown_calls == 1


async def test_service_replica_does_not_consume_unregistered_agent_stream() -> None:
    """异构副本只消费自身 Registry 中的 Agent Ready Stream。"""
    calls: list[str] = []

    def spec(agent_id: str) -> AgentRuntimeSpec:
        async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
            calls.append(agent_id)
            return text_stream(agent_id, response_id=f"response-{agent_id}")

        return AgentRuntimeSpec(
            agent_config=_agent_config(agent_id),
            base_prompt=agent_id,
            llm_provider=LiteLLMProvider(completion=completion),
        )

    runtime_config = RuntimeConfig(
        redis=local_redis_config(key_prefix=f"heterogeneous-service-{uuid4().hex}"),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    first_redis = await create_redis_client(runtime_config.redis)
    second_redis = await create_redis_client(runtime_config.redis)
    first = await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(spec("agent-a"),),
        redis_client=first_redis,
        consumer_id="worker-a",
    )
    second = await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(spec("agent-b"),),
        redis_client=second_redis,
        consumer_id="worker-b",
    )
    ctx = _context("agent-a")
    await first.application.submit_message(
        ctx,
        SubmitMessageCommand(
            message_id="message-a",
            role=InboundRole.USER,
            origin="user",
            content=(TextBlock(text="开始"),),
        ),
    )

    await second.start()
    try:
        await asyncio.sleep(0.05)
        assert calls == []
        await first.start()
        await _wait_for_success(first, ctx)
    finally:
        await first.close()
        await second.close()
        await close_redis_client(first_redis)
        await close_redis_client(second_redis)

    assert calls == ["agent-a"]


async def test_stream_outbound_uses_event_bus_without_complete_callback() -> None:
    """stream 输出器独立于 COMPLETE 生成模式发布可重放事件。"""

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        return text_stream("stream-answer", response_id="stream-response")

    prefix = f"stream-service-{uuid4().hex}"
    runtime_config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
        dispatcher=DispatcherConfig(
            ready_block_ms=10,
            heartbeat_interval_seconds=0.1,
        ),
    )
    runtime_redis = await create_redis_client(runtime_config.redis)
    event_redis = await create_redis_client(runtime_config.redis)
    service = await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            AgentRuntimeSpec(
                agent_config=_agent_config(
                    "stream-agent",
                    output_mode=OutputMode.COMPLETE,
                    outbound_type="stream",
                ),
                base_prompt="stream agent",
                llm_provider=LiteLLMProvider(completion=completion),
            ),
        ),
        redis_client=runtime_redis,
        event_stream_redis_client=event_redis,
        consumer_id="stream-worker",
    )
    ctx = _context("stream-agent")
    await service.start()
    try:
        await service.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="stream-message",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="开始"),),
            ),
        )
        await _wait_for_success(service, ctx)
        event_bus = RedisSessionEventBus(
            runtime_redis,
            event_redis,
            prefix=prefix,
            config=runtime_config.public_stream,
        )
        batch = await event_bus.read(ctx, None, count=100, block_ms=0)
        event_types = [record.event.type for record in batch.records]

        assert "message.block.delta" in event_types
        assert event_types[-1] == "run.completed"
    finally:
        await service.close()
        assert await await_redis(runtime_redis.ping())
        assert await await_redis(event_redis.ping())
        await delete_prefix(runtime_redis, prefix)
        await close_redis_client(runtime_redis)
        await close_redis_client(event_redis)


async def test_service_uses_registered_outbound_type_with_stream_generation() -> None:
    """STREAM 生成模式也必须按 outbound.type 选择自定义输出器。"""
    created: list[Mapping[str, JsonValue]] = []

    class Projector:
        async def project(
            self,
            ctx: SessionContext,
            event: AgentEventEnvelope,
        ) -> None:
            del ctx, event

    def factory(options: Mapping[str, JsonValue]) -> Projector:
        created.append(options)
        return Projector()

    prefix = f"custom-outbound-{uuid4().hex}"
    runtime_config = RuntimeConfig(
        redis=local_redis_config(key_prefix=prefix),
        asset=TEST_ASSET_LIMITS,
        asset_store=TEST_ASSET_STORE,
    )
    redis = await create_redis_client(runtime_config.redis)
    events = await create_redis_client(runtime_config.redis)
    config = _agent_config("custom-agent", output_mode=OutputMode.STREAM)
    config = config.model_copy(
        update={
            "outbound": AgentOutboundConfig(
                type="capture",
                options={"topic": "agent-events"},
            )
        }
    )

    service = await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(AgentRuntimeSpec(agent_config=config, base_prompt="custom"),),
        consumer_id="custom-outbound-worker",
        redis_client=redis,
        event_stream_redis_client=events,
        outbound_projectors={"capture": factory},
    )
    try:
        assert created == [{"topic": "agent-events"}]
    finally:
        await service.close()
        await close_redis_client(redis)
        await close_redis_client(events)
