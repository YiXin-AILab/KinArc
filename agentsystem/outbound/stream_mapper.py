"""将内部 Agent event 映射为稳定、脱敏的浏览器公开事件。"""

from typing import cast
from uuid import UUID, uuid5

from agentsystem.config.models import PublicStreamConfig
from agentsystem.message.models import (
    AssetSource,
    BlockType,
    Role,
    TextBlock,
    ThinkingBlock,
    UserContentBlock,
)
from agentsystem.outbound.public_content import PublicContentPolicy
from agentsystem.outbound.stream_models import (
    BlockCompletedPayload,
    BlockDeltaPayload,
    BlockStartedPayload,
    ContextCompactionCompletedPayload,
    ContextCompactionStartedPayload,
    MessageStartedPayload,
    ModelCompletedPayload,
    ModelStartedPayload,
    PublicEventPayload,
    PublicEventType,
    PublicStreamEvent,
    RunResumedPayload,
    RunStartedPayload,
    RunSuspendedPayload,
    RunTerminalPayload,
    StartedMediaBlock,
    StartedPublicMessage,
    StartedTextBlock,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolStartedPayload,
    TurnStartedPayload,
)
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventPayload,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    LLMCallCompleted,
    LLMCallStarted,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    MessageStarted,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunResumed,
    RunStarted,
    RunSuspended,
    ToolExecutionCancelled,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    TurnStarted,
)

_PUBLIC_EVENT_NAMESPACE = UUID("db6471c8-9b76-5aaa-9760-45b59bc64291")


def to_public_stream_event(
    event: AgentEventEnvelope,
    config: PublicStreamConfig,
) -> PublicStreamEvent | None:
    """执行公开事件白名单、字段裁剪和工具参数敏感键脱敏。

    不修改内部事件；相同内部 event_id 映射到相同公开 ID。文本及工具结果
    不进行通用敏感内容识别，内容开关与工具载荷上限由配置决定。

    Args:
        event: 待映射的内部事件。
        config: 思考、工具内容及载荷大小策略。

    Returns:
        版本化公开事件；不在白名单或被配置隐藏时返回 None。
    """
    mapped = _map_payload(event, config)
    if mapped is None:
        return None
    event_type, payload = mapped
    public_event_id = uuid5(_PUBLIC_EVENT_NAMESPACE, event.event_id)
    return PublicStreamEvent(
        event_id=str(public_event_id),
        type=event_type,
        created_at=event.created_at,
        session_id=event.session_id,
        payload=payload,
    )


def _map_payload(
    event: AgentEventEnvelope,
    config: PublicStreamConfig,
) -> tuple[PublicEventType, PublicEventPayload] | None:
    payload = event.payload
    if isinstance(payload, MessageStarted):
        return _map_message_started(payload)
    if isinstance(payload, ContextCompactionStarted):
        return "context.compaction.started", ContextCompactionStartedPayload()
    if isinstance(payload, ContextCompactionCompleted):
        return "context.compaction.completed", ContextCompactionCompletedPayload(
            message_id=payload.compaction_message.message_id,
            action=payload.action,
            before_tokens=payload.before_tokens,
            after_tokens=payload.after_tokens,
            usage=payload.usage,
        )
    if isinstance(payload, (RunStarted, TurnStarted, LLMCallStarted, LLMCallCompleted)):
        return _map_lifecycle_payload(payload)
    if isinstance(
        payload,
        (MessageBlockStarted, MessageBlockDelta, MessageBlockCompleted),
    ):
        return _map_message_payload(payload, config)
    if isinstance(
        payload,
        (ToolExecutionStarted, ToolExecutionCompleted, ToolExecutionCancelled),
    ):
        return _map_tool_payload(payload, config)
    if isinstance(
        payload,
        (RunSuspended, RunResumed, RunCompleted, RunFailed, RunCanceled),
    ):
        return _map_run_payload(payload)
    return None


def _map_message_started(
    payload: MessageStarted,
) -> tuple[PublicEventType, PublicEventPayload] | None:
    role = payload.message.role
    if role not in {Role.USER, Role.SYSTEM} or payload.message.run_id is None:
        return None
    content = cast(tuple[UserContentBlock, ...], payload.message.content)
    blocks = tuple(_map_started_block(block) for block in content)
    text_blocks = tuple(
        block for block in blocks if isinstance(block, StartedTextBlock)
    )
    return "message.started", MessageStartedPayload(
        mode=payload.mode.value,
        message=StartedPublicMessage(
            message_id=payload.message.message_id,
            role=role.value,
            origin=payload.message.origin,
            content="\n".join(block.text for block in text_blocks),
            blocks=blocks,
            created_at=payload.message.created_at,
        ),
    )


def _map_started_block(block: UserContentBlock) -> StartedTextBlock | StartedMediaBlock:
    if isinstance(block, TextBlock):
        return StartedTextBlock(text=block.text)
    source = block.source
    return StartedMediaBlock(
        type=block.type,
        source_type=source.type,
        media_type=source.media_type,
        filename=source.filename if isinstance(source, AssetSource) else None,
        asset_id=source.asset_id if isinstance(source, AssetSource) else None,
    )


def _map_lifecycle_payload(
    payload: AgentEventPayload,
) -> tuple[PublicEventType, PublicEventPayload]:
    if isinstance(payload, RunStarted):
        return "run.started", RunStartedPayload()
    if isinstance(payload, TurnStarted):
        return "turn.started", TurnStartedPayload(turn_index=payload.turn_index)
    if isinstance(payload, LLMCallStarted):
        return "model.started", ModelStartedPayload(
            provider_name=payload.provider_name,
            model=payload.model,
        )
    if isinstance(payload, LLMCallCompleted):
        return "model.completed", ModelCompletedPayload(
            model=payload.response.model,
            finish_reason=payload.response.finish_reason,
        )
    raise TypeError(f"不支持的 lifecycle payload: {type(payload).__name__}")


