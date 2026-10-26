"""Service 应用边界使用的传输无关命令与结果模型。"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from agentsystem.asset.models import (
    AssetContent as _AssetContent,
)
from agentsystem.asset.models import (
    AssetRecord as _AssetRecord,
)
from agentsystem.common.models import JsonValue, OutputMode, StrictModel
from agentsystem.inbox.models import InboundMode, InboundRole
from agentsystem.llm.models import UsageAccounting
from agentsystem.message.models import UserContentBlock
from agentsystem.runtime.state import SessionStatus

AssetContent = _AssetContent
AssetRecord = _AssetRecord


class ApplicationStatus(StrEnum):
    """表示应用命令受理结果，不替代完整 SessionStatus。

    ACCEPTED 表示请求已受理，PENDING 表示恢复请求已提交等待执行，
    CANCELED 表示取消已经完成或目标此前已取消。
    """

    ACCEPTED = "accepted"
    PENDING = "pending"
    CANCELED = "canceled"


class AgentCatalogItem(StrictModel):
    """Runtime 已成功注册的单个 Agent 公开目录项。

    Attributes:
        agent_id: 已注册 Agent 的稳定标识。
        agent_name: 展示名称。
        description: 公开描述。
        output_mode: 配置的输出模式。
    """

    agent_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    description: str = ""
    output_mode: OutputMode


class AgentCatalogView(StrictModel):
    """当前 Runtime 可执行的确定性 Agent 目录。

    Attributes:
        agents: 按 agent_id 排序的已装配目录项。
    """

    agents: tuple[AgentCatalogItem, ...]


class SubmitMessageCommand(StrictModel):
    """提交一条有序 Inbox 输入消息的应用命令。

    Attributes:
        message_id: 调用方提供的幂等消息身份。
        role: USER 或 SYSTEM 入站角色。
        origin: 消息来源。
        content: 非空文本或媒体内容块。
        mode: QUEUE 在 run 边界领取，STEER 可在工具循环安全点领取。
    """

    message_id: str = Field(min_length=1)
    role: InboundRole
    origin: str
    content: tuple[UserContentBlock, ...] = Field(min_length=1)
    mode: InboundMode = InboundMode.QUEUE


class MessageSubmissionResult(StrictModel):
    """普通消息已由 Inbox 幂等接收。

    Attributes:
        message_id: 已接收的输入身份，不包含内部 run_id。
    """

    message_id: str = Field(min_length=1)


class PendingInputView(StrictModel):
    """尚未完成 Inbox 交接的最小公开输入。

    Attributes:
        message_id: 待处理输入身份。
        role: 入站角色。
        origin: 来源标记。
        content: 文本块拼接的展示内容。
        blocks: 按原顺序公开的内容块。
        mode: 入站调度模式。
        created_at: Inbox 接收时间。
    """

    message_id: str = Field(min_length=1)
    role: InboundRole
    origin: str
    content: str
    blocks: tuple["SessionUserContentBlockView", ...] = Field(min_length=1)
    mode: InboundMode
    created_at: datetime


class PendingInputsView(StrictModel):
    """一个 session 的 FIFO 待执行输入。

    Attributes:
        session_id: 会话标识。
        inputs: 按 Inbox 顺序排列的待交接输入。
    """

    session_id: str = Field(min_length=1)
    inputs: tuple[PendingInputView, ...] = ()


class ResumeCommand(StrictModel):
    """恢复 PENDING session 的应用命令。

    Attributes:
        resume_id: 恢复请求的幂等身份。
        resume_token: 当前挂起提供的单次凭证。
        input: 随恢复请求提交的业务输入映射。
    """

    resume_id: str = Field(min_length=1)
    resume_token: str = Field(min_length=1)
    input: Mapping[str, JsonValue] = Field(default_factory=dict)


class CancelCommand(StrictModel):
    """不包含业务正文的 session 取消应用命令。"""


class SessionPublicResult(StrictModel):
    """不泄漏内部运行标识的 session 公开结果。

    Attributes:
        content: 最终公开文本。
        error_code: 失败或取消的公开错误码。
        error_message: 公开错误说明。
        resume_token: PENDING 的单次恢复凭证。
        suspend_expires_at: 可选恢复期限。
    """

    content: str | None = Field(default=None, min_length=1)
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)
    resume_token: str | None = Field(default=None, min_length=1)
    suspend_expires_at: datetime | None = None


class SessionSnapshot(StrictModel):
    """Runtime 提供的 session 状态与结果快照。

    Attributes:
        session_id: 会话标识。
        status: 最新执行状态。
        result: 公开结果或挂起信息。
        usage: 终态累计模型用量。
        timing: 有完整起止时间时附带的耗时。
    """

    session_id: str = Field(min_length=1)
    status: SessionStatus
    result: SessionPublicResult | None = None
    usage: "SessionTokenUsage | None" = None
    timing: "SessionRunTiming | None" = None


class SessionTokenUsage(StrictModel):
    """终态快照公开的 run 级 token 使用量。

    Attributes:
        input_tokens: 累计输入 token 数。
        output_tokens: 累计输出 token 数。
        total_tokens: 累计总 token 数。
        accounting: 统计完整性。
        reasoning_tokens: 可选推理 token 数。
        cache_read_tokens: 可选缓存读取 token 数。
        cache_write_tokens: 可选缓存写入 token 数。
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    accounting: UsageAccounting = UsageAccounting.PARTIAL
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)


