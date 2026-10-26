"""Compactor 单 cutoff、剪枝快速路径和 prefix-cache 摘要契约测试。"""

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from agentsystem.common.models import (
    FinishReason,
    JsonValue,
    OutputMode,
    SessionContext,
)
from agentsystem.config.models import CompactionConfig
from agentsystem.context.builder import ContextBuilder
from agentsystem.context.compactor import (
    CompactionResult,
    Compactor,
    context_token_request_fingerprint,
)
from agentsystem.llm.client import LLMClient
from agentsystem.llm.models import (
    LLMEnd,
    LLMEvent,
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    Timing,
    Usage,
)
from agentsystem.message.models import (
    AssetSource,
    CompactionBlock,
    ContextMessage,
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    CompactionMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.runtime.state import (
    AgentState,
    set_context_token_checkpoint,
    start_run,
)
from pydantic import ValidationError

_NOW = datetime(2026, 8, 12, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)
_SUMMARY = "new summary"


class RecordingProvider:
    """记录请求并返回固定摘要响应。"""

    provider_name = "summary-provider"

    def __init__(self, summary: str = _SUMMARY) -> None:
        self.summary = summary
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """只产生摘要调用唯一可信的 LLMEnd。"""
        self.requests.append(request)
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=LLMResponse(
                blocks=(TextBlock(text=self.summary),),
                usage=Usage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    cache_read_tokens=7,
                    cache_write_tokens=3,
                ),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
                model=request.config.model,
                finish_reason=FinishReason.STOP,
                response_id="summary-response",
            ),
        )


class ResponseProvider:
    """返回调用方指定的完整响应。"""

    provider_name = "response-provider"

    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """发送指定终态以验证摘要响应契约。"""
        self.requests.append(request)
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=self.response,
        )


class FailingProvider:
    """模拟真实 Provider 调用失败。"""

    provider_name = "failing-provider"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """记录调用后抛出原始 Provider 错误。"""
        self.requests.append(request)
        raise RuntimeError("provider overflow")
        yield


def text_message(
    message_id: str,
    role: Role,
    text: str,
    *,
    run_id: str | None,
) -> Message:
    """构造普通文本 Message。"""
    origins = {
        Role.SYSTEM: MessageOrigin.SYSTEM,
        Role.USER: MessageOrigin.USER,
        Role.ASSISTANT: MessageOrigin.MODEL,
    }
    return Message(
        message_id=message_id,
        session_id="session",
        role=role,
        content=(TextBlock(text=text),),
        origin=origins[role],
        run_id=run_id,
        created_at=_NOW,
        stop_reason=FinishReason.STOP if role is Role.ASSISTANT else None,
        visibility=MessageVisibility.MODEL
        if role is Role.SYSTEM and run_id is None
        else MessageVisibility.ALL,
    )


def tool_round(
    suffix: str,
    payload: str,
    *,
    run_id: str,
) -> tuple[Message, Message]:
    """构造一组闭合 ToolCall/ToolResult。"""
    call_id = f"call-{suffix}"
    return (
        Message(
            message_id=f"assistant-{suffix}",
            session_id="session",
            role=Role.ASSISTANT,
            content=(
                ToolCallBlock(
                    tool_call_id=call_id,
                    tool_name="lookup",
                    arguments={"payload": payload},
                    raw_arguments='{"payload":"' + payload + '"}',
                ),
            ),
            origin=MessageOrigin.MODEL,
            run_id=run_id,
            created_at=_NOW,
            stop_reason=FinishReason.TOOL_CALLS,
            visibility=MessageVisibility.ALL,
        ),
        Message(
            message_id=f"tool-{suffix}",
            session_id="session",
            role=Role.TOOL,
            content=(
                ToolResultBlock(
                    tool_call_id=call_id,
                    tool_name="lookup",
                    content=payload,
                ),
            ),
            origin=MessageOrigin.TOOL,
            run_id=run_id,
            created_at=_NOW,
            visibility=MessageVisibility.ALL,
        ),
    )


