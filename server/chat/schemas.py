"""公开 API 请求与响应模型；不复用内部 run DTO。"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from agentsystem.common.models import JsonValue, OutputMode, StrictModel
from agentsystem.inbox.models import InboundMode, InboundRole
from agentsystem.llm.models import UsageAccounting
from agentsystem.message.models import DOCUMENT_MEDIA_TYPES
from agentsystem.todo.models import TodoItem
from pydantic import Field, model_validator


class SessionTodosResponse(StrictModel):
    """当前完整清单，不包含存储元数据。"""

    todos: tuple[TodoItem, ...]


class ErrorResponse(StrictModel):
    """同步 HTTP 请求的统一安全错误结构。"""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    error_id: str | None = None


class ApiSessionStatus(StrEnum):
    """公开提交操作的 session 级状态。"""

    ACCEPTED = "accepted"
    PENDING = "pending"
    CANCELED = "canceled"


class SessionStatus(StrEnum):
    """HTTP 快照公开的 session 状态值。"""

    IDLE = "idle"
    RUNNING = "running"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentCatalogItemResponse(StrictModel):
    """Web 可选择的单个 Agent。"""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    output_mode: OutputMode


class AgentCatalogResponse(StrictModel):
    """当前 Runtime 成功注册的 Agent 集合。"""

    agents: tuple[AgentCatalogItemResponse, ...]


class InputUrlSource(StrictModel):
    """HTTP 入站 URL 媒体来源。"""

    type: Literal["url"] = "url"
    url: str = Field(min_length=1)
    media_type: str = Field(min_length=1)


class InputBase64Source(StrictModel):
    """HTTP 入站纯 Base64 媒体来源。"""

    type: Literal["base64"] = "base64"
    data: str = Field(min_length=1)
    media_type: str = Field(min_length=1)


class InputAssetSource(StrictModel):
    """HTTP 入站 Runtime 资产引用。"""

    type: Literal["asset"] = "asset"
    asset_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    filename: str | None = None


class AssetUploadResponse(StrictModel):
    """Runtime Store 写入结果及可直接用于消息的 AssetSource。"""

    asset_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    filename: str | None = None
    size_bytes: int = Field(gt=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: InputAssetSource


type InputMediaSource = Annotated[
    InputUrlSource | InputBase64Source | InputAssetSource,
    Field(discriminator="type"),
]


class InputTextBlock(StrictModel):
    """HTTP 入站文本 Block。"""

    type: Literal["text"] = "text"
    text: str


class InputImageBlock(StrictModel):
    """HTTP 入站图片 Block。"""

    type: Literal["image"] = "image"
    source: InputMediaSource
    detail: Literal["auto", "low", "high"] = "auto"

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求图片 MIME 主类型匹配。"""
        if not self.source.media_type.lower().startswith("image/"):
            raise ValueError("图片 MIME 不匹配")
        return self


class InputAudioBlock(StrictModel):
    """HTTP 入站音频 Block。"""

    type: Literal["audio"] = "audio"
    source: InputMediaSource

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求音频 MIME 主类型匹配。"""
        if not self.source.media_type.lower().startswith("audio/"):
            raise ValueError("音频 MIME 不匹配")
        return self


class InputVideoBlock(StrictModel):
    """HTTP 入站视频 Block。"""

    type: Literal["video"] = "video"
    source: InputMediaSource

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求视频 MIME 主类型匹配。"""
        if not self.source.media_type.lower().startswith("video/"):
            raise ValueError("视频 MIME 不匹配")
        return self


class InputDocumentBlock(StrictModel):
    """HTTP 入站文档 Block。"""

    type: Literal["document"] = "document"
    source: InputMediaSource

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """在 HTTP 校验层拒绝领域模型不接受的文档 MIME。"""
        if self.source.media_type.lower() not in DOCUMENT_MEDIA_TYPES:
            raise ValueError("文档 MIME 不受支持")
        return self


type InputContentBlock = Annotated[
    InputTextBlock
    | InputImageBlock
    | InputAudioBlock
    | InputVideoBlock
    | InputDocumentBlock,
    Field(discriminator="type"),
]


class MessageSubmissionRequest(StrictModel):
    """提交一条文本/媒体混合消息所需的公开字段。"""

    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    role: InboundRole = InboundRole.USER
    origin: str | None = None
    content: tuple[InputContentBlock, ...] = Field(min_length=1)
    mode: InboundMode = InboundMode.QUEUE


class ResumeSubmissionRequest(StrictModel):
    """提交 opaque continuation 的公开字段。"""

    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    resume_id: str = Field(min_length=1)
    resume_token: str = Field(min_length=1)
    input: Mapping[str, JsonValue] = Field(default_factory=dict)


class CancelSessionRequest(StrictModel):
    """按 session 发起取消的公开身份字段。"""

    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


class SessionStatusResponse(StrictModel):
    """所有提交类接口共用的最小响应。"""

    session_id: str = Field(min_length=1)
    status: ApiSessionStatus