def _map_message_payload(
    payload: AgentEventPayload,
    config: PublicStreamConfig,
) -> tuple[PublicEventType, PublicEventPayload] | None:
    if isinstance(payload, MessageBlockStarted):
        return _map_block_started(payload, config)
    if isinstance(payload, MessageBlockDelta):
        return _map_block_delta(payload, config)
    if isinstance(payload, MessageBlockCompleted):
        return _map_block_completed(payload, config)
    raise TypeError(f"不支持的 message payload: {type(payload).__name__}")


def _map_tool_payload(
    payload: AgentEventPayload,
    config: PublicStreamConfig,
) -> tuple[PublicEventType, PublicEventPayload]:
    if isinstance(payload, ToolExecutionStarted):
        return "tool.started", _map_tool_started(payload, config)
    if isinstance(payload, ToolExecutionCompleted):
        return "tool.completed", _map_tool_completed(payload, config)
    if isinstance(payload, ToolExecutionCancelled):
        return "tool.failed", ToolFailedPayload(
            tool_call_id=payload.tool_call_id,
            tool_name=payload.tool_name,
            reason_code=payload.reason_code,
        )
    raise TypeError(f"不支持的 tool payload: {type(payload).__name__}")


def _map_run_payload(
    payload: AgentEventPayload,
) -> tuple[PublicEventType, PublicEventPayload]:
    if isinstance(payload, RunSuspended):
        return "run.suspended", RunSuspendedPayload(
            resume_token=payload.resume_token,
            expires_at=payload.expires_at,
        )
    if isinstance(payload, RunResumed):
        return "run.resumed", RunResumedPayload()
    if isinstance(payload, RunCompleted):
        return "run.completed", RunTerminalPayload(
            status="succeeded",
            final_message_id=payload.final_message.message_id,
            usage=payload.usage,
            timing=payload.timing,
        )
    if isinstance(payload, (RunFailed, RunCanceled)):
        event_type: PublicEventType = (
            "run.failed" if isinstance(payload, RunFailed) else "run.canceled"
        )
        status = "failed" if isinstance(payload, RunFailed) else "canceled"
        return event_type, RunTerminalPayload(
            status=status,
            error=payload.error,
            usage=payload.usage,
            timing=payload.timing,
        )
    raise TypeError(f"不支持的 run payload: {type(payload).__name__}")


def _map_block_started(
    payload: MessageBlockStarted,
    config: PublicStreamConfig,
) -> tuple[PublicEventType, PublicEventPayload] | None:
    if _is_hidden_block(payload.block_type, config):
        return None
    return "message.block.started", BlockStartedPayload(
        message_id=payload.message_id,
        block_id=payload.block_id,
        index=payload.index,
        block_type=payload.block_type,
        tool_name=payload.tool_name,
        tool_call_id=payload.tool_call_id,
    )


def _map_block_delta(
    payload: MessageBlockDelta,
    config: PublicStreamConfig,
) -> tuple[PublicEventType, PublicEventPayload] | None:
    if _is_hidden_block(payload.block_type, config):
        return None
    return "message.block.delta", BlockDeltaPayload(
        message_id=payload.message_id,
        block_id=payload.block_id,
        index=payload.index,
        block_type=payload.block_type,
        delta=payload.delta,
    )


def _map_block_completed(
    payload: MessageBlockCompleted,
    config: PublicStreamConfig,
) -> tuple[PublicEventType, PublicEventPayload] | None:
    """只公开配置允许的块内容，工具参数复用统一脱敏及截断策略。"""
    block = payload.block
    if isinstance(block, ThinkingBlock) and not config.include_thinking:
        return None
    if isinstance(block, TextBlock):
        content = block.text
        arguments = None
        tool_name = None
        tool_call_id = None
    elif isinstance(block, ThinkingBlock):
        content = block.thinking
        arguments = None
        tool_name = None
        tool_call_id = None
    else:
        content = None
        arguments = None
        if config.include_tool_arguments and block.arguments is not None:
            arguments = PublicContentPolicy(config).tool_arguments(block.arguments)[0]
        tool_name = block.tool_name
        tool_call_id = block.tool_call_id
    return "message.block.completed", BlockCompletedPayload(
        message_id=payload.message_id,
        block_id=payload.block_id,
        index=payload.index,
        block_type=BlockType(block.type),
        content=content,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=arguments,
    )


def _map_tool_started(
    payload: ToolExecutionStarted,
    config: PublicStreamConfig,
) -> ToolStartedPayload:
    arguments, raw_arguments, truncated = PublicContentPolicy(config).tool_arguments(
        payload.arguments
    )
    return ToolStartedPayload(
        tool_call_id=payload.tool_call_id,
        tool_name=payload.tool_name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        truncated=truncated,
    )


def _map_tool_completed(
    payload: ToolExecutionCompleted,
    config: PublicStreamConfig,
) -> ToolCompletedPayload:
    content, truncated = PublicContentPolicy(config).tool_result(payload.result.content)
    return ToolCompletedPayload(
        tool_call_id=payload.tool_call_id,
        tool_name=payload.tool_name,
        content=content,
        is_error=payload.result.is_error,
        error_code=payload.result.error_code,
        truncated=truncated,
        duration_ms=payload.timing.duration_ms,
    )


def _is_hidden_block(block_type: BlockType, config: PublicStreamConfig) -> bool:
    if block_type is BlockType.THINKING:
        return not config.include_thinking
    if block_type is BlockType.TOOL_CALL:
        return not config.include_tool_arguments
    return False
