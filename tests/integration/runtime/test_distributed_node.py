"""多个 ServiceHost 副本共享真实 Redis 的分布式执行测试。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import uuid4

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    DispatcherConfig,
    ModelParamsConfig,
    RuntimeConfig,
)
from agentsystem.inbox.models import InboundRole
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.message.models import TextBlock
from agentsystem.redis.client import (
    RedisClient,
    close_redis_client,
    create_redis_client,
)
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import CancelCommand, SubmitMessageCommand
from litellm import ModelResponseStream
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream

type Completion = Callable[..., Awaitable[AsyncIterator[ModelResponseStream]]]


@pytest.fixture
async def distributed_redis() -> AsyncIterator[RedisClient]:
    """连接本机 Docker Redis DB 15。"""
    client = await create_redis_client(local_redis_config())
    try:
        yield client
    finally:
        await close_redis_client(client)


async def test_two_nodes_compete_but_execute_session_once(
    distributed_redis: RedisClient,
) -> None:
    """两个独立 Redis client 的 Worker 竞争时只产生一次模型调用。"""
    prefix = f"distributed-once-{uuid4().hex}"
    calls = 0

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return text_stream("single execution")

    second_client = await create_redis_client(local_redis_config())
    try:
        first = await _node(
            distributed_redis,
            prefix,
            "worker-1",
            completion,
        )
        second = await _node(
            second_client,
            prefix,
            "worker-2",
            completion,
        )
        ctx = _context("same-session")
        await first.application.submit_message(ctx, _request("message-1", "first"))
        await first.application.submit_message(ctx, _request("message-2", "second"))

        await asyncio.gather(first.start(), second.start())
        try:
            await _wait_succeeded(first, ctx)
        finally:
            await asyncio.gather(first.close(), second.close())

        assert calls == 1
        public_messages = await first.application.list_messages(ctx)
        assert [item.content for item in public_messages.messages] == [
            "first",
            "second",
            "single execution",
        ]
    finally:
        await close_redis_client(second_client)


async def test_different_sessions_execute_concurrently(
    distributed_redis: RedisClient,
) -> None:
    """无批次屏障时两个不同 session 必须能同时进入 LLM。"""
    prefix = f"distributed-concurrent-{uuid4().hex}"
    active = 0
    maximum_active = 0
    entered = 0
    both_entered = asyncio.Event()

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal active, entered, maximum_active
        active += 1
        entered += 1
        maximum_active = max(maximum_active, active)
        if entered == 2:
            both_entered.set()
        try:
            await asyncio.wait_for(both_entered.wait(), timeout=1.0)
            return text_stream("done")
        finally:
            active -= 1

    node = await _node(
        distributed_redis,
        prefix,
        "worker-concurrent",
        completion,
    )
    first_ctx = _context("session-a")
    second_ctx = _context("session-b")
    await node.application.submit_message(first_ctx, _request("message-a", "a"))
    await node.application.submit_message(second_ctx, _request("message-b", "b"))

    await node.start()
    try:
        await asyncio.gather(
            _wait_succeeded(node, first_ctx),
            _wait_succeeded(node, second_ctx),
        )
    finally:
        await node.close()

    assert maximum_active == 2


async def test_retried_public_message_is_idempotent(
    distributed_redis: RedisClient,
) -> None:
    """同一 message_id/content 的公开重试不得因服务端时间戳产生冲突或重放。"""
    prefix = f"distributed-idempotent-{uuid4().hex}"
    calls = 0

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal calls
        calls += 1
        return text_stream("once")

    node = await _node(
        distributed_redis,
        prefix,
        "worker-idempotent",
        completion,
    )
    ctx = _context("idempotent-session")
    request = _request("same-message", "same-content")

    await node.application.submit_message(ctx, request)
    await node.application.submit_message(ctx, request)
    await node.start()
    try:
        await _wait_succeeded(node, ctx)
    finally:
        await node.close()

    assert calls == 1
    messages = await node.application.list_messages(ctx)
    assert [item.content for item in messages.messages] == ["same-content", "once"]


async def test_api_node_cancels_run_owned_by_another_worker(
    distributed_redis: RedisClient,
) -> None:
    """API 节点 A 的 Pub/Sub 命令应取消实际执行节点 B 的模型等待。"""
    prefix = f"distributed-cancel-{uuid4().hex}"
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

    api_client = await create_redis_client(local_redis_config())
    try:
        api_node = await _node(api_client, prefix, "worker-api", completion)
        execution_node = await _node(
            distributed_redis,
            prefix,
            "worker-execution",
            completion,
        )
        ctx = _context("cross-node-cancel")
        await api_node.application.submit_message(
            ctx,
            _request("message", "cancel me"),
        )
        await execution_node.start()
        await asyncio.wait_for(model_started.wait(), timeout=2)
        await api_node.start()
        try:
            status = await api_node.application.cancel(ctx, CancelCommand())
            assert status.value == "accepted"
            await _wait_status(api_node, ctx, SessionStatus.CANCELED)
            await asyncio.wait_for(stream_closed.wait(), timeout=1)
        finally:
            await asyncio.gather(api_node.close(), execution_node.close())
    finally:
        await close_redis_client(api_client)


async def _node(
    client: RedisClient,
    prefix: str,
    consumer_id: str,
    completion: Completion,
) -> ServiceHost:
    return await ServiceHost.create(
        project_root=TEST_PROJECT_ROOT,
        runtime_config=RuntimeConfig(
            redis=local_redis_config(key_prefix=prefix),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.01,
            ),
        ),
        agents=(
            AgentRuntimeSpec(
                agent_config=AgentConfig(
                    agent_id="agent",
                    agent_name="Distributed Agent",
                    prompt="unused.yaml",
                    outbound=AgentOutboundConfig(
                        type="log",
                    ),
                    llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
                ),
                base_prompt="你是分布式测试助手。",
                llm_provider=LiteLLMProvider(completion=completion),
            ),
        ),
        redis_client=client,
        consumer_id=consumer_id,
    )


async def _wait_succeeded(node: ServiceHost, ctx: SessionContext) -> None:
    await _wait_status(node, ctx, SessionStatus.SUCCEEDED)


async def _wait_status(
    node: ServiceHost,
    ctx: SessionContext,
    expected: SessionStatus,
) -> None:
    async with asyncio.timeout(3):
        while True:
            snapshot = await node.application.snapshot(ctx)
            if snapshot is not None and snapshot.status is expected:
                return
            await asyncio.sleep(0.01)


def _context(session_id: str) -> SessionContext:
    return SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id=session_id,
    )


def _request(message_id: str, content: str) -> SubmitMessageCommand:
    return SubmitMessageCommand(
        message_id=message_id,
        role=InboundRole.USER,
        origin="user",
        content=(TextBlock(text=content),),
    )
