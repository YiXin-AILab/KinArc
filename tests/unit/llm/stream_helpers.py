"""使用真实 LiteLLM chunk 类型的可复用测试事件流。"""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from litellm import ModelResponseStream


def text_stream(
    content: str,
    *,
    response_id: str = "response",
    model: str = "deepseek-v4-flash",
    usage: Mapping[str, int] | None = None,
) -> AsyncIterator[ModelResponseStream]:
    """返回包含文本 delta 和终态 usage 的真实 SDK 异步流。"""

    async def generate() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id=response_id,
            model=model,
            choices=[
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"content": content},
                }
            ],
        )
        yield ModelResponseStream(
            id=response_id,
            model=model,
            choices=[{"index": 0, "finish_reason": "stop", "delta": {}}],
            usage=dict(usage or {}),
        )

    return generate()


def tool_stream(
    calls: Sequence[Mapping[str, Any]],
    *,
    response_id: str = "tool-response",
    model: str = "deepseek-v4-flash",
) -> AsyncIterator[ModelResponseStream]:
    """返回包含完整 tool-call delta 的真实 SDK 异步流。"""

    async def generate() -> AsyncIterator[ModelResponseStream]:
        yield ModelResponseStream(
            id=response_id,
            model=model,
            choices=[
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"tool_calls": list(calls)},
                }
            ],
        )
        yield ModelResponseStream(
            id=response_id,
            model=model,
            choices=[{"index": 0, "finish_reason": "tool_calls", "delta": {}}],
            usage={},
        )

    return generate()