class MessageSubmissionResponse(StrictModel):
    """普通消息进入 Inbox 后的最小幂等响应。"""

    session_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)


class PendingInputResponse(StrictModel):
    """待执行区所需的最小输入字段。"""

    message_id: str = Field(min_length=1)
    role: InboundRole
    origin: str
    content: str
    blocks: tuple["SessionUserContentBlockResponse", ...] = Field(min_length=1)
    mode: InboundMode
    created_at: datetime


class PendingInputsResponse(StrictModel):
    """session Inbox 的公开 FIFO 快照。"""

    session_id: str = Field(min_length=1)
    inputs: tuple[PendingInputResponse, ...] = ()


class SessionSnapshotResponse(StrictModel):
    """不包含 run/trace 标识的公开状态与非流式结果快照。"""

    session_id: str = Field(min_length=1)
    status: SessionStatus
    result: "SessionPublicResultResponse | None" = None
    usage: "SessionTokenUsageResponse | None" = None
    timing: "SessionRunTimingResponse | None" = None


class SessionTokenUsageResponse(StrictModel):
    """终态 HTTP 快照中的 token 使用量。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    accounting: UsageAccounting = UsageAccounting.PARTIAL
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)


class SessionRunTimingResponse(StrictModel):
    """终态 HTTP 快照中的墙钟与 active 双口径耗时。"""

    started_at: datetime
    ended_at: datetime
    elapsed_ms: float = Field(ge=0)
    active_ms: float = Field(ge=0)


class SessionPublicResultResponse(StrictModel):
    """从内部 PublicRunResult 脱敏得到的 HTTP 公开结果。"""

    content: str | None = Field(default=None, min_length=1)
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)
    resume_token: str | None = Field(default=None, min_length=1)
    suspend_expires_at: datetime | None = None


class SessionTextBlockResponse(StrictModel):
    """公开消息中的普通文本 Block。"""

    type: Literal["text"] = "text"
    text: str


class SessionThinkingBlockResponse(StrictModel):
    """不包含 provider signature 的 Thinking Block。"""

    type: Literal["thinking"] = "thinking"
    thinking: str


class SessionCompactionBlockResponse(StrictModel):
    """上下文压缩在公开消息序列中的安全占位。"""

    type: Literal["compaction"] = "compaction"


class SessionImageBlockResponse(StrictModel):
    """仅为 Asset 公开回读 ID 的图片 Block。"""

    type: Literal["image"] = "image"
    source_type: Literal["url", "base64", "asset"]
    media_type: str
    filename: str | None = None
    asset_id: str | None = None


class SessionAudioBlockResponse(StrictModel):
    """仅为 Asset 公开回读 ID 的音频 Block。"""

    type: Literal["audio"] = "audio"
    source_type: Literal["url", "base64", "asset"]
    media_type: str
    filename: str | None = None
    asset_id: str | None = None


class SessionVideoBlockResponse(StrictModel):
    """仅为 Asset 公开回读 ID 的视频 Block。"""

    type: Literal["video"] = "video"
    source_type: Literal["url", "base64", "asset"]
    media_type: str
    filename: str | None = None
    asset_id: str | None = None


class SessionDocumentBlockResponse(StrictModel):
    """仅为 Asset 公开回读 ID 的文档 Block。"""

    type: Literal["document"] = "document"
    source_type: Literal["url", "base64", "asset"]
    media_type: str
    filename: str | None = None
    asset_id: str | None = None


class SessionToolCallBlockResponse(StrictModel):
    """只公开工具名称的 ToolCall Block。"""

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue] | None = None
    raw_arguments: str | None = None
    truncated: bool = False


class SessionToolResultBlockResponse(StrictModel):
    """用于消息历史复现的 ToolResult Block。"""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    content: str | None = None
    is_error: bool = False
    truncated: bool = False


type SessionContentBlockResponse = Annotated[
    SessionTextBlockResponse
    | SessionThinkingBlockResponse
    | SessionCompactionBlockResponse
    | SessionImageBlockResponse
    | SessionAudioBlockResponse
    | SessionVideoBlockResponse
    | SessionDocumentBlockResponse
    | SessionToolCallBlockResponse
    | SessionToolResultBlockResponse,
    Field(discriminator="type"),
]

type SessionUserContentBlockResponse = Annotated[
    SessionTextBlockResponse
    | SessionImageBlockResponse
    | SessionAudioBlockResponse
    | SessionVideoBlockResponse
    | SessionDocumentBlockResponse,
    Field(discriminator="type"),
]


class SessionMessageResponse(StrictModel):
    """保留脱敏 Block 顺序的公开消息视图。"""

    message_id: str = Field(min_length=1)
    role: Literal["user", "system", "assistant", "tool"]
    origin: str
    content: str
    blocks: tuple[SessionContentBlockResponse, ...] = Field(min_length=1)
    created_at: datetime
    is_error: bool = False


class SessionMessagesResponse(StrictModel):
    """一个 session 的稳定公开消息快照。"""

    session_id: str = Field(min_length=1)
    messages: tuple[SessionMessageResponse, ...] = ()
