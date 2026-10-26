"""AgentLoop 产生、四类 Projector 消费的不可变事件。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from agentsystem.common.errors import RunError
from agentsystem.common.models import JsonValue, OutputMode
from agentsystem.inbox.models import InboundMode
from agentsystem.llm.models import LLMRequest, LLMResponse, Timing, Usage
from agentsystem.message.models import (
    AssistantBlock,
    BlockType,
    CompactionBlock,
    Message,
    ToolResultBlock,
    is_inbound_message,
)
from agentsystem.runtime.state import RunTiming


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStarted:
    """描述新 run 已进入 RUNNING 的事实；输入保存由后续投影完成。

    Attributes:
        initial_messages: 已归入该 run 的初始 USER/SYSTEM 消息。
    """

    initial_messages: tuple[Message, ...]
    type: Literal["run.started"] = field(default="run.started", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStarted:
    """描述新 turn 已递增并完成 STEER 提升的事实。

    Attributes:
        turn_index: 开始执行的轮次，与事件 envelope 中的轮次一致。
    """

    turn_index: int
    type: Literal["turn.started"] = field(default="turn.started", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnCompleted:
    """描述当前 turn 已完成必要消息保存屏障的事实。

    Attributes:
        turn_index: 已经完成的轮次。
    """

    turn_index: int
    type: Literal["turn.completed"] = field(default="turn.completed", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class MessagesCompleted:
    """携带一批待投影保存的定稿消息，不自行写入存储。

    Attributes:
        messages: 非空定稿消息批次，顺序由生产者决定。
        inbound_modes: 本批 USER/SYSTEM 输入的 Inbox 模式，供交接确认使用。
    """

    messages: tuple[Message, ...]
    inbound_modes: Mapping[str, InboundMode] = field(default_factory=dict)
    type: Literal["messages.completed"] = field(
        default="messages.completed",
        init=False,
    )

    def __post_init__(self) -> None:
        """校验消息批次和 Inbox 交接引用。

        Raises:
            ValueError: 批次为空、mode 引用批次外消息或非 USER/SYSTEM 输入。
        """
        messages = {message.message_id: message for message in self.messages}
        if not self.messages:
            raise ValueError("MessagesCompleted.messages 不能为空")
        if not set(self.inbound_modes).issubset(messages):
            raise ValueError("inbound_modes 引用了批次外 Message")
        if any(
            not is_inbound_message(messages[message_id])
            for message_id in self.inbound_modes
        ):
            raise ValueError("inbound_modes 只能引用 USER/SYSTEM 输入 Message")


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageStarted:
    """描述一条 USER/SYSTEM Inbox 输入开始进入当前 run。

    Attributes:
        message: 已经分配 run 身份的输入消息。
        mode: 领取时的 QUEUE 或 STEER 模式。
    """

    message: Message
    mode: InboundMode
    type: Literal["message.started"] = field(default="message.started", init=False)

    def __post_init__(self) -> None:
        """校验 started 事件只描述 USER/SYSTEM Inbox 输入。

        Raises:
            ValueError: 消息不是允许的入站角色与内容组合。
        """
        if not is_inbound_message(self.message):
            raise ValueError("MessageStarted 只支持 USER/SYSTEM 输入 Message")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompactionStarted:
    """输入达到触发阈值，上下文压缩开始。"""

    type: Literal["context.compaction.started"] = field(
        default="context.compaction.started",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompactionCompleted:
    """携带实际压缩结果，等待 Message → State 保存屏障。

    构造时校验消息中的 CompactionBlock 与事件覆盖边界一致；摘要动作还需
    提供唯一一次摘要调用的元数据。校验失败抛出 ValueError。

    Attributes:
        action: 裁剪或摘要动作。
        compaction_message: 待保存的压缩快照消息。
        compacted_message_ids: 摘要所覆盖的历史消息标识。
        pruned_message_ids: 本快照裁剪的消息标识。
        before_tokens: 压缩前估算的上下文 token 数。
        after_tokens: 压缩后估算的上下文 token 数。
        usage: 本次压缩使用的模型用量。
        summary_provider_name: 摘要模型所属 Provider；纯裁剪可为空。
        summary_calls: 摘要调用请求和响应，供 Trace 投影使用。
        selected_summary_index: 选中摘要响应的索引；摘要动作固定为零。
        expected_latest_compaction_id: 提交时应匹配的上一压缩消息标识。
    """

    action: Literal["pruned", "summarized"]
    compaction_message: Message
    compacted_message_ids: tuple[str, ...]
    pruned_message_ids: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    usage: Usage
    summary_provider_name: str | None = None
    summary_calls: tuple[tuple[LLMRequest, LLMResponse], ...] = ()
    selected_summary_index: int | None = None
    expected_latest_compaction_id: str | None = None
    type: Literal["context.compaction.completed"] = field(
        default="context.compaction.completed",
        init=False,
    )

    def __post_init__(self) -> None:
        """校验事件快照边界与摘要调用元数据。

        Raises:
            ValueError: 压缩块边界不一致，或摘要调用身份、响应选择不完整。
        """
        self._validate_snapshot()
        if self.action == "pruned":
            if self.selected_summary_index is not None:
                raise ValueError("pruned Event 不能选择摘要响应")
            return
        if (
            len(self.summary_calls) != 1
            or self.summary_provider_name is None
            or self.selected_summary_index != 0
        ):
            raise ValueError(
                "summarized Event 缺少完整摘要调用元数据；"
                "必须为唯一摘要调用并选择索引 0"
            )
        selected = self.summary_calls[self.selected_summary_index][1]
        message = self.compaction_message
        if message is None or message.model != selected.model:
            raise ValueError("Compaction Message model 与选中摘要响应不一致")

    def _validate_snapshot(self) -> None:
        message = self.compaction_message
        if message is None or not message.content:
            raise ValueError("ContextCompactionCompleted 缺少 CompactionBlock")
        block = message.content[0]
        if not isinstance(block, CompactionBlock):
            raise ValueError("ContextCompactionCompleted 缺少 CompactionBlock")
        if (
            block.action != self.action
            or block.compacted_message_ids != self.compacted_message_ids
            or block.pruned_message_ids != self.pruned_message_ids
        ):
            raise ValueError("ContextCompactionCompleted 字段与 Block 快照不一致")


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallStarted:
    """描述一次逻辑模型调用开始，供 Trace 和公开事件投影消费。

    Attributes:
        request_id: 本次调用身份；重试使用新的标识。
        provider_name: 归一化 Provider 名称。
        model: 请求的模型名称。
        output_mode: 本次输出模式。
        request: 可选完整领域请求，供内部 Trace 捕获。
    """

    request_id: str
    provider_name: str
    model: str
    output_mode: OutputMode
    request: LLMRequest | None = None
    type: Literal["llm.call.started"] = field(
        default="llm.call.started",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallCompleted:
    """携带一次逻辑模型调用返回的归一化响应。

    Attributes:
        request_id: 与开始事件匹配的请求身份。
        response: 领域响应，可能包含 Provider 报告的错误。
    """

    request_id: str
    response: LLMResponse
    type: Literal["llm.call.completed"] = field(
        default="llm.call.completed",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageBlockStarted:
    """描述流式 Assistant 消息的单个内容块开始。

    Attributes:
        message_id: 所属 Assistant 消息标识。
        index: 块在消息中的位置。
        block_id: 流内稳定的块标识。
        block_type: 领域内容块类型。
        tool_name: 工具块的工具名称，其他块可为空。
        tool_call_id: 工具块的调用身份，其他块可为空。
    """

    message_id: str
    index: int
    block_id: str
    block_type: BlockType
    tool_name: str | None = None
    tool_call_id: str | None = None
    type: Literal["message.block.started"] = field(
        default="message.block.started",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageBlockDelta:
    """携带流式 Assistant 内容块的增量，完整消息由定稿事件保存。

    Attributes:
        message_id: 所属 Assistant 消息标识。
        index: 块在消息中的位置。
        block_id: 与开始事件匹配的块标识。
        block_type: 领域内容块类型。
        delta: 本次新增文本或工具参数片段。
    """

    message_id: str
    index: int
    block_id: str
    block_type: BlockType
    delta: str
    type: Literal["message.block.delta"] = field(
        default="message.block.delta",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageBlockCompleted:
    """携带流式 Assistant 内容块的完整结果。

    Attributes:
        message_id: 所属 Assistant 消息标识。
        index: 块在消息中的位置。
        block_id: 与开始事件匹配的块标识。
        block: 归一化的完整 Assistant 内容块。
    """

    message_id: str
    index: int
    block_id: str
    block: AssistantBlock
    type: Literal["message.block.completed"] = field(
        default="message.block.completed",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionStarted:
    """描述 Runtime 开始处理一个 ToolCall；此时未必已调用 Handler。

    Attributes:
        tool_call_id: 模型生成的调用身份。
        tool_name: 请求执行的工具名称。
        arguments: 解析后的参数；解析失败时可为空。
        raw_arguments: 模型原始参数文本。
        parse_error: 参数解析失败的说明。
    """

    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue] | None = None
    raw_arguments: str = ""
    parse_error: str | None = None
    type: Literal["tool.execution.started"] = field(
        default="tool.execution.started",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionCompleted:
    """携带一次工具处理产生的结果和待保存 TOOL 消息。

    Attributes:
        tool_call_id: 原工具调用身份。
        tool_name: 工具名称。
        result: 成功或错误结果块。
        message: 封装结果的定稿 TOOL 消息。
        timing: 本次工具处理的起止时间和耗时。
    """

    tool_call_id: str
    tool_name: str
    result: ToolResultBlock
    message: Message
    timing: Timing
    type: Literal["tool.execution.completed"] = field(
        default="tool.execution.completed",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionSuspended:
    """描述当前唯一挂起工具使 run 进入 PENDING 的事实。

    Attributes:
        tool_call_id: 待恢复的调用身份。
        tool_name: 挂起工具名称。
        resume_token: 本次挂起的单次恢复凭证。
        suspended_at: 工具报告的挂起时间。
        expires_at: 可选的凭证到期时间。
    """

    tool_call_id: str
    tool_name: str
    resume_token: str
    suspended_at: datetime
    expires_at: datetime | None = None
    type: Literal["tool.execution.suspended"] = field(
        default="tool.execution.suspended",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionCancelled:
    """描述未闭合调用由 Runtime 补齐中断结果的事实。

    Attributes:
        tool_call_id: 需补齐结果的调用身份。
        tool_name: 对应工具名称。
        reason_code: 触发终态收敛的错误码。
        message: 包含合成错误结果的 TOOL 消息。
    """

    tool_call_id: str
    tool_name: str
    reason_code: str
    message: Message
    type: Literal["tool.execution.cancelled"] = field(
        default="tool.execution.cancelled",
        init=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSuspended:
    """描述当前执行段因工具挂起结束；run 身份保留至恢复或收敛。

    Attributes:
        resume_token: 当前有效的单次恢复凭证。
        expires_at: 可选恢复期限。
    """

    resume_token: str
    expires_at: datetime | None = None
    type: Literal["run.suspended"] = field(default="run.suspended", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResumed:
    """描述 PENDING run 已在内存快照中清除 token 并回到 RUNNING。

    恢复状态的原子持久化由后续 State 投影完成，本载荷不执行存储操作。
    """

    type: Literal["run.resumed"] = field(default="run.resumed", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCompleted:
    """携带成功终态及统计，供四类投影器按顺序提交。

    Attributes:
        final_message: 最终 Assistant 消息。
        usage: 该 run 累计模型用量。
        timing: 墙钟与实际执行段耗时。
    """

    final_message: Message
    usage: Usage
    timing: RunTiming
    type: Literal["run.completed"] = field(default="run.completed", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunFailed:
    """携带失败终态的结构化原因与累计统计。

    Attributes:
        error: 领域失败原因，公开投影负责选择可公开字段。
        usage: 该 run 累计模型用量。
        timing: 终态计时。
    """

    error: RunError
    usage: Usage
    timing: RunTiming
    type: Literal["run.failed"] = field(default="run.failed", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCanceled:
    """描述 run 已完成取消收敛，等待终态投影。

    Attributes:
        error: 结构化取消原因。
        usage: 取消前累计模型用量。
        timing: 终态计时。
    """

    error: RunError
    usage: Usage
    timing: RunTiming
    type: Literal["run.canceled"] = field(default="run.canceled", init=False)


type AgentEventPayload = (
    RunStarted
    | TurnStarted
    | TurnCompleted
    | MessageStarted
    | MessagesCompleted
    | ContextCompactionStarted
    | ContextCompactionCompleted
    | LLMCallStarted
    | LLMCallCompleted
    | MessageBlockStarted
    | MessageBlockDelta
    | MessageBlockCompleted
    | ToolExecutionStarted
    | ToolExecutionCompleted
    | ToolExecutionSuspended
    | ToolExecutionCancelled
    | RunSuspended
    | RunResumed
    | RunCompleted
    | RunFailed
    | RunCanceled
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentEventEnvelope:
    """为运行事实附加不可变的执行段关联信息，不承担事件投递。

    构造时拒绝负轮次，以及 Turn 事件 envelope 与 payload 的轮次不一致。

    Attributes:
        event_id: 事件唯一身份，用于幂等投影。
        created_at: 事件包装时间。
        session_id: 所属会话身份。
        run_id: 所属内部 run 身份。
        trace_id: 当前执行段的 Runtime Trace 身份。
        payload: 已经形成的领域事实。
        turn_index: 关联轮次；未进入 turn 时可为空。
    """

    event_id: str
    created_at: datetime
    session_id: str
    run_id: str
    trace_id: str
    payload: AgentEventPayload
    turn_index: int | None = None

    def __post_init__(self) -> None:
        """校验 envelope 轮次及 Turn payload 的关联。

        Raises:
            ValueError: 轮次为负，或 Turn envelope 与 payload 轮次不一致。
        """
        if self.turn_index is not None and self.turn_index < 0:
            raise ValueError("turn_index 不能为负数")
        if isinstance(self.payload, (TurnStarted, TurnCompleted)) and (
            self.turn_index != self.payload.turn_index
        ):
            raise ValueError("Turn 事件的 envelope/payload turn_index 不一致")


class AgentEventFactory:
    """为一个执行段包装事件，复用 session/run/trace 关联字段。

    每次 create 独立调用时间和 ID 工厂；事件 ID 的唯一性由注入工厂保证。
    本对象无 I/O，也不承担持久化或投影生命周期。
    """

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        trace_id: str,
        clock: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        """保存执行段身份和可测试的时间及 ID 工厂。

        Args:
            session_id: 非空会话标识。
            run_id: 非空内部 run 标识。
            trace_id: 非空执行段 Trace 标识。
            clock: 事件时间来源；默认使用当前 UTC 时间。
            event_id_factory: 事件身份来源；默认生成 UUID。

        Raises:
            ValueError: 任一关联身份为空。
        """
        if not session_id or not run_id or not trace_id:
            raise ValueError("EventFactory 身份字段不能为空")
        self._session_id = session_id
        self._run_id = run_id
        self._trace_id = trace_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_id_factory = event_id_factory or (lambda: str(uuid4()))

    def create(
        self,
        payload: AgentEventPayload,
        *,
        turn_index: int | None = None,
    ) -> AgentEventEnvelope:
        """包装一个已经发生的事实，不触发任何投影。

        Args:
            payload: Loop 产生的领域事件载荷。
            turn_index: 当前轮次，Turn 事件必须与载荷轮次一致。

        Returns:
            带新事件身份及时间的不可变 envelope。

        Raises:
            ValueError: 轮次为负或与 Turn 载荷不一致。
        """
        return AgentEventEnvelope(
            event_id=self._event_id_factory(),
            created_at=self._clock(),
            session_id=self._session_id,
            run_id=self._run_id,
            trace_id=self._trace_id,
            turn_index=turn_index,
            payload=payload,
        )
