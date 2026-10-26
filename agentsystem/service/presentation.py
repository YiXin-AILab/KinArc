"""把内部消息和状态投影为稳定、脱敏的 Service View。"""

from agentsystem.inbox.models import InboundMessage
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    CompactionBlock,
    DocumentBlock,
    ImageBlock,
    Message,
    MessageVisibility,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserContentBlock,
    VideoBlock,
    is_public_visible,
)
from agentsystem.outbound.public_content import PublicContentPolicy
from agentsystem.runtime.state import AgentState, SessionStatus
from agentsystem.service.models import (
    PendingInputView,
    SessionAudioBlockView,
    SessionCompactionBlockView,
    SessionContentBlockView,
    SessionDocumentBlockView,
    SessionImageBlockView,
    SessionMessageView,
    SessionPublicResult,
    SessionRunTiming,
    SessionTextBlockView,
    SessionThinkingBlockView,
    SessionTokenUsage,
    SessionToolCallBlockView,
    SessionToolResultBlockView,
    SessionUserContentBlockView,
    SessionVideoBlockView,
)


def present_pending_input(message: InboundMessage) -> PendingInputView:
    """把 Inbox 消息映射为不含媒体正文和 locator 的公开视图。

    Args:
        message: 已被 Inbox 接收且带接收时间的输入。

    Returns:
        保留内容块顺序、拼接普通文本的待处理输入视图。

    Raises:
        RuntimeError: 输入缺少接收时间。
    """
    text = "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
    if message.created_at is None:
        raise RuntimeError("公开 Inbox 消息缺少接收时间")
    return PendingInputView(
        message_id=message.message_id,
        role=message.role,
        origin=message.origin,
        content=text,
        blocks=tuple(_present_user_block(block) for block in message.content),
        mode=message.mode,
        created_at=message.created_at,
    )


def present_message(
    message: Message,
    policy: PublicContentPolicy,
) -> SessionMessageView | None:
    """按 visibility 选择消息并将可公开 Block 脱敏。

    Args:
        message: 权威存储中的定稿消息。
        policy: Thinking 可见性、工具参数及结果截断策略。

    Returns:
        公开消息视图；不可见、角色不支持或无可公开块时为 None。
        压缩消息只保留占位块，不公开内部摘要快照。
    """
    if not is_public_visible(message):
        return None
    if (
        message.role is Role.USER
        and message.visibility is MessageVisibility.ALL
        and len(message.content) == 1
        and isinstance(message.content[0], CompactionBlock)
    ):
        return _present_compaction_message(message)
    if message.role not in {
        Role.USER,
        Role.SYSTEM,
        Role.ASSISTANT,
        Role.TOOL,
    }:
        return None
    blocks = tuple(
        public
        for block in message.content
        if (public := _present_message_block(block, policy)) is not None
    )
    if not blocks:
        return None
    content = "\n".join(
        block.text for block in blocks if isinstance(block, SessionTextBlockView)
    )
    tool_results = tuple(
        block for block in blocks if isinstance(block, SessionToolResultBlockView)
    )
    if tool_results:
        content = "\n".join(
            block.content for block in tool_results if block.content is not None
        )
    return SessionMessageView(
        message_id=message.message_id,
        role=message.role.value,
        origin=message.origin,
        content=content,
        blocks=blocks,
        created_at=message.created_at,
        is_error=any(block.is_error for block in tool_results),
    )


def _present_compaction_message(message: Message) -> SessionMessageView | None:
    """只公开 Compaction 的顺序占位，不泄漏内部快照。"""
    if (
        message.role is not Role.USER
        or message.visibility is not MessageVisibility.ALL
        or len(message.content) != 1
        or not isinstance(message.content[0], CompactionBlock)
    ):
        return None
    return SessionMessageView(
        message_id=message.message_id,
        role=message.role.value,
        origin=message.origin,
        content="",
        blocks=(SessionCompactionBlockView(),),
        created_at=message.created_at,
    )


