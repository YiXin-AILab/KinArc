"""稳定的 Provider 语义请求大小估算测试。"""

import json
import math
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from agentsystem.common.models import JsonValue, OutputMode, SessionContext
from agentsystem.config.models import CompactionConfig
from agentsystem.context.builder import ContextBuilder
from agentsystem.context.compactor import (
    Compactor,
    context_token_request_fingerprint,
)
from agentsystem.llm.client import LLMClient
from agentsystem.llm.models import LLMEvent, LLMGenerationConfig, LLMRequest
from agentsystem.message.models import (
    AudioBlock,
    Base64Source,
    ContextMessage,
    DocumentBlock,
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMediaBlock,
    VideoBlock,
)
from agentsystem.runtime.state import (
    AgentState,
    set_context_token_checkpoint,
    start_run,
)

NOW = datetime(2026, 8, 5, tzinfo=UTC)
CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


class NoCallProvider:
    """计量测试中禁止调用的摘要 Provider。"""

    def __init__(self, provider_name: str = "no-call") -> None:
        self.provider_name = provider_name

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """任何调用都表示计量函数产生了外部副作用。"""
        del request
        raise AssertionError("count_tokens 不得调用 Provider")
        yield


def compactor(
    *,
    hard_input_limit: int = 10_000,
    provider_name: str = "no-call",
    model: str = "summary-model",
    **config_values: object,
) -> Compactor:
    """构造仅用于调用稳定计量入口的 Compactor。"""
    config = CompactionConfig(**config_values)
    max_output_tokens = 16_384
    return Compactor(
        client=cast(LLMClient, NoCallProvider(provider_name)),
        generation_config=LLMGenerationConfig(
            model=model,
            max_output_tokens=max_output_tokens,
        ),
        context_window=(
            hard_input_limit + max_output_tokens + config.safety_margin_tokens
        ),
        config=config,
    )


def message(
    *content: (
        TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock | UserMediaBlock
    ),
    role: Role = Role.USER,
    message_id: str = "message",
) -> Message:
    """构造包含给定 Provider 内容的 Message。"""
    origins = {
        Role.SYSTEM: MessageOrigin.SYSTEM,
        Role.USER: MessageOrigin.USER,
        Role.ASSISTANT: MessageOrigin.MODEL,
        Role.TOOL: MessageOrigin.TOOL,
    }
    return Message(
        message_id=message_id,
        session_id="session",
        role=role,
        origin=origins[role],
        run_id="run",
        created_at=NOW,
        content=content,
        visibility=MessageVisibility.ALL,
    )


