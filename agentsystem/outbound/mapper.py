"""内部终态事件到脱敏 PublicRunResult 的纯映射。"""

from agentsystem.common.errors import RunError
from agentsystem.message.models import Message, TextBlock
from agentsystem.outbound.models import PublicRunResult
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunSuspended,
)
from agentsystem.runtime.state import SessionStatus


def to_public_run_result(event: AgentEventEnvelope) -> PublicRunResult | None:
    """只映射 PENDING 和三个 run 终态，过程事件返回 None。

    最终消息只保留文本块；错误对象深拷贝，不额外重写其字段。

    Args:
        event: 待公开的内部事件封装。

    Returns:
        挂起或终态结果；不支持的事件返回 None。
    """
    payload = event.payload
    common = {
        "event_id": event.event_id,
        "created_at": event.created_at,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "trace_id": event.trace_id,
    }
    if isinstance(payload, RunSuspended):
        return PublicRunResult(
            **common,
            status=SessionStatus.PENDING,
            resume_token=payload.resume_token,
            suspend_expires_at=payload.expires_at,
        )
    if isinstance(payload, RunCompleted):
        return PublicRunResult(
            **common,
            status=SessionStatus.SUCCEEDED,
            message=_public_message(payload.final_message),
        )
    if isinstance(payload, RunFailed):
        return PublicRunResult(
            **common,
            status=SessionStatus.FAILED,
            error=_public_error(payload.error),
        )
    if isinstance(payload, RunCanceled):
        return PublicRunResult(
            **common,
            status=SessionStatus.CANCELED,
            error=_public_error(payload.error),
        )
    return None


def _public_message(message: Message) -> Message | None:
    content = tuple(block for block in message.content if isinstance(block, TextBlock))
    if not content:
        return None
    return message.model_copy(update={"content": content}, deep=True)


def _public_error(error: RunError) -> RunError:
    return error.model_copy(deep=True)
