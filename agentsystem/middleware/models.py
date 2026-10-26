"""Middleware 调用点、payload、决策和注册领域模型。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentsystem.common.errors import ErrorOperation, RunError
from agentsystem.common.models import OutputMode
from agentsystem.llm.models import LLMRequest, LLMResponse
from agentsystem.message.models import Message, ToolCallBlock, ToolResultBlock

if TYPE_CHECKING:
    from agentsystem.common.models import SessionContext
    from agentsystem.runtime.state import AgentState, SessionStatus


class MiddlewarePoint(StrEnum):
    """Runtime 中允许执行 Middleware 的稳定挂点。"""

    BEFORE_SYSTEM_PROMPT_BUILD = "before_system_prompt_build"
    AFTER_SYSTEM_PROMPT_BUILD = "after_system_prompt_build"
    BEFORE_RUN = "before_run"
    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"
    BEFORE_LLM = "before_llm"
    AFTER_LLM = "after_llm"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    BEFORE_COMPACTION = "before_compaction"
    AFTER_RUN = "after_run"
    ON_ERROR = "on_error"


class MiddlewareDecisionType(StrEnum):
    """Middleware 对当前 Runtime 步骤的控制结果。"""

    CONTINUE = "continue"
    UPDATE = "update"
    MODIFY = "modify"
    BLOCK = "block"
    REPLACE = "replace"
    RETRY = "retry"
    HANDLED = "handled"
    MESSAGES = "messages"


class MiddlewareScope(StrEnum):
    """Middleware 注册的应用范围。"""

    GLOBAL = "global"
    APP = "app"


class MiddlewareFailureMode(StrEnum):
    """Handler 抛错或超时时的放行策略。"""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemPromptBuildPayload:
    """系统提示词构建挂点的不可变输入或替换结果。

    Attributes:
        prompt: before 的基础正文、after 的完整正文或 UPDATE 的完整替换正文。
    """

    prompt: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunMiddlewarePayload:
    """BEFORE_RUN 可见的已保存初始消息及输出方式。

    Attributes:
        messages: 当前 run 领取并保存的初始消息。
        output_mode: 当前请求采用的模型输出方式。
    """

    messages: tuple[Message, ...]
    output_mode: OutputMode


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResultMiddlewarePayload:
    """AFTER_RUN 可观察的已保存终态结果，只用于观察和后续副作用。

    Attributes:
        status: SUCCEEDED、FAILED 或 CANCELED 终态。
        message: 可选的最终 Assistant Message。
        error: 可选的脱敏运行错误。
    """

    status: SessionStatus
    message: Message | None = None
    error: RunError | None = None

    def __post_init__(self) -> None:
        """校验 AFTER_RUN 只接受已经收敛的终态。

        Raises:
            ValueError: status 不是成功、失败或取消终态。
        """
        from agentsystem.runtime.state import SessionStatus

        if self.status not in {
            SessionStatus.SUCCEEDED,
            SessionStatus.FAILED,
            SessionStatus.CANCELED,
        }:
            raise ValueError("RunResultMiddlewarePayload 只接受终态")


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnMiddlewarePayload:
    """Turn 挂载点可见的持久化逻辑消息快照。

    Attributes:
        messages: 按存储顺序排列的消息，不是 Provider 发送副本。
    """

    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MessagesMiddlewarePayload:
    """等待 Runtime 持久化追加的完整消息，不自行写入 Manager。

    Attributes:
        messages: 非空的模型可见 USER 消息序列，由 MiddlewareManager 校验身份。
    """

    messages: tuple[Message, ...]

    def __post_init__(self) -> None:
        """拒绝与 CONTINUE 等价的空结果。

        Raises:
            ValueError: messages 为空。
        """
        if not self.messages:
            raise ValueError("MessagesMiddlewarePayload.messages 不能为空")


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallMiddlewarePayload:
    """Tool 执行前供 Middleware 检查或修改的单个调用。

    Attributes:
        call: 调用块；修改参数时必须保留工具名与调用 ID。
    """

    call: ToolCallBlock


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResultMiddlewarePayload:
    """Tool 执行后或被替代执行时的调用与结果对。

    Attributes:
        call: 原工具调用身份。
        result: 与调用工具名、调用 ID 对应的结果块。
    """

    call: ToolCallBlock
    result: ToolResultBlock


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionMiddlewarePayload:
    """摘要调用前已经确定的不可变边界，供策略观察或阻止摘要。

    Attributes:
        model: 本次摘要使用的模型。
        compacted_message_ids: 新快照累计覆盖的消息 ID。
        before_tokens: 压缩前完整请求的计量值。
        trigger_tokens: 触发压缩的 token 阈值。
        recent_message_budget_tokens: 最近历史的软预算。
        retained_message_tokens: 实际保留历史的估算 token 数。
    """

    model: str
    compacted_message_ids: tuple[str, ...]
    before_tokens: int
    trigger_tokens: int
    recent_message_budget_tokens: int
    retained_message_tokens: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorMiddlewarePayload:
    """交给 ON_ERROR 的脱敏错误上下文。

    Attributes:
        operation: 失败的操作类别，用于限制恢复决策。
        error: 可安全传递的运行错误。
        tool_call: 工具失败时的原始调用，恢复结果必须保留其身份。
    """

    operation: ErrorOperation
    error: RunError
    tool_call: ToolCallBlock | None = None


type MiddlewarePayload = (
    SystemPromptBuildPayload
    | RunMiddlewarePayload
    | RunResultMiddlewarePayload
    | TurnMiddlewarePayload
    | MessagesMiddlewarePayload
    | LLMRequest
    | LLMResponse
    | ToolCallMiddlewarePayload
    | ToolResultMiddlewarePayload
    | CompactionMiddlewarePayload
    | ErrorMiddlewarePayload
)


@dataclass(frozen=True, slots=True, kw_only=True)
class MiddlewareContext:
    """传给单个 Handler 的调用快照，不授予直接写入运行态的权限。

    Manager 为每个 handler 复制 state_snapshot；初始化挂点不携带 run 身份。

    Attributes:
        invocation_id: Manager 根据调用位置和注册 ID 生成的稳定标识。
        point: 本次执行的 Middleware 挂点。
        ctx: 当前会话身份。
        run_id: Runtime run 标识；初始化时为空。
        trace_id: Runtime trace 标识；初始化时为空。
        turn_index: 可选的 turn 序号。
        attempt: 当前尝试次数，参与 invocation_id 生成。
        state_snapshot: 当前状态的独立快照；初始化时为空。
        payload: 挂点对应的输入，可能包含前序 handler 的修改。
    """

    invocation_id: str
    point: MiddlewarePoint
    ctx: SessionContext
    run_id: str | None
    trace_id: str | None
    turn_index: int | None = None
    attempt: int = 1
    state_snapshot: AgentState | None
    payload: MiddlewarePayload

    def __post_init__(self) -> None:
        """初始化挂点不伪造 run；其他挂点必须携带完整运行身份。

        Raises:
            ValueError: 初始化挂点带运行态，或运行期挂点缺少身份、状态快照。
        """
        if self.point in {
            MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD,
            MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
        }:
            if any(
                value is not None
                for value in (
                    self.run_id,
                    self.trace_id,
                    self.state_snapshot,
                    self.turn_index,
                )
            ):
                raise ValueError("system prompt hooks must not carry run state")
        elif not self.run_id or not self.trace_id or self.state_snapshot is None:
            raise ValueError("runtime middleware requires run identity and state")


@dataclass(frozen=True, slots=True, kw_only=True)
class MiddlewareDecision:
    """一个 Handler 返回的控制决策，由 Manager 校验挂点约束。

    Attributes:
        type: 继续、修改、阻止或恢复等决策类别。
        payload: 决策所需的替换数据或追加消息，允许类型随挂点变化。
        reason_code: 可选的机器可读原因。
        reason: 可选的人可读原因。
    """

    type: MiddlewareDecisionType
    payload: MiddlewarePayload | None = None
    reason_code: str | None = None
    reason: str | None = None


type MiddlewareHandler = Callable[
    [MiddlewareContext],
    Awaitable[MiddlewareDecision],
]


@dataclass(frozen=True, slots=True)
class MiddlewareRegistration:
    """启动阶段注册、冻结后供 Manager 调度的元数据与 Handler。

    不自行执行 handler 或持有外部连接，handler 的资源由注册方管理。

    Attributes:
        name: Registry 内唯一的能力名称。
        middleware_id: Registry 内唯一的 handler 标识，也用于稳定排序。
        point: 执行挂点。
        scope: 全局或应用范围。
        handler: 接收调用快照并异步返回决策的函数。
        priority: 调度优先级，数值越大越先执行。
        app_id: APP 范围必填的应用 ID；GLOBAL 范围必须为空。
        timeout_ms: 单次 handler 调用的毫秒上限。
        failure_mode: handler 超时或抛错时的放行策略。
    """

    name: str
    middleware_id: str
    point: MiddlewarePoint
    scope: MiddlewareScope
    handler: MiddlewareHandler
    priority: int = 0
    app_id: str | None = None
    timeout_ms: int = 1_000
    failure_mode: MiddlewareFailureMode = MiddlewareFailureMode.FAIL_OPEN
