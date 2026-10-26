"""环境变量选定模型的真实 LiteLLM 集成测试。"""

import os
from datetime import UTC, datetime

import pytest
from agentsystem.common.models import FinishReason, OutputMode
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.models import LLMEnd, LLMGenerationConfig, LLMRequest, LLMResponse
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
)
from pydantic import SecretStr

_MODEL = os.environ.get("TEST_AGENT_LLM_MODEL")
_API_KEY = os.environ.get("TEST_AGENT_LLM_API_KEY")
_BASE_URL = os.environ.get("TEST_AGENT_LLM_BASE_URL")
pytestmark = pytest.mark.skipif(
    not all((_MODEL, _API_KEY, _BASE_URL)),
    reason="需要通过 TEST_AGENT_LLM_MODEL/API_KEY/BASE_URL 显式启用真实模型测试",
)


async def generate_response(
    provider: LiteLLMProvider,
    request: LLMRequest,
) -> LLMResponse:
    """消费真实 Provider 事件流并返回唯一终态响应。"""
    response: LLMResponse | None = None
    async for event in provider.generate(request):
        if isinstance(event, LLMEnd):
            response = event.response
    assert response is not None
    return response


async def test_live_completion_matches_normalized_contract() -> None:
    """真实 API 必须返回可归一化的文本、usage 和结束原因。"""
    system = Message(
        message_id="system",
        session_id="live-test",
        role=Role.SYSTEM,
        origin=MessageOrigin.SYSTEM,
        created_at=datetime.now(UTC),
        content=(TextBlock(text="You are a helpful assistant"),),
        visibility=MessageVisibility.MODEL,
    )
    user = Message(
        message_id="user",
        session_id="live-test",
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="live-run",
        created_at=datetime.now(UTC),
        content=(TextBlock(text="Reply with exactly: Hello"),),
        visibility=MessageVisibility.ALL,
    )
    request = LLMRequest(
        request_id="live-request",
        messages=(system, user),
        config=LLMGenerationConfig(
            model=_MODEL or "missing",
        ),
        output_mode=OutputMode.COMPLETE,
    )
    provider = LiteLLMProvider(
        api_key=SecretStr(_API_KEY or "missing"),
        api_base=_BASE_URL,
        timeout_seconds=120,
    )

    response = await generate_response(provider, request)

    assert response.finish_reason is FinishReason.STOP, response.error
    assert any(
        isinstance(block, TextBlock) and block.text.strip() for block in response.blocks
    )
    assert response.usage.total_tokens > 0
    assert response.response_id


async def test_live_tool_call_matches_normalized_contract() -> None:
    """真实 API 的 tool_calls 必须保留调用 ID、名称和 JSON 参数。"""
    user = Message(
        message_id="tool-user",
        session_id="live-tool-test",
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="live-tool-run",
        created_at=datetime.now(UTC),
        content=(TextBlock(text="必须调用 get_weather 查询上海天气，不要直接回答。"),),
        visibility=MessageVisibility.ALL,
    )
    request = LLMRequest(
        request_id="live-tool-request",
        messages=(user,),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询指定城市天气",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            },
        ),
        config=LLMGenerationConfig(
            model=_MODEL or "missing",
        ),
        output_mode=OutputMode.COMPLETE,
    )
    provider = LiteLLMProvider(
        api_key=SecretStr(_API_KEY or "missing"),
        api_base=_BASE_URL,
        timeout_seconds=120,
    )

    response = await generate_response(provider, request)

    assert response.finish_reason is FinishReason.TOOL_CALLS, response.error
    calls = [block for block in response.blocks if isinstance(block, ToolCallBlock)]
    assert len(calls) == 1
    assert calls[0].tool_call_id
    assert calls[0].tool_name == "get_weather"
    assert calls[0].arguments is not None
    assert calls[0].arguments.get("city") in {"上海", "Shanghai", "上海市"}
