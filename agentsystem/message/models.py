"""提供方无关的 Message、Block 与 Context 数据模型。"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from agentsystem.common.errors import RunError
from agentsystem.common.models import FinishReason, FrozenModel, JsonValue


class Role(StrEnum):
    """定义消息的模型协议角色，用于约束允许的内容块组合。

    Attributes:
        SYSTEM: 基础系统提示或运行期间的系统输入。
        USER: 用户输入，以及单独保存的注入或压缩内部消息。
        ASSISTANT: 模型输出文本、思考或工具调用。
        TOOL: 单个工具调用结果。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageVisibility(StrEnum):
    """定义消息可进入的消费边界，不代表存储权限或脱敏策略。

    Attributes:
        MODEL: 仅供模型上下文消费。
        PUBLIC: 仅供公开投影消费。
        ALL: 模型上下文及公开投影均可消费。
    """

    MODEL = "model"
    PUBLIC = "public"
    ALL = "all"


class MessageOrigin(StrEnum):
    """列出内置消息生产方，与模型 role 分开表达来源。

    SYSTEM、USER、MODEL、HUMAN 和 TOOL 标记对应生产方；
    CONTEXT_PROVIDER 与 COMPACTOR 标记内部上下文消息，SERVICE 标记服务。
    Message.origin 本身为字符串，不限于本枚举。
    """

    SYSTEM = "system"
    USER = "user"
    MODEL = "model"
    HUMAN = "human"
    TOOL = "tool"
    CONTEXT_PROVIDER = "context_provider"
    COMPACTOR = "compactor"
    SERVICE = "service"


class BlockType(StrEnum):
    """列出持久化内容块的判别标识，不承担块实例的校验。

    包括模型文本、思考、工具调用及结果，内部压缩及注入快照，
    以及图片、音频、视频和文档媒体输入。
    """

    THINKING = "thinking"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMPACTION = "compaction"
    INJECTED = "injected"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class MediaSourceType(StrEnum):
    """列出媒体定位方式，不执行下载、Base64 解码或资产查询。

    Attributes:
        URL: 外部地址引用。
        BASE64: 内联媒体文本。
        ASSET: Runtime 资产引用。
    """

    URL = "url"
    BASE64 = "base64"
    ASSET = "asset"


class TextBlock(FrozenModel):
    """普通文本内容块；作为不可变值随所属 Message 保存。

    Attributes:
        type: 固定为 text，供内容块联合类型判别。
        text: 原始文本，不在模型层执行裁剪或转义。
    """

    type: Literal["text"] = "text"
    text: str


class UrlSource(FrozenModel):
    """以 URL 引用媒体的不可变定位信息，不在模型校验时发起网络请求。

    Attributes:
        type: 固定为 url。
        url: 非空媒体地址；抓取和安全校验由媒体处理边界负责。
        media_type: 声明的 MIME 类型，由所属媒体 Block 校验主类型。
    """

    type: Literal["url"] = "url"
    url: str = Field(min_length=1)
    media_type: str = Field(min_length=1)


class Base64Source(FrozenModel):
    """以内联 Base64 传递媒体的不可变值，不负责解码或存储媒体。

    Attributes:
        type: 固定为 base64。
        data: 非空 Base64 文本；此模型仅校验非空。
        media_type: 声明的 MIME 类型。
    """

    type: Literal["base64"] = "base64"
    data: str = Field(min_length=1)
    media_type: str = Field(min_length=1)


class AssetSource(FrozenModel):
    """引用 Runtime AssetStore 中媒体的不可变定位信息。

    Attributes:
        type: 固定为 asset。
        asset_id: 资产标识；此模型不检查资产是否存在。
        media_type: 声明的 MIME 类型。
        filename: 可选原始文件名。
    """

    type: Literal["asset"] = "asset"
    asset_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    filename: str | None = None


type MediaSource = Annotated[
    UrlSource | Base64Source | AssetSource,
    Field(discriminator="type"),
]


class ImageBlock(FrozenModel):
    """保存图片输入及分辨率提示，不负责解析媒体或调用模型。

    Attributes:
        type: 固定为 image。
        source: URL、Base64 或资产引用，MIME 主类型必须为 image。
        detail: 提供给模型适配器的图片细节级别。
    """

    type: Literal["image"] = "image"
    source: MediaSource
    detail: Literal["auto", "low", "high"] = "auto"

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求图片 Block 声明 image MIME。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: 媒体 MIME 主类型不是 image。
        """
        _require_media_type(self.source.media_type, "image/")
        return self


class AudioBlock(FrozenModel):
    """保存单项音频输入，由模型适配器转换为供应商格式。

    Attributes:
        type: 固定为 audio。
        source: URL、Base64 或资产引用，MIME 主类型必须为 audio。
    """

    type: Literal["audio"] = "audio"
    source: MediaSource

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求音频 Block 声明 audio MIME。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: 媒体 MIME 主类型不是 audio。
        """
        _require_media_type(self.source.media_type, "audio/")
        return self