def state(*current_ids: str) -> AgentState:
    """构造当前 run 状态。"""
    return start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-current",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    ).model_copy(update={"current_message_ids": current_ids})


def source(
    retained: tuple[Message, ...],
    compaction: Message | None = None,
) -> ContextMessage:
    """构造固定 ContextMessage。"""
    return ContextMessage(
        system_message=text_message("system", Role.SYSTEM, "system", run_id=None),
        latest_compaction_message=compaction,
        retained_messages=retained,
    )


def compaction_message(
    block: CompactionBlock,
    *,
    model: str | None = None,
) -> Message:
    """构造已持久化压缩快照。"""
    return Message(
        message_id="previous-compaction",
        session_id="session",
        role=Role.USER,
        content=(block,),
        origin=MessageOrigin.COMPACTOR,
        model=model,
        run_id="run-current",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )


ClientDouble = RecordingProvider | ResponseProvider | FailingProvider


def make_compactor(
    client: ClientDouble,
    *,
    hard_input_limit: int = 10_000,
    generation_config: LLMGenerationConfig | None = None,
    middleware_manager: MiddlewareManager | None = None,
    include_asset_id_in_context: bool = False,
    **config_values: object,
) -> Compactor:
    """用可读 hard limit 构造采用新公开接口的 Compactor。"""
    config = CompactionConfig(**config_values)
    generation = generation_config or LLMGenerationConfig(model="summary-model")
    return Compactor(
        client=cast(LLMClient, client),
        generation_config=generation,
        context_window=(
            hard_input_limit
            + (generation.max_output_tokens or 0)
            + config.safety_margin_tokens
        ),
        config=config,
        include_asset_id_in_context=include_asset_id_in_context,
        middleware_manager=middleware_manager,
        clock=lambda: _NOW,
        id_factory=lambda: f"request-{len(client.requests)}",
    )


async def compact(
    compactor: Compactor,
    *,
    context: ContextMessage,
    running: AgentState,
    request_messages: tuple[Message, ...],
    tools: tuple[Mapping[str, JsonValue], ...] = (),
) -> CompactionResult | None:
    """调用新 Compactor 完整入口。"""
    return await compactor.compact(
        ctx=_CONTEXT,
        run_id="run-current",
        trace_id="trace-current",
        source=context,
        state=running,
        request_messages=request_messages,
        tools=tools,
    )


def result_block(result: CompactionResult) -> CompactionBlock:
    """读取结果中的唯一 CompactionBlock。"""
    value = result.compaction_message.content[0]
    assert isinstance(value, CompactionBlock)
    return value


def require_result(result: CompactionResult | None) -> CompactionResult:
    """把预期存在的压缩结果收窄为非空类型。"""
    assert result is not None
    return result


def simple_history() -> tuple[ContextMessage, AgentState, tuple[Message, ...]]:
    """返回存在一个可摘要前缀的最小历史。"""
    old = text_message("old", Role.USER, "x" * 1_000, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, current))
    running = state("current")
    return context, running, ContextBuilder().build(context, running)


def test_compaction_result_rejects_none_action() -> None:
    """只有实际剪枝或摘要才能形成压缩结果。"""
    with pytest.raises(ValidationError):
        CompactionResult.model_validate(
            {
                "action": "none",
                "request_messages": (),
                "before_tokens": 10,
                "after_tokens": 10,
            }
        )


def test_compaction_result_rejects_empty_request_messages() -> None:
    """压缩结果必须保留至少一条可作为 checkpoint 的请求消息。"""
    with pytest.raises(ValidationError):
        CompactionResult(
            action="pruned",
            request_messages=(),
            before_tokens=10,
            after_tokens=5,
            compaction_message=compaction_message(
                CompactionBlock(
                    action="pruned",
                    pruned_message_ids=("old",),
                )
            ),
        )