def present_result(
    state: AgentState,
    final_message: Message | None,
) -> SessionPublicResult | None:
    """根据状态构造不含内部 run/trace 标识的公开结果。

    Args:
        state: 最新运行快照。
        final_message: 成功终态引用的最终消息，无消息时为 None。

    Returns:
        挂起凭证、错误信息或最终普通文本；没有对应公开内容时为 None。
    """
    if state.status is SessionStatus.PENDING:
        return SessionPublicResult(
            resume_token=state.resume_token,
            suspend_expires_at=state.suspend_expires_at,
        )
    if state.status in {SessionStatus.FAILED, SessionStatus.CANCELED}:
        if state.error is None:
            return None
        return SessionPublicResult(
            error_code=state.error.code,
            error_message=state.error.message,
        )
    if state.status is not SessionStatus.SUCCEEDED or final_message is None:
        return None
    content = "\n".join(
        block.text for block in final_message.content if isinstance(block, TextBlock)
    )
    return SessionPublicResult(content=content) if content else None


def present_usage(state: AgentState) -> SessionTokenUsage | None:
    """只在终态公开累计 token 使用量。

    Args:
        state: 待展示的运行快照。

    Returns:
        成功、失败或取消终态的用量视图；其他状态返回 None。
    """
    if state.status not in {
        SessionStatus.SUCCEEDED,
        SessionStatus.FAILED,
        SessionStatus.CANCELED,
    }:
        return None
    return SessionTokenUsage(**state.usage_total.model_dump())


def present_timing(state: AgentState) -> SessionRunTiming | None:
    """只在具有完整时间边界时公开运行耗时。

    Args:
        state: 待展示的运行快照。

    Returns:
        墙钟与 active 双口径计时；缺少任一起止时间时返回 None。
    """
    if state.ended_at is None or state.started_at is None:
        return None
    elapsed_ms = (state.ended_at - state.started_at).total_seconds() * 1_000
    return SessionRunTiming(
        started_at=state.started_at,
        ended_at=state.ended_at,
        elapsed_ms=elapsed_ms,
        active_ms=state.active_duration_ms,
    )


def _present_message_block(
    block: object,
    policy: PublicContentPolicy,
) -> SessionContentBlockView | None:
    if isinstance(block, TextBlock):
        return SessionTextBlockView(text=block.text)
    if isinstance(block, (ImageBlock, AudioBlock, VideoBlock, DocumentBlock)):
        return _present_media_block(block)
    if isinstance(block, ThinkingBlock):
        if not policy.config.include_thinking:
            return None
        return SessionThinkingBlockView(thinking=block.thinking)
    if isinstance(block, ToolCallBlock):
        arguments, raw_arguments, truncated = policy.tool_arguments(block.arguments)
        return SessionToolCallBlockView(
            tool_call_id=block.tool_call_id,
            tool_name=block.tool_name,
            arguments=arguments,
            raw_arguments=raw_arguments,
            truncated=truncated,
        )
    if isinstance(block, ToolResultBlock):
        content, truncated = policy.tool_result(block.content)
        return SessionToolResultBlockView(
            tool_call_id=block.tool_call_id,
            tool_name=block.tool_name,
            content=content,
            is_error=block.is_error,
            truncated=truncated,
        )
    return None


def _present_user_block(block: UserContentBlock) -> SessionUserContentBlockView:
    if isinstance(block, TextBlock):
        return SessionTextBlockView(text=block.text)
    return _present_media_block(block)


def _present_media_block(
    block: ImageBlock | AudioBlock | VideoBlock | DocumentBlock,
) -> (
    SessionImageBlockView
    | SessionAudioBlockView
    | SessionVideoBlockView
    | SessionDocumentBlockView
):
    source = block.source
    filename = source.filename if isinstance(source, AssetSource) else None
    asset_id = source.asset_id if isinstance(source, AssetSource) else None
    values = dict(
        source_type=source.type,
        media_type=source.media_type,
        filename=filename,
        asset_id=asset_id,
    )
    if isinstance(block, ImageBlock):
        return SessionImageBlockView(**values)
    if isinstance(block, AudioBlock):
        return SessionAudioBlockView(**values)
    if isinstance(block, VideoBlock):
        return SessionVideoBlockView(**values)
    return SessionDocumentBlockView(**values)