class SessionRunTiming(StrictModel):
    """终态快照公开的墙钟与 active 双口径耗时。

    Attributes:
        started_at: run 首次开始时间。
        ended_at: 终态时间。
        elapsed_ms: 包含挂起等待的墙钟毫秒数。
        active_ms: 累计执行段毫秒数。
    """

    started_at: datetime
    ended_at: datetime
    elapsed_ms: float = Field(ge=0)
    active_ms: float = Field(ge=0)


class SessionTextBlockView(StrictModel):
    """公开消息中的普通文本 Block。

    Attributes:
        type: 固定的 text 类型判别值。
        text: 公开文本正文。
    """

    type: Literal["text"] = "text"
    text: str


class SessionThinkingBlockView(StrictModel):
    """不包含 provider signature 的公开 Thinking Block。

    Attributes:
        type: 固定的 thinking 类型判别值。
        thinking: 公开策略允许展示的推理文本。
    """

    type: Literal["thinking"] = "thinking"
    thinking: str


class SessionCompactionBlockView(StrictModel):
    """不包含内部压缩内容的公开占位 Block。

    Attributes:
        type: 固定的 compaction 判别值，仅保留消息位置。
    """

    type: Literal["compaction"] = "compaction"


class SessionImageBlockView(StrictModel):
    """公开图片来源元数据，不含 URL、Base64 正文或存储 locator。

    Attributes:
        type: 媒体块类型判别值。
        source_type: 原始来源类型。
        media_type: 媒体 MIME 类型。
        filename: Asset 来源的可选文件名。
        asset_id: Asset 来源可供回读的公开身份，其他来源为空。
    """

    type: Literal["image"] = "image"
    source_type: Literal["url", "base64", "asset"]
    media_type: str = Field(min_length=1)
    filename: str | None = None
    asset_id: str | None = None


class SessionAudioBlockView(StrictModel):
    """公开音频来源元数据，不含 URL、Base64 正文或存储 locator。

    Attributes:
        type: 媒体块类型判别值。
        source_type: 原始来源类型。
        media_type: 媒体 MIME 类型。
        filename: Asset 来源的可选文件名。
        asset_id: Asset 来源可供回读的公开身份，其他来源为空。
    """

    type: Literal["audio"] = "audio"
    source_type: Literal["url", "base64", "asset"]
    media_type: str = Field(min_length=1)
    filename: str | None = None
    asset_id: str | None = None


