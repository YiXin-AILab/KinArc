"""执行预算判断、工具剪枝规划和多模态历史摘要。"""

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from agentsystem.common.models import JsonValue, OutputMode, SessionContext, StrictModel
from agentsystem.config.models import CompactionConfig
from agentsystem.context.builder import ContextBuilder
from agentsystem.llm.client import LLMClient
from agentsystem.llm.models import (
    LLMEnd,
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    Usage,
)
from agentsystem.message.models import (
    AudioBlock,
    CompactionBlock,
    ContextMessage,
    DocumentBlock,
    ImageBlock,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    VideoBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    CompactionMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecisionType,
    MiddlewarePoint,
)
from agentsystem.runtime.state import AgentState

_COMPACTION_NAMESPACE = UUID("09d91bc1-8007-4d8d-98e5-cd23bad4bc19")
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4


def _canonical_json_text(payload: object) -> str:
    """按稳定 Provider 计量格式编码 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def context_token_request_fingerprint(
    *,
    provider_name: str,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Mapping[str, JsonValue]],
) -> str:
    """返回判断真实 token 锚点能否复用的稳定请求身份。

    Args:
        provider_name: 当前 LLM Provider 名称。
        model: 当前模型名称。
        messages: 完成投影的请求消息，仅首条 SYSTEM 参与指纹。
        tools: 按请求顺序排列的工具 schema。

    Returns:
        Provider、模型、SYSTEM 和工具配置的 SHA-256 十六进制摘要。
    """
    system_message = next(
        (message for message in messages if message.role is Role.SYSTEM),
        None,
    )
    projected_system = (
        _project_provider_message(system_message)[0]
        if system_message is not None
        else None
    )
    canonical = _canonical_json_text(
        {
            "model": model,
            "provider_name": provider_name,
            "system": projected_system,
            "tools": [dict(tool) for tool in tools],
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _apply_estimated_delta(
    current_tokens: int,
    *,
    before_estimate: int,
    after_estimate: int,
) -> int:
    """把同口径纯估算变化量应用到当前 token 基线。"""
    return max(0, current_tokens + after_estimate - before_estimate)


@dataclass(frozen=True, slots=True)
class _MessageUnit:
    """最终 Provider history 中不可拆分的 Message 区间。"""

    start: int
    end: int
    is_open_tool_round: bool


@dataclass(frozen=True, slots=True)
class _CompactionPlan:
    """一次压缩使用的唯一 Provider/source cutoff。"""

    provider_history_start: int
    provider_cutoff_index: int
    source_cutoff_index: int
    compacted_message_ids: tuple[str, ...]
    retained_message_tokens: int


class CompactionResult(StrictModel):
    """单次 compact 调用的结果，由 Loop 产生事件、Session 投影持久化。

    Attributes:
        action: 本次采用工具剪枝还是历史摘要。
        request_messages: 下一次请求可直接使用的上下文副本。
        before_tokens: 压缩前计量值，可能包含真实 usage 锚点修正。
        after_tokens: 按相同计量口径计算的压缩后值。
        usage: 摘要调用的模型用量；纯剪枝时为空用量。
        compaction_message: 待持久化的完整压缩快照。
        summary_provider_name: 摘要 Provider 名称；纯剪枝时为空。
        summary_calls: 摘要请求和响应记录。
        selected_summary_index: 被快照选用的摘要响应下标。
    """

    action: Literal["pruned", "summarized"]
    request_messages: tuple[Message, ...] = Field(min_length=1)
    before_tokens: int = Field(ge=0)
    after_tokens: int = Field(ge=0)
    usage: Usage = Field(default_factory=Usage)
    compaction_message: Message
    summary_provider_name: str | None = None
    summary_calls: tuple[tuple[LLMRequest, LLMResponse], ...] = ()
    selected_summary_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """确保动作、快照和摘要调用记录严格对齐。

        Returns:
            通过一致性校验的当前实例。

        Raises:
            ValueError: 动作、摘要调用索引或响应模型与快照不匹配。
        """
        block = _result_block(self.compaction_message)
        if block.action != self.action:
            raise ValueError("结果 action 与 CompactionBlock 不一致")
        if self.action == "pruned":
            if self.selected_summary_index is not None:
                raise ValueError("pruned 结果不能选择摘要响应")
            return self
        if (
            not self.summary_calls
            or self.summary_provider_name is None
            or self.selected_summary_index is None
            or self.selected_summary_index >= len(self.summary_calls)
        ):
            raise ValueError("summarized 结果缺少完整摘要调用记录")
        message = self.compaction_message
        if (
            message is None
            or message.model != self.summary_calls[self.selected_summary_index][1].model
        ):
            raise ValueError("摘要快照 model 与选中响应不一致")
        return self


class Compactor:
    """执行预算判断、工具剪枝规划和历史摘要。

    依赖注入的 LLMClient、配置和可选 Middleware；只生成候选快照，不写存储。
    调用方负责持有 session 执行权，并在应用结果前完成事件投影。
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        generation_config: LLMGenerationConfig,
        context_window: int,
        config: CompactionConfig,
        include_asset_id_in_context: bool = False,
        middleware_manager: MiddlewareManager | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """保存 Client、生成参数与同一份不可变压缩配置。

        Args:
            client: 执行摘要请求的客户端，其自身负责模型调用超时。
            generation_config: 摘要请求模型和生成参数。
            context_window: 模型上下文窗口 token 上限。
            config: 安全余量、触发阈值、保留预算和剪枝长度配置。
            include_asset_id_in_context: 是否在发送副本中展示资产引用。
            middleware_manager: 可选的摘要前策略挂点管理器。
            clock: 快照和摘要指令消息时间工厂。
            id_factory: 摘要请求 ID 工厂。

        Raises:
            ValueError: 扣除输出预算和安全余量后无正数输入预算。
        """
        hard_input_limit = (
            context_window
            - (generation_config.max_output_tokens or 0)
            - config.safety_margin_tokens
        )
        if hard_input_limit <= 0:
            raise ValueError("hard_input_limit 必须为正数")
        self._client = client
        self._generation_config = generation_config
        self._hard_input_limit = hard_input_limit
        self._config = config
        self._include_asset_id_in_context = include_asset_id_in_context
        self._middleware = middleware_manager
        self._builder = ContextBuilder(
            self._config,
            include_asset_id_in_context=include_asset_id_in_context,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or _timestamp_id(self._clock)

    def count_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]],
    ) -> int:
        """按 Provider 字段的规范 JSON 字符数和固定媒体预算估算请求大小。

        Args:
            messages: 已完成内部消息投影的发送消息。
            tools: 本次请求携带的工具 schema。

        Returns:
            文本字符估算值与媒体块固定预算之和，不是 tokenizer 精确值。

        Raises:
            ValueError: 包含未投影的内部块或非法 TOOL 消息。
        """
        projected_messages = []
        media_count = 0
        for message in messages:
            projected, message_media_count = _project_provider_message(message)
            projected_messages.append(projected)
            media_count += message_media_count
        canonical = _canonical_json_text(
            {
                "messages": projected_messages,
                "tools": [dict(tool) for tool in tools],
            }
        )
        return (
            math.ceil(len(canonical) / _TOKEN_ESTIMATE_CHARS_PER_TOKEN)
            + media_count * self._config.media_block_tokens
        )

    def count_request_tokens(
        self,
        state: AgentState,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]],
    ) -> int:
        """优先复用真实检查点，只估算其后的 Provider 请求增量。

        Args:
            state: 保存 usage 锚点、消息位置和请求指纹的状态快照。
            messages: 当前完整发送消息。
            tools: 当前工具 schema。

        Returns:
            锚点有效时返回修正后的 token 值，否则返回完整估算值。
        """
        full_estimate = self.count_tokens(messages, tools)
        checkpoint_id = state.context_token_checkpoint_message_id
        checkpoint_tokens = state.context_tokens
        fingerprint = context_token_request_fingerprint(
            provider_name=self._client.provider_name,
            model=self._generation_config.model,
            messages=messages,
            tools=tools,
        )
        if (
            checkpoint_id is None
            or checkpoint_tokens is None
            or state.context_token_request_fingerprint != fingerprint
        ):
            return full_estimate
        checkpoint_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message.message_id == checkpoint_id
            ),
            None,
        )
        if checkpoint_index is None:
            return full_estimate
        checkpoint_estimate = self.count_tokens(
            messages[: checkpoint_index + 1],
            tools,
        )
        return max(0, checkpoint_tokens + full_estimate - checkpoint_estimate)

    def should_compact(
        self,
        source: ContextMessage,
        state: AgentState,
        request_messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]],
    ) -> bool:
        """判断是否达到触发阈值且存在可压缩的历史前缀。

        Args:
            source: 权威上下文来源，用于映射压缩边界。
            state: 当前 run 的消息和 token 锚点状态。
            request_messages: 已完成投影的请求消息。
            tools: 当前请求工具 schema。

        Returns:
            压缩开启、达到阈值且存在有效边界时为 True。
        """
        if not self._config.enabled:
            return False
        trigger = math.floor(self._hard_input_limit * self._config.trigger_ratio)
        if self.count_request_tokens(state, request_messages, tools) < trigger:
            return False
        return self._plan_compaction(source, state, tuple(request_messages)) is not None

    async def compact(
        self,
        *,
        ctx: SessionContext,
        run_id: str,
        trace_id: str,
        source: ContextMessage,
        state: AgentState,
        request_messages: tuple[Message, ...],
        tools: tuple[Mapping[str, JsonValue], ...],
    ) -> CompactionResult | None:
        """先尝试工具剪枝，不足以降至阈值下时执行一次历史摘要。

        Args:
            ctx: 当前会话身份。
            run_id: 持锁执行中的内部 run 标识。
            trace_id: 当前 Runtime trace 标识。
            source: 权威上下文和已有压缩快照。
            state: 当前执行状态，用于保护当前消息与未闭合工具批次。
            request_messages: 已由 ContextBuilder 生成的发送消息。
            tools: 随摘要请求传递并参与预算计算的工具 schema。

        Returns:
            新发送副本与待持久化快照；没有可压缩前缀时返回 None。

        Raises:
            ValueError: 策略阻止摘要、历史边界无效或摘要响应不符合约束。

        模型和 Middleware 异常继续传播；本方法不修改 source 或 state。
        """
        pure_before_tokens = self.count_tokens(request_messages, tools)
        before_tokens = self.count_request_tokens(state, request_messages, tools)
        trigger_tokens = math.floor(self._hard_input_limit * self._config.trigger_ratio)
        plan = self._plan_compaction(source, state, request_messages)
        if plan is None:
            return None

        pruned_ids = self._next_pruned_message_ids(
            source,
            request_messages,
            plan,
        )
        prune_message = self._pruned_snapshot(ctx, run_id, source, pruned_ids)
        if prune_message is not None and not _same_snapshot(source, prune_message):
            pruned_messages = self._build_from_snapshot(source, state, prune_message)
            pure_pruned_tokens = self.count_tokens(pruned_messages, tools)
            pruned_tokens = _apply_estimated_delta(
                before_tokens,
                before_estimate=pure_before_tokens,
                after_estimate=pure_pruned_tokens,
            )
            if pruned_tokens < trigger_tokens:
                return CompactionResult(
                    action="pruned",
                    request_messages=pruned_messages,
                    before_tokens=before_tokens,
                    after_tokens=pruned_tokens,
                    compaction_message=prune_message,
                )

        if self._middleware is not None:
            decision = await self._middleware.dispatch(
                MiddlewareContext(
                    invocation_id="context-compaction",
                    point=MiddlewarePoint.BEFORE_COMPACTION,
                    ctx=ctx,
                    run_id=run_id,
                    trace_id=trace_id,
                    turn_index=state.turn_index or None,
                    state_snapshot=state.model_copy(deep=True),
                    payload=CompactionMiddlewarePayload(
                        model=self._generation_config.model,
                        compacted_message_ids=plan.compacted_message_ids,
                        before_tokens=before_tokens,
                        trigger_tokens=trigger_tokens,
                        recent_message_budget_tokens=math.floor(
                            self._hard_input_limit * self._config.recent_message_ratio
                        ),
                        retained_message_tokens=plan.retained_message_tokens,
                    ),
                )
            )
            if decision.type is MiddlewareDecisionType.BLOCK:
                reason = decision.reason_code or "policy_blocked"
                raise ValueError(f"BEFORE_COMPACTION blocked: {reason}")

        request, response = await self._summarize(
            ctx=ctx,
            run_id=run_id,
            request_messages=request_messages,
            provider_cutoff_index=plan.provider_cutoff_index,
            tools=tools,
        )
        message, candidate_messages, pure_after_tokens = self._build_candidate(
            ctx=ctx,
            run_id=run_id,
            source=source,
            state=state,
            plan=plan,
            response=response,
            tools=tools,
        )
        after_tokens = _apply_estimated_delta(
            before_tokens,
            before_estimate=pure_before_tokens,
            after_estimate=pure_after_tokens,
        )
        return CompactionResult(
            action="summarized",
            request_messages=candidate_messages,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            usage=response.usage,
            compaction_message=message,
            summary_provider_name=self._client.provider_name,
            summary_calls=((request, response),),
            selected_summary_index=0,
        )

    def _plan_compaction(
        self,
        source: ContextMessage,
        state: AgentState,
        request_messages: tuple[Message, ...],
    ) -> _CompactionPlan | None:
        """按 messages-only 软预算返回唯一连续 cutoff。"""
        source_ids = {message.message_id for message in source.retained_messages}
        history_start = next(
            (
                index
                for index, message in enumerate(request_messages)
                if message.message_id in source_ids
            ),
            None,
        )
        if history_start is None:
            return None
        history = request_messages[history_start:]
        if not history:
            return None
        units = _message_units(history)
        recent_budget = math.floor(
            self._hard_input_limit * self._config.recent_message_ratio
        )

        tail_start = len(history) - 1
        while tail_start > 0:
            candidate = history[tail_start - 1 :]
            if self.count_tokens(candidate, ()) > recent_budget:
                break
            tail_start -= 1

        forced_starts = [
            unit.start
            for unit in units
            if unit.is_open_tool_round
            or any(
                message.message_id in state.current_message_ids
                for message in history[unit.start : unit.end]
            )
        ]
        if forced_starts:
            tail_start = min(tail_start, min(forced_starts))
        for unit in units:
            if unit.start < tail_start < unit.end:
                tail_start = unit.start
                break
        if tail_start == 0:
            return None

        next_retained_id = history[tail_start].message_id
        source_cutoff = next(
            (
                index
                for index, message in enumerate(source.retained_messages)
                if message.message_id == next_retained_id
            ),
            None,
        )
        if source_cutoff is None or source_cutoff == 0:
            return None
        prefix = source.retained_messages[:source_cutoff]
        return _CompactionPlan(
            provider_history_start=history_start,
            provider_cutoff_index=history_start + tail_start,
            source_cutoff_index=source_cutoff,
            compacted_message_ids=_cumulative_ids(source, prefix),
            retained_message_tokens=self.count_tokens(history[tail_start:], ()),
        )

    def _next_pruned_message_ids(
        self,
        source: ContextMessage,
        request_messages: tuple[Message, ...],
        plan: _CompactionPlan,
    ) -> tuple[str, ...]:
        """返回 cutoff 前闭合工具内容的完整确定性剪枝快照。"""
        source_by_id = {
            message.message_id: message for message in source.retained_messages
        }
        provider_prefix = request_messages[
            plan.provider_history_start : plan.provider_cutoff_index
        ]
        eligible: set[str] = set()
        for unit in _message_units(provider_prefix):
            messages = provider_prefix[unit.start : unit.end]
            if unit.is_open_tool_round or not _tool_call_ids(messages[0]):
                continue
            for projected in messages:
                original = source_by_id.get(projected.message_id)
                if original is None:
                    continue
                pruned = self._builder._prune_tools(
                    (original,), frozenset((original.message_id,))
                )[0]
                if pruned.content != original.content:
                    eligible.add(original.message_id)

        previous = _latest_block(source)
        if previous is not None:
            eligible.update(
                message_id
                for message_id in previous.pruned_message_ids
                if message_id in source_by_id
            )
        return tuple(
            message.message_id
            for message in source.retained_messages
            if message.message_id in eligible
        )

    async def _summarize(
        self,
        *,
        ctx: SessionContext,
        run_id: str,
        request_messages: tuple[Message, ...],
        provider_cutoff_index: int,
        tools: tuple[Mapping[str, JsonValue], ...],
    ) -> tuple[LLMRequest, LLMResponse]:
        """复用主请求 Provider 前缀并执行一次 COMPLETE 摘要调用。"""
        messages = (
            *request_messages[:provider_cutoff_index],
            _compact_instruction_message(
                ctx,
                run_id,
                self._clock(),
                include_asset_id_in_context=self._include_asset_id_in_context,
            ),
        )
        request = LLMRequest(
            request_id=self._id_factory(),
            messages=messages,
            tools=tools,
            config=self._generation_config,
            output_mode=OutputMode.COMPLETE,
        )
        response: LLMResponse | None = None
        async for event in self._client.generate(request):
            if not isinstance(event, LLMEnd):
                continue
            if event.request_id != request.request_id:
                raise ValueError("摘要 Provider request_id 不一致")
            response = event.response
            break
        if response is None:
            raise ValueError("摘要 Provider 未产生 LLMEnd")
        _validate_summary_response(response)
        return request, response

    def _build_candidate(
        self,
        *,
        ctx: SessionContext,
        run_id: str,
        source: ContextMessage,
        state: AgentState,
        plan: _CompactionPlan,
        response: LLMResponse,
        tools: tuple[Mapping[str, JsonValue], ...],
    ) -> tuple[Message, tuple[Message, ...], int]:
        """构造清除剪枝状态的完整摘要候选。"""
        message = _compaction_message(
            ctx=ctx,
            run_id=run_id,
            source=source,
            block=CompactionBlock(
                action="summarized",
                summary=_summary_text(response),
                compacted_message_ids=plan.compacted_message_ids,
                next_retained_message_id=(
                    source.retained_messages[plan.source_cutoff_index].message_id
                ),
            ),
            model=response.model,
            created_at=self._clock(),
        )
        candidate_source = ContextMessage(
            system_message=source.system_message,
            latest_compaction_message=message,
            retained_messages=source.retained_messages[plan.source_cutoff_index :],
            injection_messages=_retained_injections(
                source,
                source.retained_messages[plan.source_cutoff_index :],
            ),
        )
        request_messages = self._builder.build(candidate_source, state)
        return message, request_messages, self.count_tokens(request_messages, tools)

    def _pruned_snapshot(
        self,
        ctx: SessionContext,
        run_id: str,
        source: ContextMessage,
        pruned_ids: tuple[str, ...],
    ) -> Message | None:
        if not pruned_ids:
            return None
        previous = _latest_block(source)
        block = CompactionBlock(
            action="pruned",
            summary=(previous.summary if previous is not None else None),
            compacted_message_ids=(
                previous.compacted_message_ids if previous is not None else ()
            ),
            next_retained_message_id=(
                previous.next_retained_message_id if previous is not None else None
            ),
            pruned_message_ids=pruned_ids,
        )
        latest = source.latest_compaction_message
        model = (
            latest.model
            if latest is not None
            and previous is not None
            and previous.summary is not None
            else None
        )
        return _compaction_message(
            ctx=ctx,
            run_id=run_id,
            source=source,
            block=block,
            model=model,
            created_at=self._clock(),
        )

    def _build_from_snapshot(
        self,
        source: ContextMessage,
        state: AgentState,
        snapshot: Message,
    ) -> tuple[Message, ...]:
        candidate = ContextMessage(
            system_message=source.system_message,
            latest_compaction_message=snapshot,
            retained_messages=source.retained_messages,
            injection_messages=source.injection_messages,
        )
        return self._builder.build(candidate, state)