def canonical(payload: Mapping[str, object]) -> str:
    """按生产规则序列化测试期望 payload。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_count_tokens_ignores_runtime_message_fields() -> None:
    """Message 身份和运行态字段不得影响 Provider 请求大小。"""
    first = message(TextBlock(text="你好"), message_id="one")
    second = first.model_copy(
        update={
            "message_id": "different",
            "session_id": "session-b",
            "created_at": NOW + timedelta(days=1),
        }
    )

    assert compactor().count_tokens((first,), ()) == compactor().count_tokens(
        (second,), ()
    )


def test_count_tokens_uses_canonical_provider_payload() -> None:
    """文本、ToolCall、ToolResult 和 schema 按实际发送字段计算。"""
    call = message(
        TextBlock(text="checking"),
        ToolCallBlock(
            tool_call_id="call-1",
            tool_name="lookup",
            arguments={"value": "domain-copy"},
            raw_arguments='{"value":"provider-copy"}',
        ),
        role=Role.ASSISTANT,
        message_id="assistant",
    )
    result = message(
        ToolResultBlock(
            tool_call_id="call-1",
            tool_name="lookup",
            content="result",
        ),
        role=Role.TOOL,
        message_id="tool",
    )
    tools: tuple[Mapping[str, JsonValue], ...] = (
        {"type": "function", "function": {"name": "lookup"}},
    )
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"value":"provider-copy"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ],
        "tools": [tools[0]],
    }

    assert compactor().count_tokens((call, result), tools) == math.ceil(
        len(canonical(payload)) / 4
    )


def test_count_tokens_uses_python_character_length() -> None:
    """非 ASCII 文本按 Python 字符数而非 UTF-8 bytes 估算。"""
    current = message(TextBlock(text="你好世界"), message_id="current")
    payload = {
        "messages": [{"role": "user", "content": "你好世界"}],
        "tools": [],
    }

    assert compactor().count_tokens((current,), ()) == math.ceil(
        len(canonical(payload)) / 4
    )


def test_request_count_adds_only_messages_after_usage_checkpoint() -> None:
    """真实 total token 之后只增加新 Provider Message 的估算差值。"""
    estimator = compactor()
    system = message(TextBlock(text="system"), role=Role.SYSTEM, message_id="system")
    assistant = message(
        TextBlock(text="previous answer"),
        role=Role.ASSISTANT,
        message_id="assistant",
    )
    current = message(TextBlock(text="new question"), message_id="current")
    messages = (system, assistant, current)
    fingerprint = context_token_request_fingerprint(
        provider_name="no-call",
        model="summary-model",
        messages=messages,
        tools=(),
    )
    running = set_context_token_checkpoint(
        AgentState(session_context=CONTEXT),
        tokens=40_000,
        message_id="assistant",
        request_fingerprint=fingerprint,
    )
    prefix_payload = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "previous answer"},
        ],
        "tools": [],
    }
    full_payload = {
        "messages": [
            *prefix_payload["messages"],
            {"role": "user", "content": "new question"},
        ],
        "tools": [],
    }
    expected_delta = math.ceil(len(canonical(full_payload)) / 4) - math.ceil(
        len(canonical(prefix_payload)) / 4
    )

    assert estimator.count_request_tokens(running, messages, ()) == (
        40_000 + expected_delta
    )


def test_request_count_falls_back_when_checkpoint_message_is_missing() -> None:
    """当前 Provider 请求找不到 checkpoint 时完整估算。"""
    estimator = compactor()
    messages = (message(TextBlock(text="current"), message_id="current"),)
    fingerprint = context_token_request_fingerprint(
        provider_name="no-call",
        model="summary-model",
        messages=messages,
        tools=(),
    )
    running = set_context_token_checkpoint(
        AgentState(session_context=CONTEXT),
        tokens=40_000,
        message_id="missing",
        request_fingerprint=fingerprint,
    )

    assert estimator.count_request_tokens(running, messages, ()) == (
        estimator.count_tokens(messages, ())
    )


def test_request_count_falls_back_when_tool_fingerprint_changes() -> None:
    """Tool schema 变化后不得复用旧请求的真实锚点。"""
    estimator = compactor()
    assistant = message(
        TextBlock(text="answer"),
        role=Role.ASSISTANT,
        message_id="assistant",
    )
    messages = (assistant,)
    old_tools: tuple[Mapping[str, JsonValue], ...] = (
        {"type": "function", "function": {"name": "old"}},
    )
    new_tools: tuple[Mapping[str, JsonValue], ...] = (
        {"type": "function", "function": {"name": "new"}},
    )
    fingerprint = context_token_request_fingerprint(
        provider_name="no-call",
        model="summary-model",
        messages=messages,
        tools=old_tools,
    )
    running = set_context_token_checkpoint(
        AgentState(session_context=CONTEXT),
        tokens=40_000,
        message_id="assistant",
        request_fingerprint=fingerprint,
    )

    assert estimator.count_request_tokens(running, messages, new_tools) == (
        estimator.count_tokens(messages, new_tools)
    )


@pytest.mark.parametrize(
    (
        "old_provider",
        "current_provider",
        "old_model",
        "current_model",
        "old_system_text",
        "current_system_text",
    ),
    (
        ("old-provider", "no-call", "summary-model", "summary-model", "s", "s"),
        ("no-call", "no-call", "old-model", "summary-model", "s", "s"),
        ("no-call", "no-call", "summary-model", "summary-model", "old", "new"),
    ),
)
def test_request_count_falls_back_when_request_identity_changes(
    old_provider: str,
    current_provider: str,
    old_model: str,
    current_model: str,
    old_system_text: str,
    current_system_text: str,
) -> None:
    """Provider、model 或 SYSTEM 变化都会使旧锚点失效。"""
    assistant = message(
        TextBlock(text="answer"),
        role=Role.ASSISTANT,
        message_id="assistant",
    )
    old_messages = (
        message(TextBlock(text=old_system_text), role=Role.SYSTEM),
        assistant,
    )
    current_messages = (
        message(TextBlock(text=current_system_text), role=Role.SYSTEM),
        assistant,
    )
    running = set_context_token_checkpoint(
        AgentState(session_context=CONTEXT),
        tokens=40_000,
        message_id="assistant",
        request_fingerprint=context_token_request_fingerprint(
            provider_name=old_provider,
            model=old_model,
            messages=old_messages,
            tools=(),
        ),
    )
    estimator = compactor(provider_name=current_provider, model=current_model)

    assert estimator.count_request_tokens(running, current_messages, ()) == (
        estimator.count_tokens(current_messages, ())
    )


def test_request_count_adds_projected_injection_and_tool_result_delta() -> None:
    """检查点后的 Injection 投影、ToolResult 和多条消息都计入增量。"""
    estimator = compactor()
    assistant = message(
        TextBlock(text="answer"),
        role=Role.ASSISTANT,
        message_id="assistant",
    )
    tool_result = message(
        ToolResultBlock(
            tool_call_id="call-1",
            tool_name="lookup",
            content="result",
        ),
        role=Role.TOOL,
        message_id="tool",
    )
    injection_text = (
        '<context_injection>\n<context provider="profile">\n'
        "preferred language: zh\n</context>\n</context_injection>\nquestion"
    )
    current = message(TextBlock(text=injection_text), message_id="current")
    messages = (assistant, tool_result, current)
    running = set_context_token_checkpoint(
        AgentState(session_context=CONTEXT),
        tokens=40_000,
        message_id="assistant",
        request_fingerprint=context_token_request_fingerprint(
            provider_name="no-call",
            model="summary-model",
            messages=messages,
            tools=(),
        ),
    )
    prefix_payload = {
        "messages": [{"role": "assistant", "content": "answer"}],
        "tools": [],
    }
    full_payload = {
        "messages": [
            *prefix_payload["messages"],
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            {"role": "user", "content": injection_text},
        ],
        "tools": [],
    }
    expected_delta = math.ceil(len(canonical(full_payload)) / 4) - math.ceil(
        len(canonical(prefix_payload)) / 4
    )

    assert estimator.count_request_tokens(running, messages, ()) == (
        40_000 + expected_delta
    )


def test_count_tokens_omits_thinking_not_sent_by_provider() -> None:
    """Provider 丢弃的 ThinkingBlock 不参与计量。"""
    plain = message(TextBlock(text="answer"), role=Role.ASSISTANT)
    thinking = message(
        ThinkingBlock(thinking="secret reasoning"),
        TextBlock(text="answer"),
        role=Role.ASSISTANT,
    )

    assert compactor().count_tokens((plain,), ()) == compactor().count_tokens(
        (thinking,), ()
    )


@pytest.mark.parametrize(
    "block",
    [
        ImageBlock(source=Base64Source(data="YQ==", media_type="image/png")),
        AudioBlock(source=Base64Source(data="YQ==", media_type="audio/mpeg")),
        VideoBlock(source=Base64Source(data="YQ==", media_type="video/mp4")),
        DocumentBlock(source=Base64Source(data="YQ==", media_type="application/pdf")),
    ],
)
def test_each_media_block_adds_exactly_2048(block: UserMediaBlock) -> None:
    """每个媒体 Block 只贡献固定 2048 token。"""
    plain = message(TextBlock(text="describe"))
    with_media = message(TextBlock(text="describe"), block)

    assert (
        compactor().count_tokens((with_media,), ())
        - compactor().count_tokens((plain,), ())
        == 2_048
    )


def test_media_budget_comes_from_compaction_config() -> None:
    """自定义媒体预算必须影响每个媒体 Block 的估算增量。"""
    regular = compactor(media_block_tokens=2_048)
    custom = compactor(media_block_tokens=4_096)
    plain = message(TextBlock(text="describe"))
    media = message(
        TextBlock(text="describe"),
        ImageBlock(source=Base64Source(data="YQ==", media_type="image/png")),
    )

    assert (
        custom.count_tokens((media,), ()) - custom.count_tokens((plain,), ()) == 4_096
    )
    assert (
        regular.count_tokens((media,), ()) - regular.count_tokens((plain,), ()) == 2_048
    )


def test_media_internal_length_does_not_affect_count() -> None:
    """媒体内部 Base64 长度不得进入 canonical JSON。"""
    short = message(
        ImageBlock(source=Base64Source(data="YQ==", media_type="image/png"))
    )
    long = message(
        ImageBlock(source=Base64Source(data="YQ==" * 10_000, media_type="image/png"))
    )

    assert compactor().count_tokens((short,), ()) == compactor().count_tokens(
        (long,), ()
    )


def test_should_compact_requires_trigger_and_real_action() -> None:
    """超过 trigger 仍须存在真实剪枝或摘要边界才启动压缩。"""
    regular = compactor(hard_input_limit=10_000, trigger_ratio=0.8)
    oversized = compactor(hard_input_limit=100, trigger_ratio=0.8)
    old = message(TextBlock(text="x" * 5_000), message_id="old").model_copy(
        update={"run_id": "run-old"}
    )
    current = message(TextBlock(text="current"), message_id="current").model_copy(
        update={"run_id": "run-current"}
    )
    history = ContextMessage(
        system_message=Message(
            message_id="system",
            session_id="session",
            role=Role.SYSTEM,
            origin=MessageOrigin.SYSTEM,
            run_id=None,
            created_at=NOW,
            content=(TextBlock(text="system"),),
            visibility=MessageVisibility.MODEL,
        ),
        retained_messages=(old, current),
    )
    running = start_run(
        AgentState(session_context=CONTEXT),
        run_id="run-current",
        output_mode=OutputMode.COMPLETE,
        now=NOW,
    ).model_copy(update={"current_message_ids": ("current",)})
    request_messages = ContextBuilder().build(history, running)

    assert regular.should_compact(history, running, request_messages, ()) is False
    assert oversized.should_compact(history, running, request_messages, ()) is True

    protected = ContextMessage(
        system_message=history.system_message,
        retained_messages=(
            current.model_copy(update={"content": (TextBlock(text="y" * 5_000),)}),
        ),
    )
    protected_messages = ContextBuilder().build(protected, running)
    assert oversized.should_compact(protected, running, protected_messages, ()) is False


@pytest.mark.parametrize("context_window", [18_432, 18_431])
def test_compactor_rejects_exhausted_input_budget(
    context_window: int,
) -> None:
    """非正 hard input limit 无法形成合法预算。"""
    with pytest.raises(ValueError, match="hard_input_limit"):
        Compactor(
            client=cast(LLMClient, NoCallProvider()),
            generation_config=LLMGenerationConfig(
                model="summary-model",
                max_output_tokens=16_384,
            ),
            context_window=context_window,
            config=CompactionConfig(safety_margin_tokens=2_048),
        )
