"""标题生成使用公开文本、有限调用且不产生 Runtime 副作用。"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import ServiceError
from agentsystem.common.models import FinishReason, OutputMode
from agentsystem.llm.client import LLMClient
from agentsystem.llm.models import (
    LLMEnd,
    LLMEvent,
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    LLMStart,
    MediaTransport,
    Timing,
)
from agentsystem.message.models import (
    CompactionBlock,
    Message,
    MessageVisibility,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)
from agentsystem.service.application import _generate_title as generate_title
from tests.unit.llm.test_client import RecordingProvider
from tests.unit.llm.test_client import _client as make_client

NOW = datetime(2026, 9, 24, tzinfo=UTC)


class TitleProvider(RecordingProvider):
    """提供可控响应和等待行为，所有模型调用离线执行。"""

    def __init__(self, text: str = "生成标题", delay: float = 0) -> None:
        self.text = text
        self.delay = delay
        self.requests: list[LLMRequest] = []
        self.reason = FinishReason.STOP

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        yield LLMStart(
            request_id=request.request_id,
            created_at=NOW,
            model=request.config.model,
            output_mode=request.output_mode,
        )
        await asyncio.sleep(self.delay)
        yield LLMEnd(
            request_id=request.request_id,
            created_at=NOW,
            response=LLMResponse(
                blocks=(TextBlock(text=self.text),),
                timing=Timing(started_at=NOW, ended_at=NOW, duration_ms=0),
                model="test",
                finish_reason=self.reason,
            ),
        )


def _client(provider: TitleProvider) -> LLMClient:
    return make_client(provider, transport=MediaTransport.AUTO)


def message(
    text: str,
    *,
    role: Role = Role.USER,
    visible: MessageVisibility = MessageVisibility.ALL,
) -> Message:
    return Message(
        message_id=text[:10] or "m",
        session_id="s",
        role=role,
        origin="user",
        run_id="existing-run",
        created_at=NOW,
        visibility=visible,
        content=(TextBlock(text=text),),
    )


async def test_generation_uses_only_first_user_message() -> None:
    provider = TitleProvider("  中文标题  ")
    assistant = message("公开答复", role=Role.ASSISTANT).model_copy(
        update={
            "content": (ThinkingBlock(thinking="隐藏思考"), TextBlock(text="公开答复"))
        }
    )
    history = (
        message("秘密系统", role=Role.SYSTEM),
        message("内部注入", visible=MessageVisibility.MODEL),
        message("压缩摘要").model_copy(
            update={
                "origin": "compactor",
                "content": (
                    CompactionBlock(
                        action="summarized",
                        summary="摘要",
                        compacted_message_ids=("old",),
                    ),
                ),
            }
        ),
        message("公开问题").model_copy(update={"origin": "web"}),
        assistant,
        *(message("x" * 2000) for _ in range(8)),
    )
    result = await generate_title(
        _client(provider),
        LLMGenerationConfig(model="test"),
        history,
        now=NOW,
        timeout_seconds=1,
    )
    assert result == "中文标题"
    request = provider.requests[0]
    text = str(request.messages)
    assert "隐藏思考" not in text and "秘密系统" not in text and "内部注入" not in text
    assert request.messages[1].content == (TextBlock(text="公开问题"),)
    assert "公开答复" not in text and "xxxx" not in text
    assert request.output_mode is OutputMode.COMPLETE and request.tools == ()
    assert [item.role for item in request.messages] == [Role.SYSTEM, Role.USER]


@pytest.mark.parametrize("max_output_tokens", [None, 512, 16384])
async def test_title_request_ends_with_user_even_after_assistant_reply(
    max_output_tokens: int | None,
) -> None:
    """对话最后是助手回复时，仍显式发送新的标题任务输入。"""
    provider = TitleProvider()
    await generate_title(
        _client(provider),
        LLMGenerationConfig(model="test", max_output_tokens=max_output_tokens),
        (message("问题"), message("答复", role=Role.ASSISTANT)),
        now=NOW,
        timeout_seconds=1,
    )
    assert provider.requests[0].messages[-1].role is Role.USER
    assert provider.requests[0].config.max_output_tokens == min(
        max_output_tokens or 1024, 1024
    )


@pytest.mark.parametrize("text", ["", "  ", "x" * 17, "中" * 17, "x" * 201])
async def test_invalid_generated_title(text: str) -> None:
    with pytest.raises(ServiceError) as error:
        await generate_title(
            _client(TitleProvider(text)),
            LLMGenerationConfig(model="test"),
            (message("hello"),),
            now=NOW,
            timeout_seconds=1,
        )
    assert error.value.code == "title_generation_failed"


@pytest.mark.parametrize("text", ["x" * 16, "中" * 16, "😀" * 16])
async def test_generated_title_accepts_16_unicode_characters(text: str) -> None:
    assert (
        await generate_title(
            _client(TitleProvider(text)),
            LLMGenerationConfig(model="test"),
            (message("用户问题"),),
            now=NOW,
            timeout_seconds=1,
        )
        == text
    )


@pytest.mark.parametrize(
    "history",
    [
        (),
        (message("只有助手", role=Role.ASSISTANT),),
        (message("   "), message("后续消息不能替代首条")),
    ],
)
async def test_missing_first_user_text_does_not_call_model(
    history: tuple[Message, ...],
) -> None:
    provider = TitleProvider()
    with pytest.raises(ServiceError) as error:
        await generate_title(
            _client(provider),
            LLMGenerationConfig(model="test"),
            history,
            now=NOW,
            timeout_seconds=1,
        )
    assert error.value.code == "title_context_empty"
    assert not provider.requests


async def test_first_user_text_is_bounded() -> None:
    provider = TitleProvider()
    await generate_title(
        _client(provider),
        LLMGenerationConfig(model="test"),
        (message("中" * 1500),),
        now=NOW,
        timeout_seconds=1,
    )
    assert provider.requests[0].messages[1].content == (TextBlock(text="中" * 1000),)


async def test_timeout_and_cancellation() -> None:
    provider = TitleProvider(delay=1)
    with pytest.raises(ServiceError) as error:
        await generate_title(
            _client(provider),
            LLMGenerationConfig(model="test"),
            (message("hello"),),
            now=NOW,
            timeout_seconds=0.01,
        )
    assert error.value.code == "title_generation_timeout"
    task = asyncio.create_task(
        generate_title(
            _client(provider),
            LLMGenerationConfig(model="test"),
            (message("hello"),),
            now=NOW,
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "reason", [FinishReason.LENGTH, FinishReason.CONTENT_FILTER, FinishReason.ABORTED]
)
async def test_incomplete_output_is_rejected(reason: FinishReason) -> None:
    provider = TitleProvider("不能保存")
    provider.reason = reason
    with pytest.raises(ServiceError) as error:
        await generate_title(
            _client(provider),
            LLMGenerationConfig(model="test"),
            (message("问题"),),
            now=NOW,
            timeout_seconds=1,
        )
    assert error.value.code == "title_generation_failed"


async def test_tool_response_is_rejected_without_execution() -> None:
    class ToolProvider(TitleProvider):
        async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
            yield LLMEnd(
                request_id=request.request_id,
                created_at=NOW,
                response=LLMResponse(
                    blocks=(
                        ToolCallBlock(
                            tool_call_id="call",
                            tool_name="danger",
                            arguments={},
                            raw_arguments="{}",
                        ),
                    ),
                    timing=Timing(started_at=NOW, ended_at=NOW, duration_ms=0),
                    model="test",
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
            )

    with pytest.raises(ServiceError) as error:
        await generate_title(
            _client(ToolProvider()),
            LLMGenerationConfig(model="test"),
            (message("问题"),),
            now=NOW,
            timeout_seconds=1,
        )
    assert error.value.code == "title_generation_failed"