async def test_recent_budget_selects_one_continuous_message_tail() -> None:
    """20% 预算必须从末尾选择唯一连续 tail。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "x" * 1_000, run_id="run-old")
    nearby = text_message("nearby", Role.ASSISTANT, "n" * 100, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, nearby, current))
    running = state("current")

    result = require_result(
        await compact(
            make_compactor(provider, hard_input_limit=1_000),
            context=context,
            running=running,
            request_messages=ContextBuilder().build(context, running),
        )
    )

    assert result_block(result).compacted_message_ids == ("old",)
    assert result_block(result).next_retained_message_id == "nearby"
    assert tuple(message.message_id for message in result.request_messages[-2:]) == (
        "nearby",
        "current",
    )


async def test_current_messages_force_an_oversized_continuous_tail() -> None:
    """最早 current Message 到末尾优先于 20% 软预算完整保留。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "old" * 400, run_id="run-old")
    current = text_message("current", Role.USER, "current" * 400, run_id="run-current")
    answer = text_message(
        "answer", Role.ASSISTANT, "answer" * 400, run_id="run-current"
    )
    context = source((old, current, answer))
    running = state("current")

    result = require_result(
        await compact(
            make_compactor(provider, hard_input_limit=500),
            context=context,
            running=running,
            request_messages=ContextBuilder().build(context, running),
        )
    )

    assert result_block(result).compacted_message_ids == ("old",)
    assert result_block(result).next_retained_message_id == "current"
    assert tuple(message.message_id for message in result.request_messages[-2:]) == (
        "current",
        "answer",
    )


async def test_cutoff_inside_tool_round_keeps_the_whole_round_in_tail() -> None:
    """理论 cutoff 命中 ToolCall/ToolResult 中间时整轮进入近端 tail。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "old" * 400, run_id="run-old")
    assistant, tool = tool_round("boundary", "z" * 50, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, assistant, tool, current))
    running = state("current")

    result = require_result(
        await compact(
            make_compactor(provider, hard_input_limit=500),
            context=context,
            running=running,
            request_messages=ContextBuilder().build(context, running),
        )
    )

    assert result_block(result).compacted_message_ids == ("old",)
    assert result_block(result).next_retained_message_id == "assistant-boundary"
    assert tuple(message.message_id for message in result.request_messages[-3:]) == (
        "assistant-boundary",
        "tool-boundary",
        "current",
    )


async def test_multicall_batch_is_kept_as_one_tail_unit() -> None:
    """一个 Assistant 的多 ToolCall 和全部结果必须同进同出。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "old" * 400, run_id="run-old")
    assistant = Message(
        message_id="assistant-batch",
        session_id="session",
        role=Role.ASSISTANT,
        content=tuple(
            ToolCallBlock(
                tool_call_id=f"call-{suffix}",
                tool_name="lookup",
                arguments={"value": suffix},
                raw_arguments=f'{{"value":"{suffix}"}}',
            )
            for suffix in ("a", "b")
        ),
        origin=MessageOrigin.MODEL,
        run_id="run-old",
        created_at=_NOW,
        stop_reason=FinishReason.TOOL_CALLS,
        visibility=MessageVisibility.ALL,
    )
    results = tuple(
        Message(
            message_id=f"tool-{suffix}",
            session_id="session",
            role=Role.TOOL,
            content=(
                ToolResultBlock(
                    tool_call_id=f"call-{suffix}",
                    tool_name="lookup",
                    content=suffix * 40,
                ),
            ),
            origin=MessageOrigin.TOOL,
            run_id="run-old",
            created_at=_NOW,
            visibility=MessageVisibility.ALL,
        )
        for suffix in ("a", "b")
    )
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, assistant, *results, current))
    running = state("current")

    result = require_result(
        await compact(
            make_compactor(provider, hard_input_limit=500),
            context=context,
            running=running,
            request_messages=ContextBuilder().build(context, running),
        )
    )

    assert result_block(result).next_retained_message_id == "assistant-batch"
    assert tuple(message.message_id for message in result.request_messages[-4:]) == (
        "assistant-batch",
        "tool-a",
        "tool-b",
        "current",
    )


