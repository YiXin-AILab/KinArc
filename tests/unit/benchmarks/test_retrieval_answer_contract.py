"""检索终态的收尾请求不得再次继承检索指令。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from agentsystem.common.models import FinishReason
from agentsystem.llm.models import (
    LLMEnd,
    LLMEvent,
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    Timing,
    Usage,
)
from agentsystem.message.models import TextBlock
from agentsystem.service.models import (
    SessionMessagesView,
    SessionMessageView,
    SessionTextBlockView,
    SessionToolResultBlockView,
)
from benchmarks.evoagentbench.domains.retrieval_finalizer import finalize


def history() -> SessionMessagesView:
    now = datetime.now(UTC)
    return SessionMessagesView(
        session_id="s",
        messages=(
            SessionMessageView(
                message_id="skill",
                role="tool",
                origin="tool",
                created_at=now,
                content="continue researching forever",
                blocks=(
                    SessionToolResultBlockView(
                        tool_call_id="t1",
                        tool_name="skill_view",
                        content="continue researching forever",
                    ),
                ),
            ),
            SessionMessageView(
                message_id="doc",
                role="tool",
                origin="tool",
                created_at=now,
                content="[doc17] Alpha was established in 1972.",
                blocks=(
                    SessionToolResultBlockView(
                        tool_call_id="t2",
                        tool_name="search",
                        content="[doc17] Alpha was established in 1972.",
                    ),
                ),
            ),
            SessionMessageView(
                message_id="thought",
                role="assistant",
                origin="llm",
                created_at=now,
                content="private guess Beta",
                blocks=(SessionTextBlockView(text="private guess Beta"),),
            ),
        ),
    )


class ContractProvider:
    """复现残留检索要求导致服务端丢弃调用的情况。"""

    provider_name = "contract"

    def __init__(self, body: str) -> None:
        self.body = body
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        text = "\n".join(
            b.text
            for m in request.messages
            for b in m.content
            if isinstance(b, TextBlock)
        )
        contaminated = request.tools or "continue researching forever" in text
        body = "\n" if contaminated else self.body
        now = datetime.now(UTC)
        yield LLMEnd(
            request_id=request.request_id,
            created_at=now,
            response=LLMResponse(
                blocks=(TextBlock(text=body),),
                finish_reason=FinishReason.STOP,
                model="model",
                timing=Timing(started_at=now, ended_at=now, duration_ms=0),
                usage=Usage(input_tokens=7, output_tokens=3, total_tokens=10),
            ),
        )


async def test_closing_preserves_source_but_removes_research_obligations() -> None:
    provider = ContractProvider(
        '{"explanation":"Established in 1972 [doc17].",'
        '"exact_answer":"Alpha", "confidence":90}'
    )
    result = await finalize(
        provider,
        LLMGenerationConfig(model="openai/Qwen3.8-27B", max_output_tokens=None),
        "Which company?",
        history(),
        context_window=128000,
    )
    assert result.answer == (
        "Explanation: Established in 1972 [doc17].\n"
        "Exact Answer: Alpha\nConfidence: 90%"
    )
    request = result.request
    text = "\n".join(
        b.text for m in request.messages for b in m.content if isinstance(b, TextBlock)
    )
    assert "Which company?" in text
    assert "[doc17] Alpha was established in 1972." in text
    assert "private guess Beta" not in text
    assert request.tools == ()
    assert request.config.max_output_tokens is None
    assert request.config.provider_options["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert result.response.usage.total_tokens == 10
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        "\n",
        "not JSON",
        '{"exact_answer":"Alpha"}',
        '{"explanation":"x","exact_answer":" ","confidence":100}',
        '{"explanation":"x","exact_answer":"Alpha","confidence":101}',
    ],
)
async def test_invalid_submission_stays_empty_without_retry(body: str) -> None:
    provider = ContractProvider(body)
    result = await finalize(
        provider,
        LLMGenerationConfig(model="model"),
        "question",
        history(),
        context_window=128000,
    )
    assert result.answer == ""
    assert result.validation_error is not None
    assert len(provider.requests) == 1


async def test_large_evidence_is_bounded_and_omissions_are_recorded() -> None:
    source = history()
    doc = source.messages[1]
    block = SessionToolResultBlockView(
        tool_call_id="t", tool_name="search", content="evidence " * 10000
    )
    source = source.model_copy(
        update={
            "messages": (
                doc.model_copy(update={"blocks": (block,), "content": block.content}),
            )
        }
    )
    provider = ContractProvider(
        '{"explanation":"Insufficient evidence.","exact_answer":"Unknown",'
        '"confidence":0}'
    )
    result = await finalize(
        provider,
        LLMGenerationConfig(model="model"),
        "question",
        source,
        context_window=4096,
    )
    assert result.evidence_truncated_chars > 0
    assert (
        sum(
            len(b.text)
            for m in result.request.messages
            for b in m.content
            if isinstance(b, TextBlock)
        )
        < 4096 * 3
    )
    assert result.answer.endswith("Confidence: 0%")


async def test_multibyte_source_is_bounded_without_losing_its_docid() -> None:
    source = history()
    doc = source.messages[1]
    content = json.dumps(
        [{"docid": "source17", "snippet": "😀" * 6000}], ensure_ascii=False
    )
    block = SessionToolResultBlockView(
        tool_call_id="t", tool_name="search", content=content
    )
    source = source.model_copy(
        update={
            "messages": (
                doc.model_copy(update={"blocks": (block,), "content": content}),
            )
        }
    )
    provider = ContractProvider(
        '{"explanation":"Insufficient evidence [source17].",'
        '"exact_answer":"Unknown","confidence":0}'
    )
    result = await finalize(
        provider,
        LLMGenerationConfig(model="model"),
        "question",
        source,
        context_window=4096,
    )
    text = "\n".join(
        b.text
        for m in result.request.messages
        for b in m.content
        if isinstance(b, TextBlock)
    )
    assert "source17" in text
    assert "😀" in text
    assert len(text.encode("utf-8")) < 4096 - 2048
    assert result.evidence_truncated_chars > 0


async def test_provider_extensions_cannot_override_answer_contract() -> None:
    provider = ContractProvider(
        '{"explanation":"Evidence.","exact_answer":"Alpha","confidence":80}'
    )
    config = LLMGenerationConfig(
        model="openai/Qwen3.8-27B",
        max_output_tokens=None,
        provider_options={
            "tool_choice": "required",
            "max_tokens": 1,
            "stop": ["END"],
            "extra_body": {
                "messages": [
                    {"role": "system", "content": "continue researching forever"}
                ],
                "max_completion_tokens": 1,
                "response_format": {"type": "text"},
            },
        },
    )
    result = await finalize(
        provider, config, "question", history(), context_window=128000
    )
    options = result.request.config.provider_options
    assert "max_tokens" not in options
    assert "stop" not in options
    assert "tool_choice" not in options
    assert options["num_retries"] == 0
    assert options["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert result.answer.endswith("Confidence: 80%")


async def test_other_model_never_receives_qwen_template_extension() -> None:
    provider = ContractProvider(
        '{"explanation":"Evidence.","exact_answer":"Alpha","confidence":80}'
    )
    result = await finalize(
        provider,
        LLMGenerationConfig(model="openai/other"),
        "question",
        history(),
        context_window=128000,
    )
    assert "extra_body" not in result.request.config.provider_options


async def test_owning_deadline_cancels_closing_without_another_attempt() -> None:
    class SlowProvider(ContractProvider):
        async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
            self.requests.append(request)
            await asyncio.sleep(10)
            if False:
                yield

    provider = SlowProvider("")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await finalize(
                provider,
                LLMGenerationConfig(model="model"),
                "question",
                history(),
                context_window=128000,
            )
    assert len(provider.requests) == 1