class SessionVideoBlockView(StrictModel):
    """公开视频来源元数据，不含 URL、Base64 正文或存储 locator。

    Attributes:
        type: 媒体块类型判别值。
        source_type: 原始来源类型。
        media_type: 媒体 MIME 类型。
        filename: Asset 来源的可选文件名。
        asset_id: Asset 来源可供回读的公开身份，其他来源为空。
    """

    type: Literal["video"] = "video"
    source_type: Literal["url", "base64", "asset"]
    media_type: str = Field(min_length=1)
    filename: str | None = None
    asset_id: str | None = None


class SessionDocumentBlockView(StrictModel):
    """公开文档来源元数据，不含 URL、Base64 正文或存储 locator。

    Attributes:
        type: 媒体块类型判别值。
        source_type: 原始来源类型。
        media_type: 媒体 MIME 类型。
        filename: Asset 来源的可选文件名。
        asset_id: Asset 来源可供回读的公开身份，其他来源为空。
    """

    type: Literal["document"] = "document"
    source_type: Literal["url", "base64", "asset"]
    media_type: str = Field(min_length=1)
    filename: str | None = None
    asset_id: str | None = None


class SessionToolCallBlockView(StrictModel):
    """公开工具调用身份，以及由 PublicContentPolicy 筛选和截断的参数。

    Attributes:
        type: 固定的 tool_call 类型判别值。
        tool_call_id: 关联公开工具结果的调用身份。
        tool_name: 工具名称。
        arguments: 公开策略允许且未截断的结构化参数。
        raw_arguments: 按策略截断后的参数文本。
        truncated: 参数是否发生截断。
    """

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue] | None = None
    raw_arguments: str | None = None
    truncated: bool = False


class SessionToolResultBlockView(StrictModel):
    """用于历史复现的公开 Tool Result Block。

    Attributes:
        type: 固定的 tool_result 类型判别值。
        tool_call_id: 对应调用身份。
        tool_name: 工具名称。
        content: 公开策略允许的结果文本。
        is_error: 工具是否报告错误。
        truncated: 结果是否发生截断。
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    content: str | None = None
    is_error: bool = False
    truncated: bool = False


type SessionContentBlockView = Annotated[
    SessionTextBlockView
    | SessionThinkingBlockView
    | SessionCompactionBlockView
    | SessionImageBlockView
    | SessionAudioBlockView
    | SessionVideoBlockView
    | SessionDocumentBlockView
    | SessionToolCallBlockView
    | SessionToolResultBlockView,
    Field(discriminator="type"),
]

type SessionUserContentBlockView = Annotated[
    SessionTextBlockView
    | SessionImageBlockView
    | SessionAudioBlockView
    | SessionVideoBlockView
    | SessionDocumentBlockView,
    Field(discriminator="type"),
]


class SessionMessageView(StrictModel):
    """保留 Block 顺序并过滤内部字段的公开消息视图。

    Attributes:
        message_id: 消息身份。
        role: 消息角色。
        origin: 来源标记。
        content: 文本或工具结果拼接的展示内容。
        blocks: 按原顺序排列的公开内容块。
        created_at: 消息创建时间。
        is_error: 是否含错误工具结果。
    """

    message_id: str = Field(min_length=1)
    role: Literal["user", "system", "assistant", "tool"]
    origin: str
    content: str
    blocks: tuple[SessionContentBlockView, ...] = Field(min_length=1)
    created_at: datetime
    is_error: bool = False


class SessionMessagesView(StrictModel):
    """一个 session 的稳定公开消息视图。

    Attributes:
        session_id: 会话身份。
        messages: 过滤可见性并应用公开策略后的消息序列。
    """

    session_id: str = Field(min_length=1)
    messages: tuple[SessionMessageView, ...] = ()