async def test_open_current_tool_round_is_forced_into_tail() -> None:
    """未闭合当前工具批次必须完整保留且不因预算报错。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "old" * 400, run_id="run-old")
    assistant, _ = tool_round("pending", "z" * 400, run_id="run-current")
    context = source((old, assistant))
    running = state("assistant-pending").model_copy(
        update={"pending_tool_call_id": "call-pending"}
    )

    result = require_result(
        await compact(
            make_compactor(provider, hard_input_limit=500),
            context=context,
            running=running,
            request_messages=ContextBuilder().build(context, running),
        )
    )

    assert result_block(result).compacted_message_ids == ("old",)
    assert result_block(result).next_retained_message_id == "assistant-pending"


def test_only_current_message_has_no_compaction_action() -> None:
    """触发线以上但没有新 prefix 时不得产生压缩候选。"""
    provider = RecordingProvider()
    current = text_message("current", Role.USER, "x" * 5_000, run_id="run-current")
    context = source((current,))
    running = state("current")
    messages = ContextBuilder().build(context, running)

    assert (
        make_compactor(provider, hard_input_limit=100).should_compact(
            context, running, messages, ()
        )
        is False
    )


async def test_summary_request_reuses_main_prefix_tools_and_generation_config() -> None:
    """摘要只能在主请求 cutoff 前缀末尾追加一条 USER 指令。"""
    provider = RecordingProvider()
    previous = compaction_message(
        CompactionBlock(
            action="summarized",
            summary="previous summary",
            compacted_message_ids=("ancient",),
            next_retained_message_id="old",
        ),
        model="summary-model",
    )
    old = text_message("old", Role.USER, "x" * 1_000, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, current), previous)
    running = state("current")
    messages = ContextBuilder().build(context, running)
    tools: tuple[Mapping[str, JsonValue], ...] = (
        {"type": "function", "function": {"name": "lookup"}},
    )
    generation_config = LLMGenerationConfig(
        model="summary-model",
        temperature=0.2,
        max_output_tokens=16_384,
        parallel_tool_calls=True,
        provider_options={"prompt_cache_retention": "24h"},
    )

    result = require_result(
        await compact(
            make_compactor(
                provider,
                hard_input_limit=500,
                generation_config=generation_config,
            ),
            context=context,
            running=running,
            request_messages=messages,
            tools=tools,
        )
    )

    summary_request = result.summary_calls[0][0]
    assert summary_request.tools == tools
    assert summary_request.messages[:-1] == messages[:3]
    assert summary_request.messages[-1].role is Role.USER
    instruction = summary_request.messages[-1].content[0]
    assert isinstance(instruction, TextBlock)
    assert summary_request.config == generation_config
    assert summary_request.output_mode is OutputMode.COMPLETE
    assert result.selected_summary_index == 0
    assert result.usage.cache_read_tokens == 7
    assert len(result.summary_calls) == 1


@pytest.mark.parametrize(
    ("trigger_delta", "expected_action"),
    [(1, "pruned"), (0, "summarized")],
)
async def test_pruning_must_be_strictly_below_trigger(
    trigger_delta: int,
    expected_action: str,
) -> None:
    """剪枝结果等于触发线时必须丢弃候选并执行摘要。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "old", run_id="run-old")
    assistant, tool = tool_round("old", "z" * 6_000, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, assistant, tool, current))
    running = state("current")
    config = CompactionConfig(
        trigger_ratio=0.50,
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    )
    prune_snapshot = compaction_message(
        CompactionBlock(
            action="pruned",
            pruned_message_ids=("assistant-old", "tool-old"),
        )
    )
    pruned_messages = ContextBuilder(config).build(
        source(context.retained_messages, prune_snapshot), running
    )
    pruned_tokens = make_compactor(
        provider,
        trigger_ratio=0.50,
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    ).count_tokens(pruned_messages, ())
    compactor = make_compactor(
        provider,
        hard_input_limit=2 * (pruned_tokens + trigger_delta),
        trigger_ratio=0.50,
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    )
    messages = ContextBuilder(config).build(context, running)

    result = require_result(
        await compact(
            compactor,
            context=context,
            running=running,
            request_messages=messages,
        )
    )

    assert result.action == expected_action
    if expected_action == "pruned":
        assert provider.requests == []
        assert result_block(result).pruned_message_ids == (
            "assistant-old",
            "tool-old",
        )
    else:
        assert len(provider.requests) == 1
        assert result_block(result).pruned_message_ids == ()
        assert provider.requests[0].messages[:-1] == messages[:-1]


