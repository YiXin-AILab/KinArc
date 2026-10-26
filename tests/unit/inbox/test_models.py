"""Inbox、Ready 和 Resume 领域模型测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.inbox.models import (
    InboundMessage,
    InboundRole,
    NormalReady,
    ReadyEntry,
    ReadyKind,
    ResumeReady,
)
from agentsystem.message.models import (
    Base64Source,
    ImageBlock,
    MessageOrigin,
    TextBlock,
    ToolCallBlock,
)
from pydantic import TypeAdapter, ValidationError

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def test_inbound_message_requires_user_content_and_is_immutable() -> None:
    """普通 Inbox 消息必须包含用户可提交的模型内容。"""
    with pytest.raises(ValidationError):
        InboundMessage(
            message_id="message-1",
            session_id="session",
            content=(),
            origin=MessageOrigin.USER,
            created_at=NOW,
        )
    message = InboundMessage(
        message_id="message-1",
        session_id="session",
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        created_at=NOW,
    )
    field_name = "session_id"
    with pytest.raises(ValidationError):
        setattr(message, field_name, "changed")


def test_inbound_message_defaults_to_user_and_preserves_custom_origin() -> None:
    """Inbox 默认使用 USER role，并把 origin 作为不透明字符串保存。"""
    message = InboundMessage(
        message_id="message-1",
        session_id="session",
        origin="人工坐席 / tier-2",
        content=(TextBlock(text="hello"),),
    )

    assert message.role is InboundRole.USER
    assert message.origin == "人工坐席 / tier-2"


def test_inbound_system_accepts_user_content_blocks() -> None:
    """SYSTEM Inbox 输入与 USER 共享媒体内容能力。"""
    message = InboundMessage(
        message_id="system-1",
        session_id="session",
        role=InboundRole.SYSTEM,
        origin="workflow.refund",
        content=(ImageBlock(source=Base64Source(data="eA==", media_type="image/png")),),
    )

    assert message.role is InboundRole.SYSTEM


def test_inbound_rejects_assistant_role() -> None:
    """公开 Inbox 不接受 ASSISTANT role。"""
    with pytest.raises(ValidationError):
        InboundMessage.model_validate(
            {
                "message_id": "assistant-1",
                "session_id": "session",
                "role": "assistant",
                "origin": "caller",
                "content": [{"type": "text", "text": "forged"}],
            }
        )


def test_inbound_message_round_trips_media_source() -> None:
    """Inbox JSON 往返必须保留媒体 Source 判别信息。"""
    message = InboundMessage(
        message_id="message-1",
        session_id="session",
        content=(
            TextBlock(text="describe"),
            ImageBlock(source=Base64Source(data="aGVsbG8=", media_type="image/png")),
        ),
        origin=MessageOrigin.USER,
        created_at=NOW,
    )

    parsed = InboundMessage.model_validate_json(message.model_dump_json())

    assert parsed == message


def test_inbound_message_rejects_assistant_tool_call() -> None:
    """Inbox 只允许用户可提交的内容块。"""
    with pytest.raises(ValidationError):
        InboundMessage(
            message_id="message-1",
            session_id="session",
            content=(
                ToolCallBlock(
                    tool_call_id="call",
                    tool_name="search",
                    arguments={},
                    raw_arguments="{}",
                ),
            ),
            origin=MessageOrigin.USER,
            created_at=NOW,
        )


def test_normal_ready_contains_only_session_location() -> None:
    """NormalReady 不得携带 run 或业务 input。"""
    with pytest.raises(ValidationError):
        NormalReady.model_validate(
            {
                "app_id": "app",
                "user_id": "user",
                "agent_id": "agent",
                "session_id": "session",
                "run_id": "forbidden",
            }
        )

    ready = NormalReady(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="session",
    )
    assert ready.kind is ReadyKind.NORMAL


def test_resume_ready_requires_control_identity_and_parses_by_kind() -> None:
    """ResumeReady 必须完整携带同一 run 的稳定控制凭证。"""
    with pytest.raises(ValidationError):
        ResumeReady(
            resume_id="resume-1",
            app_id="app",
            user_id="user",
            agent_id="agent",
            session_id="session",
            run_id="run-1",
            resume_token="",
        )

    payload = {
        "kind": "resume",
        "resume_id": "resume-1",
        "app_id": "app",
        "user_id": "user",
        "agent_id": "agent",
        "session_id": "session",
        "run_id": "run-1",
        "resume_token": "single-use-token",
        "input": {"approved": True},
    }
    ready: ReadyEntry = TypeAdapter(ReadyEntry).validate_python(payload)

    assert isinstance(ready, ResumeReady)
    assert ready.input == {"approved": True}
