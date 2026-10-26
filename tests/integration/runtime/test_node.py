"""ServiceHost 在真实 Redis 上的装配与生命周期测试。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

import pytest
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    DispatcherConfig,
    ModelParamsConfig,
    RuntimeConfig,
)
from agentsystem.inbox.models import InboundRole
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.provider import LLMProvider
from agentsystem.message.models import TextBlock, ToolCallBlock
from agentsystem.outbound.mapper import to_public_run_result
from agentsystem.outbound.models import PublicRunResult
from agentsystem.outbound.projector_registry import PublicProjectorFactory
from agentsystem.redis.client import (
    RedisClient,
    await_redis,
    close_redis_client,
    create_redis_client,
)
from agentsystem.runtime.events import AgentEventEnvelope
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from agentsystem.service.models import ResumeCommand, SubmitMessageCommand
from agentsystem.tool.authorization import ToolAuthorization, ToolAuthorizationDecision
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource
from httpx import ASGITransport, AsyncClient
from litellm import ModelResponseStream
from server.app import create_runtime_api_app
from tests.redis_config import local_redis_config
from tests.runtime_config import (
    TEST_ASSET_LIMITS,
    TEST_ASSET_STORE,
    TEST_PROJECT_ROOT,
)
from tests.unit.llm.stream_helpers import text_stream, tool_stream


@pytest.fixture
async def runtime_redis() -> AsyncIterator[RedisClient]:
    """连接本机 Docker Redis，并在测试结束后关闭连接池。"""
    client = await create_redis_client(local_redis_config())
    try:
        yield client
    finally:
        await close_redis_client(client)


async def test_runtime_node_executes_message_and_preserves_external_redis(
    runtime_redis: RedisClient,
) -> None:
    """完整装配应完成消息执行，且不接管外部 Redis client 的所有权。"""
    prefix = f"node-test-{uuid4().hex}"

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        return text_stream(
            "你好，我已经完成处理。",
            response_id="response-1",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
            },
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
    agent_config = AgentConfig(
        agent_id="agent",
        agent_name="测试 Agent",
        prompt="unused-in-explicit-prompt-mode.yaml",
        outbound=AgentOutboundConfig(
            type="log",
        ),
        llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
    )
    node = await _runtime_service(
        runtime_config=runtime_config,
        agent_config=agent_config,
        base_prompt="你是一个可靠的测试助手。",
        redis_client=runtime_redis,
        llm_provider=LiteLLMProvider(completion=completion),
        consumer_id="node-test",
    )
    ctx = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="session",
    )

    await node.start()
    try:
        await node.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="message-1",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="你好"),),
            ),
        )
        async with asyncio.timeout(3):
            while True:
                snapshot = await node.application.snapshot(ctx)
                if snapshot is not None and snapshot.status is SessionStatus.SUCCEEDED:
                    break
                await asyncio.sleep(0.01)
    finally:
        await node.close()

    assert snapshot is not None
    assert snapshot.result is not None
    assert snapshot.result.content == "你好，我已经完成处理。"
    assert await await_redis(runtime_redis.ping()) is True


async def test_runtime_node_selects_code_registered_public_projector(
    runtime_redis: RedisClient,
) -> None:
    """AgentConfig 只选择实现名称，不包含 callback URL 等实现配置。"""
    projected: list[PublicRunResult] = []

    class Projector:
        async def project(
            self,
            ctx: SessionContext,
            event: AgentEventEnvelope,
        ) -> None:
            assert ctx.session_id == "callback-session"
            result = to_public_run_result(event)
            if result is not None:
                projected.append(result)

    def factory(options: Mapping[str, JsonValue]) -> Projector:
        assert options == {}
        return Projector()

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        return text_stream("完成", response_id="callback-response")

    node = await _runtime_service(
        runtime_config=RuntimeConfig(
            redis=local_redis_config(key_prefix=f"node-callback-{uuid4().hex}"),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.1,
            ),
        ),
        agent_config=AgentConfig(
            agent_id="agent",
            agent_name="Callback Agent",
            prompt="unused.yaml",
            outbound=AgentOutboundConfig(
                type="custom",
            ),
            llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
        ),
        base_prompt="你是 Callback 测试助手。",
        redis_client=runtime_redis,
        llm_provider=LiteLLMProvider(completion=completion),
        outbound_projectors={"custom": factory},
        consumer_id="node-callback-test",
    )
    ctx = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="callback-session",
    )

    await node.start()
    try:
        await node.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="callback-user",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="开始"),),
            ),
        )
        await _wait_for_status(node, ctx, SessionStatus.SUCCEEDED)
    finally:
        await node.close()

    assert [result.status for result in projected] == [SessionStatus.SUCCEEDED]
    snapshot = await node.application.snapshot(ctx)
    assert snapshot is not None
    assert snapshot.result is not None
    assert snapshot.result.content == "完成"


async def test_runtime_node_fastapi_lifespan_starts_and_closes_dispatcher(
    runtime_redis: RedisClient,
) -> None:
    """HTTP 只写 MQ，lifespan 启动 Dispatcher 后才执行 Agent。"""
    prefix = f"node-api-test-{uuid4().hex}"
    completion_calls = 0

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal completion_calls
        completion_calls += 1
        return text_stream("API 执行完成", response_id="response-api")

    node = await _runtime_service(
        runtime_config=RuntimeConfig(
            redis=local_redis_config(key_prefix=prefix),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.1,
            ),
        ),
        agent_config=AgentConfig(
            agent_id="agent",
            agent_name="API Agent",
            prompt="unused.yaml",
            outbound=AgentOutboundConfig(
                type="log",
            ),
            llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
        ),
        base_prompt="你是 API 测试助手。",
        redis_client=runtime_redis,
        llm_provider=LiteLLMProvider(completion=completion),
        consumer_id="node-api-test",
    )
    app = create_runtime_api_app(node)
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        accepted = await client.post(
            "/v1/sessions/session-api/messages",
            json={
                **identity,
                "message_id": "message-api",
                "content": [{"type": "text", "text": "执行"}],
            },
        )
        assert accepted.status_code == 202
        assert completion_calls == 0
        assert (
            await client.get("/v1/sessions/session-api", params=identity)
        ).status_code == 404
        assert (
            await await_redis(
                runtime_redis.xlen(InboxRedisKeys(prefix).ready(identity["agent_id"]))
            )
            == 1
        )

        async with app.router.lifespan_context(app):
            async with asyncio.timeout(3):
                while True:
                    snapshot = await client.get(
                        "/v1/sessions/session-api",
                        params=identity,
                    )
                    if snapshot.status_code == 200 and snapshot.json()["status"] == (
                        "succeeded"
                    ):
                        break
                    await asyncio.sleep(0.01)
            messages = await client.get(
                "/v1/sessions/session-api/messages",
                params=identity,
            )
        assert completion_calls == 1
        assert messages.status_code == 200
        assert [item["role"] for item in messages.json()["messages"]] == [
            "user",
            "assistant",
        ]
        assert all(
            "run_id" not in item and "thinking" not in item
            for item in messages.json()["messages"]
        )

    with pytest.raises(RuntimeError, match="已关闭"):
        await node.start()


async def test_runtime_node_resumes_suspended_tool_through_real_redis(
    runtime_redis: RedisClient,
) -> None:
    """PENDING token 经 ResumeReady 恢复同一工具批次并最终成功。"""
    prefix = f"node-resume-test-{uuid4().hex}"
    completions = 0
    tool_calls = 0

    async def completion(**_: object) -> AsyncIterator[ModelResponseStream]:
        nonlocal completions
        completions += 1
        if completions == 1:
            return tool_stream(
                [
                    {
                        "index": 0,
                        "id": "approval-call",
                        "type": "function",
                        "function": {
                            "name": "approval",
                            "arguments": "{}",
                        },
                    }
                ],
                response_id="tool-response",
            )
        return text_stream("已恢复完成", response_id="final-response")

    class Handler:
        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            nonlocal tool_calls
            assert ctx.session_id == "resume-session"
            assert arguments == {}
            tool_calls += 1
            return ToolOutput(content="approved")

    class SuspendOnce:
        def __init__(self) -> None:
            self.calls = 0

        async def authorize(
            self,
            ctx: SessionContext,
            call: ToolCallBlock,
            definition: ToolDefinition,
        ) -> ToolAuthorizationDecision:
            del ctx, call, definition
            self.calls += 1
            if self.calls == 1:
                return ToolAuthorizationDecision.suspend("opaque-resume-token")
            return ToolAuthorizationDecision.allow()

    approval_tool = ToolDefinition(
        name="approval",
        description="需要一次恢复的审批工具",
        parameters={"type": "object"},
        source=ToolSource.BUILTIN,
        handler=Handler(),
        can_suspend=True,
    )
    authorization = SuspendOnce()
    node = await _runtime_service(
        runtime_config=RuntimeConfig(
            redis=local_redis_config(key_prefix=prefix),
            asset=TEST_ASSET_LIMITS,
            asset_store=TEST_ASSET_STORE,
            dispatcher=DispatcherConfig(
                ready_block_ms=10,
                heartbeat_interval_seconds=0.1,
            ),
        ),
        agent_config=AgentConfig(
            agent_id="agent",
            agent_name="Resume Agent",
            prompt="unused.yaml",
            outbound=AgentOutboundConfig(
                type="log",
            ),
            llm_model=ModelParamsConfig(model="openai/deepseek-v4-flash"),
        ),
        base_prompt="你是恢复测试助手。",
        redis_client=runtime_redis,
        llm_provider=LiteLLMProvider(completion=completion),
        external_tools=(approval_tool,),
        tool_authorization=authorization,
        consumer_id="node-resume-test",
    )
    ctx = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="resume-session",
    )

    await node.start()
    try:
        await node.application.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="resume-user",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="请审批"),),
            ),
        )
        await _wait_for_status(node, ctx, SessionStatus.PENDING)
        pending = await node.application.snapshot(ctx)
        assert pending is not None
        assert pending.result is not None
        assert pending.result.resume_token == "opaque-resume-token"
        await node.application.resume(
            ctx,
            ResumeCommand(
                resume_id="resume-request",
                resume_token="opaque-resume-token",
                input={"approved": True},
            ),
        )
        await _wait_for_status(node, ctx, SessionStatus.SUCCEEDED)
    finally:
        await node.close()

    assert completions == 2
    assert authorization.calls == 2
    assert tool_calls == 1
    messages = await node.application.list_messages(ctx)
    assert [item.role for item in messages.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages.messages[1].blocks[0].type == "tool_call"


async def _runtime_service(
    *,
    runtime_config: RuntimeConfig,
    agent_config: AgentConfig,
    base_prompt: str,
    redis_client: RedisClient,
    llm_provider: LLMProvider,
    consumer_id: str,
    outbound_projectors: Mapping[str, PublicProjectorFactory] | None = None,
    external_tools: tuple[ToolDefinition, ...] = (),
    tool_authorization: ToolAuthorization | None = None,
) -> ServiceHost:
    """把单 Agent 测试参数装配为正式多 Agent Service。"""
    return await ServiceHost.create(
        runtime_config=runtime_config,
        project_root=TEST_PROJECT_ROOT,
        agents=(
            AgentRuntimeSpec(
                agent_config=agent_config,
                base_prompt=base_prompt,
                external_tools=external_tools,
                llm_provider=llm_provider,
            ),
        ),
        redis_client=redis_client,
        outbound_projectors=outbound_projectors,
        tool_authorization=tool_authorization,
        consumer_id=consumer_id,
    )


async def _wait_for_status(
    node: ServiceHost,
    ctx: SessionContext,
    status: SessionStatus,
) -> None:
    async with asyncio.timeout(3):
        while True:
            snapshot = await node.application.snapshot(ctx)
            if snapshot is not None and snapshot.status is status:
                return
            await asyncio.sleep(0.01)