async def test_summary_preserves_real_anchor_with_estimated_request_delta() -> None:
    """摘要只把纯估算的上下文变化量应用到真实 token 锚点。"""
    provider = RecordingProvider()
    context, running, messages = simple_history()
    compactor = make_compactor(provider, hard_input_limit=500)
    fingerprint = context_token_request_fingerprint(
        provider_name=provider.provider_name,
        model="summary-model",
        messages=messages,
        tools=(),
    )
    anchored = set_context_token_checkpoint(
        running,
        tokens=8_000,
        message_id="old",
        request_fingerprint=fingerprint,
    )
    pure_before = compactor.count_tokens(messages, ())
    checkpoint_estimate = compactor.count_tokens(messages[:2], ())
    expected_before = 8_000 + pure_before - checkpoint_estimate

    result = require_result(
        await compact(
            compactor,
            context=context,
            running=anchored,
            request_messages=messages,
        )
    )
    pure_after = compactor.count_tokens(result.request_messages, ())

    assert result.before_tokens == expected_before
    assert result.after_tokens == max(
        0,
        expected_before + pure_after - pure_before,
    )


async def test_pruning_preserves_real_anchor_with_estimated_request_delta() -> None:
    """工具剪枝从真实 token 锚点扣除同口径纯估算差值。"""
    provider = RecordingProvider()
    old = text_message("old", Role.USER, "old", run_id="run-old")
    assistant, tool = tool_round("old", "z" * 6_000, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, assistant, tool, current))
    running = state("current")
    config = CompactionConfig(
        trigger_ratio=0.50,
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    )
    estimator = make_compactor(
        provider,
        trigger_ratio=0.50,
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    )
    messages = ContextBuilder(config).build(context, running)
    prune_snapshot = compaction_message(
        CompactionBlock(
            action="pruned",
            pruned_message_ids=("assistant-old", "tool-old"),
        )
    )
    pruned_messages = ContextBuilder(config).build(
        source(context.retained_messages, prune_snapshot), running
    )
    pure_pruned = estimator.count_tokens(pruned_messages, ())
    compactor = make_compactor(
        provider,
        hard_input_limit=2 * (pure_pruned + 21),
        trigger_ratio=0.50,
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    )
    pure_before = compactor.count_tokens(messages, ())
    checkpoint_index = next(
        index
        for index, message in enumerate(messages)
        if message.message_id == "assistant-old"
    )
    checkpoint_estimate = compactor.count_tokens(messages[: checkpoint_index + 1], ())
    fingerprint = context_token_request_fingerprint(
        provider_name=provider.provider_name,
        model="summary-model",
        messages=messages,
        tools=(),
    )
    anchored = set_context_token_checkpoint(
        running,
        tokens=checkpoint_estimate + 20,
        message_id="assistant-old",
        request_fingerprint=fingerprint,
    )
    expected_before = pure_before + 20

    result = require_result(
        await compact(
            compactor,
            context=context,
            running=anchored,
            request_messages=messages,
        )
    )
    pure_after = compactor.count_tokens(result.request_messages, ())

    assert result.action == "pruned"
    assert result.before_tokens == expected_before
    assert result.after_tokens == max(
        0,
        expected_before + pure_after - pure_before,
    )


