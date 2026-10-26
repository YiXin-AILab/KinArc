"""Message 稳定序列到 ContextMessage 的纯组装测试。"""

from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError, RunError
from agentsystem.message.models import (
    CompactionBlock,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.persistence import assemble_context_message

NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _message(
    message_id: str,
    role: Role,
    origin: str,
    text: str,
    offset: int,
) -> Message:
    return Message(
        message_id=message_id,
        session_id="session",
        role=role,
        origin=origin,
        run_id=None if role is Role.SYSTEM else "run-1",
        created_at=NOW + timedelta(seconds=offset),
        content=(TextBlock(text=text),),
        visibility=(
            MessageVisibility.MODEL if role is Role.SYSTEM else MessageVisibility.ALL
        ),
    )


def test_assembly_selects_latest_compaction_and_live_injection() -> None:
    """组装只保留未压缩尾部及其有效 Injection。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    old_user = _message("old", Role.USER, MessageOrigin.USER, "old", 1)
    live_user = _message("live", Role.USER, MessageOrigin.USER, "live", 2)
    compaction = Message(
        message_id="compaction",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=3),
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=("old",),
                next_retained_message_id="live",
            ),
        ),
        visibility=MessageVisibility.ALL,
    )
    old_injection = Message(
        message_id="injection-old",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.CONTEXT_PROVIDER,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=4),
        content=(InjectedBlock(target_user_message_id="old"),),
        visibility=MessageVisibility.MODEL,
    )
    live_injection = Message(
        message_id="injection-live",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.CONTEXT_PROVIDER,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=5),
        content=(InjectedBlock(target_user_message_id="live"),),
        visibility=MessageVisibility.MODEL,
    )

    context = assemble_context_message(
        (
            system,
            old_user,
            live_user,
            compaction,
            old_injection,
            live_injection,
        )
    )

    assert context.system_message == system
    assert context.latest_compaction_message == compaction
    assert context.retained_messages == (live_user,)
    assert context.injection_messages == (live_injection,)


def test_assembly_keeps_dynamic_system_in_retained_order() -> None:
    """带 run_id 的 SYSTEM 是公开输入，不得被当作基础 Prompt 过滤。"""
    base = _message("base", Role.SYSTEM, "system", "base", 0)
    dynamic = Message(
        message_id="dynamic",
        session_id="session",
        role=Role.SYSTEM,
        origin="service",
        run_id="run-1",
        created_at=NOW + timedelta(seconds=1),
        content=(TextBlock(text="notice"),),
        visibility=MessageVisibility.ALL,
    )
    user = _message("user", Role.USER, "human", "hello", 2)

    context = assemble_context_message((base, dynamic, user))

    assert context.system_message == base
    assert context.retained_messages == (dynamic, user)


def test_assembly_excludes_public_only_error_from_model_context() -> None:
    """公开错误消息不得回流到后续模型上下文。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", 1)
    failed = Message(
        message_id="failed",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="failed"),),
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.PUBLIC,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=2),
        error=RunError(code="failed", message="failed"),
    )

    context = assemble_context_message((system, user, failed))

    assert context.retained_messages == (user,)


@pytest.mark.parametrize(
    "systems",
    [
        (),
        (
            _message("system-1", Role.SYSTEM, MessageOrigin.SYSTEM, "one", 0),
            _message("system-2", Role.SYSTEM, MessageOrigin.SYSTEM, "two", 1),
        ),
    ],
)
def test_assembly_requires_exactly_one_base_system(
    systems: tuple[Message, ...],
) -> None:
    """缺失或重复基础 SYSTEM 都不能形成合法上下文。"""
    with pytest.raises(AdapterError) as captured:
        assemble_context_message(systems)
    assert captured.value.code == "message_context_invalid"