def _result_block(message: Message | None) -> CompactionBlock:
    if message is None or len(message.content) != 1:
        raise ValueError("压缩结果缺少唯一 CompactionBlock")
    block = message.content[0]
    if not isinstance(block, CompactionBlock):
        raise ValueError("压缩结果缺少唯一 CompactionBlock")
    return block


def _project_provider_message(message: Message) -> tuple[dict[str, JsonValue], int]:
    if isinstance(message.content[0], (CompactionBlock, InjectedBlock)):
        raise ValueError("内部 Compaction/Injection Message 必须先完成投影")
    media_count = sum(
        isinstance(block, (ImageBlock, AudioBlock, VideoBlock, DocumentBlock))
        for block in message.content
    )
    if message.role is Role.TOOL:
        block = message.content[0]
        if not isinstance(block, ToolResultBlock):
            raise ValueError("TOOL Message 缺少 ToolResultBlock")
        return {
            "role": message.role.value,
            "tool_call_id": block.tool_call_id,
            "content": block.content,
        }, media_count
    projected: dict[str, JsonValue] = {
        "role": message.role.value,
        "content": "".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        ),
    }
    calls: list[JsonValue] = [
        {
            "id": block.tool_call_id,
            "type": "function",
            "function": {
                "name": block.tool_name,
                "arguments": block.raw_arguments,
            },
        }
        for block in message.content
        if isinstance(block, ToolCallBlock)
    ]
    if calls:
        projected["tool_calls"] = calls
    return projected, media_count


