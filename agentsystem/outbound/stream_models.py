"""浏览器可见的版本化 Stream event、cursor 与读取记录。"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from agentsystem.common.errors import RunError
from agentsystem.common.models import FinishReason, JsonValue, StrictModel
from agentsystem.llm.models import Usage
from agentsystem.message.models import BlockType
from agentsystem.runtime.state import RunTiming


class PublicStreamLifecycle(StrEnum):
    """一代公开 Stream 的写入生命周期，区分活动流与已出现终态的流。"""

    OPEN = "open"
    TERMINAL = "terminal"


class PublicEventCursor(StrictModel):
    """包含 Stream epoch 和 Redis 有序 ID 的不透明游标。

    Attributes:
        epoch: 一代流的 UUID，流重建后旧游标失效。
        stream_id: 毫秒部分与序号组成的有序 ID。
    """

    epoch: UUID
    stream_id: str = Field(pattern=r"^(0|[1-9]\d*)-(0|[1-9]\d*)$")

    @classmethod
    def parse(cls, value: str) -> "PublicEventCursor":
        """解析 `epoch:redis-id` 并校验组成字段。

        Args:
            value: 传输层接收到的完整游标文本。

        Returns:
            结构化游标。

        Raises:
            ValueError: 分隔符、UUID 或 Stream ID 无效。
        """
        epoch, separator, stream_id = value.partition(":")
        if not separator or not epoch or not stream_id:
            raise ValueError("cursor 格式必须为 <epoch>:<redis-stream-id>")
        return cls(epoch=UUID(epoch), stream_id=stream_id)

    def render(self) -> str:
        """返回稳定的传输格式。

        Returns:
            由规范 UUID 和 Stream ID 拼接的游标字符串。
        """
        return f"{self.epoch}:{self.stream_id}"


class RunStartedPayload(StrictModel):
    """公开 run 已开始的载荷，status 固定为 running，不携带内部 run_id。"""

    status: Literal["running"] = "running"


class StartedTextBlock(StrictModel):
    """message.started 中可公开的输入文本块。

    Attributes:
        type: 固定 text 判别值。
        text: 原始输入文本。
    """

    type: Literal["text"] = "text"
    text: str


class StartedMediaBlock(StrictModel):
    """message.started 中的媒体元数据，不包含 URL 或 Base64 正文。

    Attributes:
        type: 媒体块类型。
        source_type: 原始来源类型。
        media_type: 声明或归档后的 MIME。
        filename: 资产来源的可选展示文件名。
        asset_id: 资产来源的标识，其他来源为空。
    """

    type: Literal["image", "audio", "video", "document"]
    source_type: Literal["url", "base64", "asset"]
    media_type: str = Field(min_length=1)
    filename: str | None = None
    asset_id: str | None = None


class StartedPublicMessage(StrictModel):
    """已进入权威历史的最小 Inbox 输入消息公开快照。

    Attributes:
        message_id: 权威消息标识。
        role: 公开输入角色，限 user 或 system。
        origin: 消息来源标签。
        content: 按顺序以换行连接的文本块内容。
        blocks: 保持原顺序的公开文本及媒体元数据。
        created_at: 消息创建时间。
        is_error: 输入消息固定为 False。
    """

    message_id: str = Field(min_length=1)
    role: Literal["user", "system"]
    origin: str
    content: str
    blocks: tuple[StartedTextBlock | StartedMediaBlock, ...] = Field(min_length=1)
    created_at: datetime
    is_error: Literal[False] = False


class MessageStartedPayload(StrictModel):
    """输入消息已保存并完成 Inbox 交接的公开载荷。

    Attributes:
        mode: 本次领取使用 queue 或 steer 模式。
        message: 已保存输入消息的公开快照。
    """

    mode: Literal["queue", "steer"]
    message: StartedPublicMessage


class TurnStartedPayload(StrictModel):
    """公开 turn 已开始的载荷，turn_index 为从一开始的轮次编号。"""

    turn_index: int = Field(gt=0)


class ContextCompactionStartedPayload(StrictModel):
    """公开上下文压缩已开始的空载荷，不暴露被选择的上下文内容。"""


class ContextCompactionCompletedPayload(StrictModel):
    """公开上下文压缩结果，不暴露摘要内容。

    Attributes:
        message_id: 压缩消息标识，可为空。
        action: 仅裁剪或生成摘要的实际动作。
        before_tokens: 压缩前估算 token 数。
        after_tokens: 压缩后估算 token 数。
        usage: 摘要调用的使用量。
    """

    message_id: str | None = Field(default=None, min_length=1)
    action: Literal["pruned", "summarized"]
    before_tokens: int = Field(ge=0)
    after_tokens: int = Field(ge=0)
    usage: Usage


class ModelStartedPayload(StrictModel):
    """公开模型调用已开始的载荷。

    Attributes:
        provider_name: 稳定提供方名称。
        model: 请求的模型标识。
    """

    provider_name: str = Field(min_length=1)
    model: str = Field(min_length=1)


class ModelCompletedPayload(StrictModel):
    """公开模型调用已完成的载荷，不包含完整模型响应。

    Attributes:
        model: 响应中归一化的模型名。
        finish_reason: 统一结束原因。
    """

    model: str = Field(min_length=1)
    finish_reason: FinishReason


class BlockStartedPayload(StrictModel):
    """公开 Assistant block 边界开始的载荷。

    Attributes:
        message_id: 所属 Assistant 消息标识。
        block_id: 关联增量和结束事件的块标识。
        index: 消息内从零开始的块序号。
        block_type: 块的规范类型。
        tool_name: 工具块的工具名，其他块为空。
        tool_call_id: 工具块的调用标识，其他块为空。
    """

    message_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    block_type: BlockType
    tool_name: str | None = None
    tool_call_id: str | None = None


class BlockDeltaPayload(StrictModel):
    """公开 Assistant block 增量，供客户端即时展示。

    Attributes:
        message_id: 所属消息标识。
        block_id: 所属块标识。
        index: 消息内块序号。
        block_type: 块类型。
        delta: 本次追加的文本或参数片段。
    """

    message_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    block_type: BlockType
    delta: str


class BlockCompletedPayload(StrictModel):
    """公开 Assistant block 已闭合的载荷。

    Attributes:
        message_id: 所属消息标识。
        block_id: 已完成块的标识。
        index: 消息内块序号。
        block_type: 块类型。
        content: 完整文本或允许公开的思考内容，工具块为空。
        tool_name: 工具块的工具名称。
        tool_call_id: 工具块的调用标识。
        arguments: 按策略脱敏且未超限的工具参数，否则为空。
    """

    message_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    block_type: BlockType
    content: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: Mapping[str, JsonValue] | None = None


class ToolStartedPayload(StrictModel):
    """公开 Tool 执行开始的载荷。

    Attributes:
        tool_call_id: 工具调用标识。
        tool_name: 工具名称。
        arguments: 允许公开且未截断的脱敏参数。
        raw_arguments: 脱敏参数的 JSON 文本，可截断；并非原始模型片段。
        truncated: 参数 JSON 是否达到公开字节上限并发生截断。
    """

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue] | None = None
    raw_arguments: str | None = None
    truncated: bool = False


class ToolCompletedPayload(StrictModel):
    """公开 Tool 返回结果的载荷，业务错误仍属于正常完成。

    Attributes:
        tool_call_id: 工具调用标识。
        tool_name: 工具名称。
        content: 按公开策略选择和截断的结果原文。
        is_error: 工具结果是否报告错误。
        error_code: 可选工具错误码。
        truncated: 结果正文是否截断。
        duration_ms: 工具执行耗时毫秒数。
    """

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    content: str | None = None
    is_error: bool = False
    error_code: str | None = None
    truncated: bool = False
    duration_ms: float = Field(ge=0)


class ToolFailedPayload(StrictModel):
    """公开 Tool 被取消或未完成的载荷。

    Attributes:
        tool_call_id: 工具调用标识。
        tool_name: 工具名称。
        reason_code: 未完成原因的稳定代码。
    """

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)


class RunSuspendedPayload(StrictModel):
    """公开 run 已进入 PENDING 的恢复信息。

    Attributes:
        status: 固定 pending 状态。
        resume_token: 单次恢复 token。
        expires_at: 可选的挂起到期时刻。
    """

    status: Literal["pending"] = "pending"
    resume_token: str = Field(min_length=1)
    expires_at: datetime | None = None


class RunResumedPayload(StrictModel):
    """公开 run 已从 PENDING 恢复的载荷，status 固定为 running。"""

    status: Literal["running"] = "running"


class RunTerminalPayload(StrictModel):
    """成功、失败或取消 run 的公开终态提示。

    客户端仍需读取权威快照完成同步，不能只靠此事件重建完整状态。

    Attributes:
        status: 成功、失败或取消状态。
        final_message_id: 成功时的最终消息标识。
        error: 失败或取消的结构化错误。
        usage: run 累计模型用量。
        timing: run 的时间统计。
        snapshot_required: 固定为 True，指示客户端查询快照。
    """

    status: Literal["succeeded", "failed", "canceled"]
    final_message_id: str | None = None
    error: RunError | None = None
    usage: Usage
    timing: RunTiming
    snapshot_required: Literal[True] = True


type PublicEventPayload = (
    MessageStartedPayload
    | RunStartedPayload
    | TurnStartedPayload
    | ContextCompactionStartedPayload
    | ContextCompactionCompletedPayload
    | ModelStartedPayload
    | ModelCompletedPayload
    | BlockStartedPayload
    | BlockDeltaPayload
    | BlockCompletedPayload
    | ToolStartedPayload
    | ToolCompletedPayload
    | ToolFailedPayload
    | RunSuspendedPayload
    | RunResumedPayload
    | RunTerminalPayload
)

type PublicEventType = Literal[
    "message.started",
    "run.started",
    "turn.started",
    "context.compaction.started",
    "context.compaction.completed",
    "model.started",
    "message.block.started",
    "message.block.delta",
    "message.block.completed",
    "model.completed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "run.suspended",
    "run.resumed",
    "run.completed",
    "run.failed",
    "run.canceled",
]


class PublicStreamEvent(StrictModel):
    """写入 EventBus 的版本化公开事件，不包含内部 run_id 或 trace_id。

    Attributes:
        schema_version: 当前协议版本，固定为 1。
        event_id: 从内部事件标识稳定派生的公开 ID。
        type: 公开事件名称，决定 payload 的具体类型。
        created_at: 原始事件时间。
        session_id: 所属会话标识。
        payload: 与事件名称匹配的公开载荷。
    """

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1)
    type: PublicEventType
    created_at: datetime
    session_id: str = Field(min_length=1)
    payload: PublicEventPayload

    @model_validator(mode="before")
    @classmethod
    def select_payload_model(cls, data: object) -> object:
        """使用外层 event type 消除结构相同 payload 的反序列化歧义。

        Args:
            data: Pydantic 接收到的原始输入。

        Returns:
            已将字典 payload 转为具体模型的浅拷贝，或不适用转换时的原输入。

        Raises:
            ValueError: 指定类型的 payload 校验失败。
        """
        if not isinstance(data, dict):
            return data
        event_type = data.get("type")
        raw_payload = data.get("payload")
        payload_model = _PAYLOAD_MODELS.get(event_type)
        if payload_model is None or not isinstance(raw_payload, dict):
            return data
        normalized = dict(data)
        normalized["payload"] = payload_model.model_validate(raw_payload)
        return normalized

    @model_validator(mode="after")
    def validate_payload_type(self) -> Self:
        """校验外层事件名对应的明确 payload 类型。

        Returns:
            校验通过的当前事件。

        Raises:
            ValueError: payload 类型不匹配外层事件名。
        """
        expected = _PAYLOAD_MODELS[self.type]
        if not isinstance(self.payload, expected):
            raise ValueError(f"{self.type} 使用了不匹配的 payload")
        return self


class PublicEventRecord(StrictModel):
    """带 EventBus 有序 cursor 的公开事件记录。

    Attributes:
        cursor: 总线分配的世代和有序位置。
        event: 对应的版本化公开事件。
    """

    cursor: PublicEventCursor
    event: PublicStreamEvent


_PAYLOAD_MODELS: dict[object, type[StrictModel]] = {
    "message.started": MessageStartedPayload,
    "run.started": RunStartedPayload,
    "turn.started": TurnStartedPayload,
    "context.compaction.started": ContextCompactionStartedPayload,
    "context.compaction.completed": ContextCompactionCompletedPayload,
    "model.started": ModelStartedPayload,
    "model.completed": ModelCompletedPayload,
    "message.block.started": BlockStartedPayload,
    "message.block.delta": BlockDeltaPayload,
    "message.block.completed": BlockCompletedPayload,
    "tool.started": ToolStartedPayload,
    "tool.completed": ToolCompletedPayload,
    "tool.failed": ToolFailedPayload,
    "run.suspended": RunSuspendedPayload,
    "run.resumed": RunResumedPayload,
    "run.completed": RunTerminalPayload,
    "run.failed": RunTerminalPayload,
    "run.canceled": RunTerminalPayload,
}