async def test_summary_middleware_receives_final_budget_once() -> None:
    """只有摘要路径在 Provider 前收到最终 cutoff 预算。"""
    provider = RecordingProvider()
    observed: list[CompactionMiddlewarePayload] = []

    async def observe(context: MiddlewareContext) -> MiddlewareDecision:
        assert provider.requests == []
        assert isinstance(context.payload, CompactionMiddlewarePayload)
        observed.append(context.payload)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="observe-summary",
            middleware_id="observe-summary",
            point=MiddlewarePoint.BEFORE_COMPACTION,
            scope=MiddlewareScope.GLOBAL,
            handler=observe,
        )
    )
    registry.freeze()
    context, running, messages = simple_history()
    compactor = make_compactor(
        provider,
        hard_input_limit=500,
        middleware_manager=MiddlewareManager(registry),
    )

    await compact(
        compactor,
        context=context,
        running=running,
        request_messages=messages,
    )

    current = context.retained_messages[-1]
    assert observed == [
        CompactionMiddlewarePayload(
            model="summary-model",
            compacted_message_ids=("old",),
            before_tokens=compactor.count_tokens(messages, ()),
            trigger_tokens=400,
            recent_message_budget_tokens=100,
            retained_message_tokens=compactor.count_tokens((current,), ()),
        )
    ]


async def test_blocked_summary_middleware_skips_provider() -> None:
    """BEFORE_COMPACTION BLOCK 必须发生在摘要 Provider 副作用之前。"""
    provider = RecordingProvider()

    async def block_summary(context: MiddlewareContext) -> MiddlewareDecision:
        assert isinstance(context.payload, CompactionMiddlewarePayload)
        return MiddlewareDecision(
            type=MiddlewareDecisionType.BLOCK,
            reason_code="summary_denied",
        )

    registry = MiddlewareRegistry()
    registry.register(
        MiddlewareRegistration(
            name="block-summary",
            middleware_id="block-summary",
            point=MiddlewarePoint.BEFORE_COMPACTION,
            scope=MiddlewareScope.GLOBAL,
            handler=block_summary,
        )
    )
    registry.freeze()
    context, running, messages = simple_history()

    with pytest.raises(ValueError, match="summary_denied"):
        await compact(
            make_compactor(
                provider,
                hard_input_limit=500,
                middleware_manager=MiddlewareManager(registry),
            ),
            context=context,
            running=running,
            request_messages=messages,
        )

    assert provider.requests == []