class VideoBlock(FrozenModel):
    """保存单项视频输入，由模型适配器转换为供应商格式。

    Attributes:
        type: 固定为 video。
        source: URL、Base64 或资产引用，MIME 主类型必须为 video。
    """

    type: Literal["video"] = "video"
    source: MediaSource

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求视频 Block 声明 video MIME。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: 媒体 MIME 主类型不是 video。
        """
        _require_media_type(self.source.media_type, "video/")
        return self


DOCUMENT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/msword",
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/csv",
        "text/plain",
    }
)


class DocumentBlock(FrozenModel):
    """保存单项文档输入，仅接受 DOCUMENT_MEDIA_TYPES 中的 MIME。

    Attributes:
        type: 固定为 document。
        source: URL、Base64 或资产引用；此模型不读取文档内容。
    """

    type: Literal["document"] = "document"
    source: MediaSource

    @model_validator(mode="after")
    def validate_media_type(self) -> Self:
        """要求文档 Block 使用明确允许的 MIME。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: 媒体 MIME 不在文档允许列表中。
        """
        if self.source.media_type.lower() not in DOCUMENT_MEDIA_TYPES:
            raise ValueError("DocumentBlock 的 MIME 类型不受支持")
        return self


def _require_media_type(media_type: str, prefix: str) -> None:
    """校验媒体 MIME 的主类型。"""
    if not media_type.lower().startswith(prefix):
        raise ValueError(f"媒体 MIME 必须以 {prefix} 开头")


class ThinkingBlock(FrozenModel):
    """保存模型思考文本及供应商返回的不透明签名。

    Attributes:
        type: 固定为 thinking。
        thinking: 原始思考文本。
        signature: 可选签名，供后续模型请求原样传递。
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str | None = None


class ToolCallBlock(FrozenModel):
    """记录一次模型工具调用，同时保留解析结果和原始参数。

    此不可变值不执行工具，也不负责工具名称或权限校验。

    Attributes:
        type: 固定为 tool_call。
        tool_call_id: 关联工具结果的调用标识。
        tool_name: 模型请求的工具名称。
        arguments: 已解析参数；解析失败或尚无结果时可为 None。
        raw_arguments: 原始参数文本，供诊断或适配器重放。
        parse_error: 可选参数解析错误描述。
        thought_signature: 可选供应商思考签名。
    """

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue] | None = None
    raw_arguments: str
    parse_error: str | None = None
    thought_signature: str | None = None