def _message_units(messages: Sequence[Message]) -> tuple[_MessageUnit, ...]:
    """把最终 Provider messages 划分成普通消息或完整工具批次。"""
    units: list[_MessageUnit] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role is Role.TOOL:
            raise ValueError("上下文包含孤立 ToolResult")
        call_ids = _tool_call_ids(message)
        if not call_ids:
            units.append(
                _MessageUnit(start=index, end=index + 1, is_open_tool_round=False)
            )
            index += 1
            continue
        cursor = index + 1
        result_ids: list[str] = []
        while cursor < len(messages) and messages[cursor].role is Role.TOOL:
            block = messages[cursor].content[0]
            if not isinstance(block, ToolResultBlock):
                raise ValueError("TOOL Message 缺少 ToolResultBlock")
            result_ids.append(block.tool_call_id)
            cursor += 1
        if len(result_ids) != len(set(result_ids)) or not set(result_ids).issubset(
            call_ids
        ):
            raise ValueError("ToolCall/ToolResult round 不匹配")
        units.append(
            _MessageUnit(
                start=index,
                end=cursor,
                is_open_tool_round=set(result_ids) != set(call_ids),
            )
        )
        index = cursor
    return tuple(units)


def _tool_call_ids(message: Message) -> tuple[str, ...]:
    return tuple(
        block.tool_call_id
        for block in message.content
        if isinstance(block, ToolCallBlock)
    )


