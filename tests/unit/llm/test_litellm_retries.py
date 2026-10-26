"""通过真实 LiteLLM/OpenAI SDK 和隔离 HTTP 传输验证配置重试边界。"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from agentsystem.config.models import ModelParamsConfig
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.models import LLMEnd, LLMStart
from agentsystem.message.models import TextBlock
from pydantic import SecretStr
from tests.unit.llm.test_litellm_provider import collect_response, request

_CHUNK = (
    b'data: {"id":"test","object":"chat.completion.chunk","created":1,'
    b'"model":"test","choices":[{"index":0,"delta":{"content":"ok"},'
    b'"finish_reason":null}]}\n\n'
)
_END = (
    b'data: {"id":"test","object":"chat.completion.chunk","created":1,'
    b'"model":"test","choices":[{"index":0,"delta":{},'
    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
)


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _CHUNK
        raise httpx.ReadError("injected stream interruption")


@pytest.mark.parametrize(
    ("retries", "status", "always_fail", "broken_stream", "succeeds"),
    [
        (0, 429, False, False, False),
        (3, 429, False, False, True),
        (0, 500, False, False, False),
        (3, 500, False, False, True),
        (3, 400, False, False, True),
        (3, 429, True, False, False),
        (3, 200, False, True, False),
    ],
)
async def test_configured_retries_at_http_boundary(
    monkeypatch: pytest.MonkeyPatch,
    retries: int,
    status: int,
    always_fail: bool,
    broken_stream: bool,
    succeeds: bool,
) -> None:
    """丢失 provider_options 透传会使暂时失败后的当前请求无法恢复。"""
    sent: list[bytes] = []

    async def send(
        client: httpx.AsyncClient, outgoing: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        del client, kwargs
        sent.append(outgoing.content)
        if status != 200 and (len(sent) == 1 or always_fail):
            return httpx.Response(
                status,
                json={"error": {"message": "injected failure", "type": "api_error"}},
                headers={"retry-after": "0.001"},
                request=outgoing,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=(
                _BrokenStream() if broken_stream else httpx.ByteStream(_CHUNK + _END)
            ),
            request=outgoing,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    config = ModelParamsConfig(
        model="openai/test",
        provider_options={"num_retries": retries},
    )
    provider = LiteLLMProvider(
        api_key=SecretStr("test-only"),
        api_base="https://retry-test.invalid/v1",
        timeout_seconds=2,
    )
    llm_request = request().model_copy(update={"config": config.to_generation_config()})
    events, response = await collect_response(provider, llm_request)

    assert (response.error is None) is succeeds
    assert sum(isinstance(event, LLMStart) for event in events) == 1
    assert sum(isinstance(event, LLMEnd) for event in events) == 1
    assert all(body == sent[0] for body in sent)
    if succeeds:
        assert len(sent) == 2
        assert response.blocks == (TextBlock(text="ok"),)
    elif always_fail:
        # LiteLLM 1.95.0 的 SDK 重试与外层重试叠加：首次 4 次，再 3 次。
        assert len(sent) == 7
    else:
        assert len(sent) == 1