class ToolResultBlock(FrozenModel):
    """保存一次工具调用的结果，作为 TOOL Message 的唯一内容块。

    Attributes:
        type: 固定为 tool_result。
        tool_call_id: 对应调用标识。
        tool_name: 对应工具名称。
        content: 供模型消费的结果文本。
        is_error: 是否为工具错误结果。
        error_code: 可选稳定错误代码，限定为小写字母、数字及下划线。
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    content: str
    is_error: bool = False
    error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )


class InjectedItem(FrozenModel):
    """保存一个 ContextProvider 产生的不可变注入项。

    Attributes:
        provider_name: 注入内容的提供方名称。
        content: 非空注入文本。
    """

    provider_name: str = Field(min_length=1)
    content: str = Field(min_length=1)


class InjectedBlock(FrozenModel):
    """保存与目标 USER Message 关联的上下文快照。

    此内部块单独存入 USER Message，由上下文组装阶段关联目标消息。

    Attributes:
        type: 固定为 injected。
        target_user_message_id: 被注入上下文的用户消息标识。
        items: 按提供方执行结果保存的注入项，可为空。
    """

    type: Literal["injected"] = "injected"
    target_user_message_id: str = Field(min_length=1)
    items: tuple[InjectedItem, ...] = ()


class CompactionBlock(FrozenModel):
    """保存完整上下文压缩状态，而非相对于上一版的增量。

    该不可变内部块描述摘要覆盖范围和剪枝信息；不直接删除权威消息。

    Attributes:
        type: 固定为 compaction。
        action: summarized 表示生成摘要，pruned 表示剪枝。
        summary: 当前摘要文本；纯剪枝时可为空。
        compacted_message_ids: 已被当前摘要覆盖的消息标识。
        next_retained_message_id: 摘要后首条保留消息的可选标识。
        pruned_message_ids: 此快照记录的剪枝消息标识，与摘要覆盖集合互斥。
    """

    type: Literal["compaction"] = "compaction"
    action: Literal["pruned", "summarized"]
    summary: str | None = Field(default=None, min_length=1)
    compacted_message_ids: tuple[str, ...] = ()
    next_retained_message_id: str | None = None
    pruned_message_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        """确保摘要边界和剪枝集合形成一个完整快照。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: ID 重复或集合交叉，或摘要、剪枝与边界字段组合不合法。
        """
        compacted = self.compacted_message_ids
        pruned = self.pruned_message_ids
        if len(compacted) != len(set(compacted)) or len(pruned) != len(set(pruned)):
            raise ValueError("Compaction Message ID 不得重复")
        if set(compacted).intersection(pruned):
            raise ValueError("摘要覆盖 ID 与剪枝 ID 必须互斥")
        if self.action == "summarized":
            if self.summary is None or not compacted or pruned:
                raise ValueError("summarized Block 必须只包含完整摘要状态")
            return self
        if not pruned:
            raise ValueError("pruned Block 必须包含实际剪枝 Message ID")
        if self.summary is None:
            if compacted or self.next_retained_message_id is not None:
                raise ValueError("纯剪枝 Block 不能包含摘要边界")
            return self
        if not compacted:
            raise ValueError("继承摘要的 pruned Block 缺少覆盖 ID")
        return self


type UserMediaBlock = ImageBlock | AudioBlock | VideoBlock | DocumentBlock
type UserContentBlock = TextBlock | UserMediaBlock
type AssistantBlock = TextBlock | ThinkingBlock | ToolCallBlock
type ModelContentBlock = UserContentBlock | AssistantBlock | ToolResultBlock
type StoredBlock = Annotated[
    ModelContentBlock | CompactionBlock | InjectedBlock,
    Field(discriminator="type"),
]


class Message(FrozenModel):
    """按原始 Block 顺序保存的一条不可变权威会话消息。

    模型构造时校验角色、来源、可见性和内部块组合，不执行持久化。

    Attributes:
        message_id: 会话内消息标识，用于幂等提交。
        session_id: 所属会话标识，完整隔离范围由 SessionContext 提供。
        role: 模型协议角色。
        content: 非空且有序的内容块。
        origin: 内容实际生产方。
        visibility: 模型及公开投影的消费边界。
        model: 可选模型名称。
        run_id: 所属执行标识；基础 SYSTEM 消息不归属 run。
        created_at: 消息创建时间。
        stop_reason: 仅 ASSISTANT 消息可用的完成原因。
        error: 仅 ASSISTANT 消息可用的失败信息。
    """

    message_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    role: Role
    content: tuple[StoredBlock, ...] = Field(min_length=1)
    origin: str
    visibility: MessageVisibility
    model: str | None = None
    run_id: str | None = Field(default=None, min_length=1)
    created_at: datetime
    stop_reason: FinishReason | None = None
    error: RunError | None = None

    @model_validator(mode="after")
    def validate_message_contract(self) -> Self:
        """在持久化边界统一校验角色和内容关系。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: 消息的角色、来源、run、可见性或内容块组合不合法。
        """
        self._validate_role_contract()
        self._validate_internal_blocks()
        self._validate_assistant_fields()
        self._validate_visibility_contract()
        return self

    def _validate_role_contract(self) -> None:
        if self.role is Role.SYSTEM:
            if self.run_id is None and self.origin != MessageOrigin.SYSTEM:
                raise ValueError("基础 SYSTEM Message 必须使用 system origin")
            return
        if self.run_id is None:
            raise ValueError("USER、ASSISTANT 和 TOOL Message 必须包含 run_id")

    def _validate_internal_blocks(self) -> None:
        """内部块必须单独保存；普通块则按 role 检查类型及工具结果数量。"""
        internal_blocks = (CompactionBlock, InjectedBlock)
        if len(self.content) == 1 and isinstance(self.content[0], internal_blocks):
            if self.role is not Role.USER:
                raise ValueError("内部 Block Message 必须使用 USER role")
            if (
                isinstance(self.content[0], InjectedBlock)
                and self.origin != MessageOrigin.CONTEXT_PROVIDER
            ):
                raise ValueError("Injection Message 必须使用 context_provider origin")
            if (
                isinstance(self.content[0], CompactionBlock)
                and self.origin != MessageOrigin.COMPACTOR
            ):
                raise ValueError("Compaction Message 必须使用 compactor origin")
            return
        if any(isinstance(block, internal_blocks) for block in self.content):
            raise ValueError("内部 Block 不能与模型内容 Block 混合")
        allowed_blocks: dict[Role, tuple[type[FrozenModel], ...]] = {
            Role.SYSTEM: (
                TextBlock,
                ImageBlock,
                AudioBlock,
                VideoBlock,
                DocumentBlock,
            ),
            Role.USER: (TextBlock, ImageBlock, AudioBlock, VideoBlock, DocumentBlock),
            Role.ASSISTANT: (TextBlock, ThinkingBlock, ToolCallBlock),
            Role.TOOL: (ToolResultBlock,),
        }
        if not all(
            isinstance(block, allowed_blocks[self.role]) for block in self.content
        ):
            raise ValueError("Message role 不允许当前 Block 类型")
        if self.role is Role.TOOL and len(self.content) != 1:
            raise ValueError("TOOL Message 必须只包含一个 ToolResultBlock")

    def _validate_assistant_fields(self) -> None:
        if self.role is not Role.ASSISTANT and (
            self.stop_reason is not None or self.error is not None
        ):
            raise ValueError("stop_reason 和 error 只能用于 ASSISTANT Message")

    def _validate_visibility_contract(self) -> None:
        expected = _required_visibility(self)
        if expected is not None and self.visibility is not expected:
            raise ValueError("Message visibility 与结构语义不匹配")
        if self.role is Role.USER and self.visibility is MessageVisibility.PUBLIC:
            raise ValueError("USER Message 不能只对公开投影可见")


def _required_visibility(message: Message) -> MessageVisibility | None:
    """按内部块和角色推导固定可见性；普通 USER 留给调用方选择。"""
    block = message.content[0]
    if isinstance(block, InjectedBlock):
        return MessageVisibility.MODEL
    if isinstance(block, CompactionBlock):
        return MessageVisibility.ALL
    if message.role is Role.SYSTEM:
        return (
            MessageVisibility.MODEL if message.run_id is None else MessageVisibility.ALL
        )
    if message.role is Role.TOOL:
        return MessageVisibility.ALL
    if message.role is Role.ASSISTANT:
        return (
            MessageVisibility.PUBLIC
            if message.error is not None
            else MessageVisibility.ALL
        )
    return None


def is_model_visible(message: Message) -> bool:
    """判断 Message 是否进入模型上下文。

    Args:
        message: 待判断消费边界的消息。

    Returns:
        visibility 为 MODEL 或 ALL 时返回 True。
    """
    return message.visibility in {MessageVisibility.MODEL, MessageVisibility.ALL}


def is_public_visible(message: Message) -> bool:
    """判断 Message 是否进入公开投影。

    Args:
        message: 待判断消费边界的消息。

    Returns:
        visibility 为 PUBLIC 或 ALL 时返回 True。
    """
    return message.visibility in {MessageVisibility.PUBLIC, MessageVisibility.ALL}


def is_inbound_message(message: Message) -> bool:
    """判断消息是否为已领取并归入 run 的 Inbox 输入。

    Args:
        message: 待判断来源结构的消息。

    Returns:
        角色为 USER/SYSTEM、归属 run、对全部可见且非内部块时为 True。
    """
    return (
        message.role in {Role.USER, Role.SYSTEM}
        and message.run_id is not None
        and message.visibility is MessageVisibility.ALL
        and not isinstance(message.content[0], (CompactionBlock, InjectedBlock))
    )


class ContextMessage(FrozenModel):
    """封装 MessageManager 一次一致性读取的不可变上下文源。

    分区保留权威消息，不等同于发送给模型的最终请求，也不访问存储。

    Attributes:
        system_message: 基础 SYSTEM 消息。
        latest_compaction_message: 最新完整压缩快照，没有压缩时为 None。
        retained_messages: 按存储顺序保留的模型可见消息。
        injection_messages: 与保留用户消息关联的内部注入消息。
    """

    system_message: Message
    latest_compaction_message: Message | None = None
    retained_messages: tuple[Message, ...] = ()
    injection_messages: tuple[Message, ...] = ()

    @model_validator(mode="after")
    def validate_context_roles(self) -> Self:
        """确保各分区只包含其协议允许的 Message 类型。

        Returns:
            通过校验的当前不可变模型实例。

        Raises:
            ValueError: system_message 非 SYSTEM，或压缩及注入分区包含错误块类型。
        """
        if self.system_message.role is not Role.SYSTEM:
            raise ValueError("system_message 必须是 SYSTEM Message")
        if self.latest_compaction_message is not None:
            self._require_single_block(
                self.latest_compaction_message,
                CompactionBlock,
                "latest_compaction_message",
            )
        for message in self.injection_messages:
            self._require_single_block(message, InjectedBlock, "injection_messages")
        return self

    @staticmethod
    def _require_single_block(
        message: Message,
        block_type: type[CompactionBlock] | type[InjectedBlock],
        field_name: str,
    ) -> None:
        if len(message.content) != 1 or not isinstance(message.content[0], block_type):
            raise ValueError(f"{field_name} 包含了不匹配的 Message")