async def test_asset_reference_and_media_remain_in_reused_prefix() -> None:
    """摘要前缀和末尾指令必须共同保留媒体与可复用 Asset 句柄。"""
    summary = """<summary><multimodal_context>
<media index="1" type="image" media_type="image/png" asset_id="sha256-image">
<content>登录页面截图。</content></media></multimodal_context></summary>"""
    provider = RecordingProvider(summary)
    old = Message(
        message_id="old",
        session_id="session",
        role=Role.USER,
        content=(
            TextBlock(text="inspect"),
            ImageBlock(
                source=AssetSource(
                    asset_id="sha256-image",
                    media_type="image/png",
                    filename="private.png",
                )
            ),
        ),
        origin=MessageOrigin.USER,
        run_id="run-old",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((old, current))
    running = state("current")
    messages = ContextBuilder(include_asset_id_in_context=True).build(context, running)

    result = require_result(
        await compact(
            make_compactor(
                provider,
                hard_input_limit=500,
                include_asset_id_in_context=True,
            ),
            context=context,
            running=running,
            request_messages=messages,
        )
    )

    request = result.summary_calls[0][0]
    assert request.messages[:-1] == messages[:-1]
    prefix_text = "".join(
        block.text
        for message in request.messages[:-1]
        for block in message.content
        if isinstance(block, TextBlock)
    )
    instruction = request.messages[-1].content[0]
    assert isinstance(instruction, TextBlock)
    assert "sha256-image" in prefix_text
    assert result_block(result).summary == summary


async def test_large_valid_summary_is_accepted_without_size_gate() -> None:
    """结构合法的首个摘要即使估算仍超限也必须直接采用。"""
    provider = RecordingProvider("summary" * 5_000)
    context, running, messages = simple_history()

    result = require_result(
        await compact(
            make_compactor(provider, hard_input_limit=100),
            context=context,
            running=running,
            request_messages=messages,
        )
    )

    assert result.action == "summarized"
    assert result.after_tokens > 100
    assert len(provider.requests) == 1


async def test_summary_replaces_authoritative_historical_pruning() -> None:
    """历史已提交剪枝留在当前前缀，摘要成功后清空 pruning snapshot。"""
    provider = RecordingProvider()
    assistant, tool = tool_round("old", "z" * 6_000, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    previous = compaction_message(
        CompactionBlock(
            action="pruned",
            pruned_message_ids=("assistant-old", "tool-old"),
        )
    )
    context = source((assistant, tool, current), previous)
    running = state("current")
    config = CompactionConfig(
        tool_argument_max_chars=32,
        tool_result_max_chars=32,
    )
    messages = ContextBuilder(config).build(context, running)

    result = require_result(
        await compact(
            make_compactor(
                provider,
                hard_input_limit=100,
                tool_argument_max_chars=32,
                tool_result_max_chars=32,
            ),
            context=context,
            running=running,
            request_messages=messages,
        )
    )

    assert result.summary_calls[0][0].messages[:-1] == messages[:-1]
    assert result_block(result).pruned_message_ids == ()


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(
            blocks=(TextBlock(text="partial"),),
            usage=Usage(),
            timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
            model="summary-model",
            finish_reason=FinishReason.LENGTH,
        ),
        LLMResponse(
            blocks=(),
            usage=Usage(),
            timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
            model="summary-model",
            finish_reason=FinishReason.STOP,
        ),
        LLMResponse(
            blocks=(
                ToolCallBlock(
                    tool_call_id="call-summary",
                    tool_name="lookup",
                    arguments={},
                    raw_arguments="{}",
                ),
            ),
            usage=Usage(),
            timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
            model="summary-model",
            finish_reason=FinishReason.STOP,
        ),
    ],
)
async def test_invalid_summary_response_fails_without_retry(
    response: LLMResponse,
) -> None:
    """非 STOP、空文本或 ToolCall 摘要必须在首次响应失败。"""
    provider = ResponseProvider(response)
    context, running, messages = simple_history()

    with pytest.raises(ValueError):
        await compact(
            make_compactor(provider, hard_input_limit=500),
            context=context,
            running=running,
            request_messages=messages,
        )

    assert len(provider.requests) == 1


async def test_provider_failure_does_not_fall_back_to_pruning() -> None:
    """未达标剪枝后的 Provider 错误必须原样传播且没有结果 fallback。"""
    provider = FailingProvider()
    assistant, tool = tool_round("old", "z" * 2_000, run_id="run-old")
    current = text_message("current", Role.USER, "current", run_id="run-current")
    context = source((assistant, tool, current))
    running = state("current")
    with pytest.raises(RuntimeError, match="provider overflow"):
        await compact(
            make_compactor(
                provider,
                hard_input_limit=100,
                tool_argument_max_chars=32,
                tool_result_max_chars=32,
            ),
            context=context,
            running=running,
            request_messages=ContextBuilder(
                CompactionConfig(
                    tool_argument_max_chars=32,
                    tool_result_max_chars=32,
                )
            ).build(context, running),
        )

    assert len(provider.requests) == 1