def _latest_block(source: ContextMessage) -> CompactionBlock | None:
    message = source.latest_compaction_message
    if message is None:
        return None
    return _result_block(message)


def _same_snapshot(source: ContextMessage, candidate: Message | None) -> bool:
    previous = _latest_block(source)
    if previous is None or candidate is None:
        return previous is None and candidate is None
    return previous == _result_block(candidate)


def _cumulative_ids(
    source: ContextMessage,
    prefix: Sequence[Message],
) -> tuple[str, ...]:
    previous = _latest_block(source)
    covered = previous.compacted_message_ids if previous is not None else ()
    new_ids = tuple(message.message_id for message in prefix)
    if set(covered).intersection(new_ids):
        raise ValueError("累计摘要覆盖 ID 出现重复")
    return covered + new_ids


def _compaction_message(
    *,
    ctx: SessionContext,
    run_id: str,
    source: ContextMessage,
    block: CompactionBlock,
    model: str | None,
    created_at: datetime,
) -> Message:
    previous = source.latest_compaction_message
    identity = json.dumps(
        {
            "app_id": ctx.app_id,
            "user_id": ctx.user_id,
            "agent_id": ctx.agent_id,
            "session_id": ctx.session_id,
            "expected_latest_id": previous.message_id if previous is not None else None,
            "snapshot": block.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return Message(
        message_id=str(uuid5(_COMPACTION_NAMESPACE, identity)),
        session_id=ctx.session_id,
        role=Role.USER,
        content=(block,),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        model=model,
        run_id=run_id,
        created_at=created_at,
    )


def _compact_instruction_message(
    ctx: SessionContext,
    run_id: str,
    now: datetime,
    *,
    include_asset_id_in_context: bool,
) -> Message:
    prompt = """这是 Runtime 内部上下文维护操作，不是历史任务的继续执行。
不要调用任何工具，只输出一份可以完整替代前缀历史的摘要。

你是一个专门负责上下文压缩的 Agent。
你的任务是分析随后提供的会话历史，并生成一份高密度、结构清晰、
可以完整替代被压缩历史的文本摘要。会话可能包含普通文本、代码、
工具调用与结果、图片、截图、图表、架构图、文档、音频和视频。

压缩规则：
1. 【核心目标】保留用户的核心目标、真实意图、持续要求和当前最重要的问题。
   不要把临时尝试误写成最终目标。
2. 【用户反馈与纠错】忠实记录用户的明确指令、反馈与纠偏；特别是用户对
   Agent 做法或结论的修正，必须作为高优先级约束保留。
3. 【事实与决策】保留已经确认的事实、约束、架构决定、取舍及其原因。
   明确区分用户确认、工具或代码验证的事实与模型推断；不得补造原历史中不存在的信息。
4. 【工程细节保真】精确保留继续工作所必需的工程细节，包括精确文件路径、
   代码片段、类与接口、函数签名、命令原文、配置项、数值、标识符、
   报错原文、测试结果和失败根因，严禁做模糊化概括。
5. 【执行状态】保留当前执行状态，包括已完成事项、进行中事项、
   待办事项、阻塞条件和明确的下一步。
6. 【工具调用沉淀】工具内容应保留调用目的、关键参数、有效结果、错误根因和对
   后续决策的影响。省略重复、冗长、未解析的原始日志，以及没有产生新信息的失败尝试；
   但不得省略仍未解决的失败及其证据。
   对仍需读取的剪枝内容，精确保留原始调用的 tool_call_id、part 和
   <pruned-tool-content> 引用；分页读取还须保留 source、range、已核对的关键事实，
   区分尚未读取与已读取但正文再次被剪枝的区间。
   不得改用 tool_content_view 自身的调用 ID。
7. 【递归增量合并】若待压缩历史中已包含先前的 <summary> 块，该块代表历史检查点。
   切勿原样逐字复制：应剔除已被推翻或已失效的旧信息，继承依然有效的事实，
   并将后续新增的交互进展合并为一份统一结构下的完整最新摘要。
8. 【多模态保真】对多模态内容按其在待压缩历史中的出现顺序使用 [Media 1]、
   [Media 2] 等标记：
   - 图片或截图：提取可见文字、错误提示、代码、界面结构、控件状态、颜色
     和其他关键视觉事实；
   - 图表：提取关键数值、趋势、异常点、图例和结论；
   - 架构图或流程图：描述节点、关系、方向、分支和数据流，必要时使用简洁 Mermaid；
   - 文档：保留标题、关键条款、表格事实和结论；
   - 音频或视频：保留明确表达的请求、事实、语气、时间点和技术规格。
   无法从媒体中确认的信息必须明确标为未知，不得猜测。
9. 【信息精炼】删除寒暄、语义重复、已经被后续决定取代的中间方案和无后续价值的逐步尝试。
   若某个旧决定已被替代，应保留最终决定，并仅在解释迁移原因所必需时记录旧决定。
10. 【元信息屏蔽】不得在输出中提及本次总结请求、不得出现“上下文已被压缩/截断”等元描述，
    直接以客观视角记录事实与状态。
11. 【绝对禁止工具调用】本次任务仅为文本摘要生成，绝对禁止调用任何工具（Tool Call）、
    发起任何操作或输出工具调用参数。

输出要求：
- 只输出以下 XML，不要输出 Markdown 代码围栏、前言、解释、工具调用或
  XML 之外的任何内容。
- 所有标签都必须保留；没有相关信息时填写“无”。
- 内容应具体、紧凑、可执行，避免空泛描述。

<summary>
<current_goal>
用户的总体目标、当前问题和持续要求。
</current_goal>
<user_context>
与任务有关的用户事实、偏好、环境和明确表达的意图（包含用户给出的关键反馈与纠偏）。
</user_context>
<decisions_and_constraints>
已经确认的决定、约束、接口契约、取舍及原因。
</decisions_and_constraints>
<execution_state>
<completed>
已经完成并验证的工作与里程碑。
</completed>
<in_progress>
正在进行的工作和当前落点。
</in_progress>
<pending>
未完成事项、阻塞条件和下一步。
</pending>
</execution_state>
<multimodal_context>
按 [Media N] 列出媒体类型、来源位置及提取出的关键事实；没有媒体时填写“无”。
</multimodal_context>
<critical_references>
继续任务所必需的精确文件路径、代码、命令、配置、ID、错误原文、测试结果和其他关键引用。
</critical_references>
<conversation_narrative>
按时间顺序给出高密度会话叙述，说明目标、关键行动、发现、决定和状态如何演进，
并引用相关 [Media N]。
</conversation_narrative>
</summary>"""
    if include_asset_id_in_context:
        prompt += """

资产句柄规则（优先于上方 [Media N] 规则）：
- 输入中的 `<asset_reference>` 与紧随其后的媒体属于同一项。
- 在 `<multimodal_context>` 中为每项媒体输出一个 `<media>` 节点，并按出现顺序设置
  `index`。把媒体摘要写入同一节点的 `<content>`。
- 对包含 `<asset_reference>` 的媒体，必须原样复制其中的 `asset_id`、`type` 和
  `media_type` 属性；不得改写、缩短或猜测句柄。
- 再次压缩已有摘要时，必须继续原样保留仍可能被 Tool 使用的旧 `asset_id`。
- 没有媒体时输出 `<multimodal_context>无</multimodal_context>`。

格式示例：
<multimodal_context>
<media index="1" type="document" media_type="application/pdf" asset_id="sha256-...">
<content>文档中的关键事实……</content>
</media>
</multimodal_context>"""
    return Message(
        message_id=f"compact-instruction-{run_id}",
        session_id=ctx.session_id,
        role=Role.USER,
        content=(TextBlock(text=prompt),),
        origin=MessageOrigin.SERVICE,
        visibility=MessageVisibility.MODEL,
        run_id=run_id,
        created_at=now,
    )


def _validate_summary_response(response: LLMResponse) -> None:
    if response.error is not None or response.finish_reason.value != "stop":
        raise ValueError("摘要模型未正常停止")
    if any(isinstance(block, ToolCallBlock) for block in response.blocks):
        raise ValueError("摘要响应不得包含 ToolCallBlock")
    _summary_text(response)


def _summary_text(response: LLMResponse) -> str:
    parts = tuple(
        block.text.strip()
        for block in response.blocks
        if isinstance(block, TextBlock) and block.text.strip()
    )
    if not parts:
        raise ValueError("摘要响应缺少非空 TextBlock")
    return "\n".join(parts)


def _retained_injections(
    source: ContextMessage,
    retained: Sequence[Message],
) -> tuple[Message, ...]:
    user_ids = {message.message_id for message in retained if message.role is Role.USER}
    return tuple(
        message
        for message in source.injection_messages
        if getattr(message.content[0], "target_user_message_id", None) in user_ids
    )


def _timestamp_id(clock: Callable[[], datetime]) -> Callable[[], str]:
    return lambda: str(uuid5(_COMPACTION_NAMESPACE, str(clock().timestamp())))
