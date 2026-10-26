"""全部 MessageManager 权威实现共享的可观察行为合约。"""

from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import (
    CompactionBlock,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)

CONTRACT_NOW = datetime(2026, 8, 3, tzinfo=UTC)


def contract_message(
    ctx: SessionContext,
    message_id: str,
    role: Role,
    origin: str,
    text: str,
    offset: int,
    *,
    visibility: MessageVisibility,
) -> Message:
    """构造所有后端共享的普通 Message fixture。"""
    return Message(
        message_id=message_id,
        session_id=ctx.session_id,
        role=role,
        origin=origin,
        run_id=None if role is Role.SYSTEM else "run-1",
        created_at=CONTRACT_NOW + timedelta(seconds=offset),
        content=(TextBlock(text=text),),
        visibility=visibility,
    )


class BasicMessageManagerContract:
    """MessageManager 基础写入、冲突、可见性和排序合约。"""

    async def test_batch_commit_is_idempotent_atomic_and_conflict_checked(
        self,
        message_manager: MessageManager,
        message_context: SessionContext,
    ) -> None:
        """相同内容幂等，冲突批次不得留下部分新消息。"""
        system = contract_message(
            message_context,
            "system",
            Role.SYSTEM,
            MessageOrigin.SYSTEM,
            "system",
            0,
            visibility=MessageVisibility.MODEL,
        )
        user = contract_message(
            message_context,
            "user-1",
            Role.USER,
            MessageOrigin.USER,
            "hello",
            1,
            visibility=MessageVisibility.ALL,
        )
        changed = user.model_copy(update={"content": (TextBlock(text="changed"),)})
        new_user = contract_message(
            message_context,
            "user-2",
            Role.USER,
            MessageOrigin.USER,
            "new",
            2,
            visibility=MessageVisibility.ALL,
        )

        await message_manager.commit(message_context, (system, user))
        await message_manager.commit(message_context, (system, user))
        with pytest.raises(AdapterError) as captured:
            await message_manager.commit(message_context, (new_user, changed))
        assert captured.value.code == "message_conflict"

        assert await message_manager.get(message_context, "user-2") is None
        assert await message_manager.list_messages(message_context) == (system, user)

    async def test_listing_returns_all_messages_in_stable_order(
        self,
        message_manager: MessageManager,
        message_context: SessionContext,
    ) -> None:
        """权威查询不承担公开过滤，并保持提交顺序。"""
        system = contract_message(
            message_context,
            "system",
            Role.SYSTEM,
            MessageOrigin.SYSTEM,
            "system",
            0,
            visibility=MessageVisibility.MODEL,
        )
        user = contract_message(
            message_context,
            "user-1",
            Role.USER,
            MessageOrigin.USER,
            "hello",
            1,
            visibility=MessageVisibility.ALL,
        )
        assistant = contract_message(
            message_context,
            "assistant-1",
            Role.ASSISTANT,
            MessageOrigin.MODEL,
            "answer",
            2,
            visibility=MessageVisibility.ALL,
        )
        dynamic_system = Message(
            message_id="dynamic-system",
            session_id=message_context.session_id,
            role=Role.SYSTEM,
            origin="service",
            run_id="run-1",
            created_at=CONTRACT_NOW + timedelta(seconds=3),
            content=(TextBlock(text="business notice"),),
            visibility=MessageVisibility.ALL,
        )

        await message_manager.commit(
            message_context,
            (system, user, assistant, dynamic_system),
        )

        assert await message_manager.list_messages(message_context) == (
            system,
            user,
            assistant,
            dynamic_system,
        )


class CompactionMessageManagerContract:
    """MessageManager 上下文快照和 Compaction CAS 合约。"""

    async def test_context_source_selects_latest_compaction_and_live_injection(
        self,
        message_manager: MessageManager,
        message_context: SessionContext,
    ) -> None:
        """上下文快照排除已压缩前缀和指向该前缀的 Injection。"""
        system = contract_message(
            message_context,
            "system",
            Role.SYSTEM,
            MessageOrigin.SYSTEM,
            "system",
            0,
            visibility=MessageVisibility.MODEL,
        )
        old_user = contract_message(
            message_context,
            "user-old",
            Role.USER,
            MessageOrigin.USER,
            "old",
            1,
            visibility=MessageVisibility.ALL,
        )
        live_user = contract_message(
            message_context,
            "user-live",
            Role.USER,
            MessageOrigin.USER,
            "live",
            2,
            visibility=MessageVisibility.ALL,
        )
        compaction = _compaction(
            message_context,
            "compaction-1",
            ("user-old",),
            "user-live",
            3,
        )
        old_injection = _injection(
            message_context,
            "injection-old",
            "user-old",
            4,
        )
        live_injection = _injection(
            message_context,
            "injection-live",
            "user-live",
            5,
        )

        await message_manager.commit(
            message_context,
            (
                system,
                old_user,
                live_user,
                compaction,
                old_injection,
                live_injection,
            ),
        )

        context = await message_manager.read_context_source(message_context)

        assert context.system_message == system
        assert context.latest_compaction_message == compaction
        assert context.retained_messages == (live_user,)
        assert context.injection_messages == (live_injection,)

    async def test_compaction_commit_uses_latest_message_compare_and_set(
        self,
        message_manager: MessageManager,
        message_context: SessionContext,
    ) -> None:
        """两个摘要基于同一旧版本时，只允许一个推进 latest 指针。"""
        system = contract_message(
            message_context,
            "system",
            Role.SYSTEM,
            MessageOrigin.SYSTEM,
            "system",
            0,
            visibility=MessageVisibility.MODEL,
        )
        old_user = contract_message(
            message_context,
            "old",
            Role.USER,
            MessageOrigin.USER,
            "old",
            1,
            visibility=MessageVisibility.ALL,
        )
        live_user = contract_message(
            message_context,
            "live",
            Role.USER,
            MessageOrigin.USER,
            "live",
            2,
            visibility=MessageVisibility.ALL,
        )
        first = _compaction(
            message_context,
            "compaction-1",
            ("old",),
            "live",
            3,
        )
        stale = _compaction(
            message_context,
            "compaction-stale",
            ("old",),
            "live",
            4,
        )
        second = _compaction(
            message_context,
            "compaction-2",
            ("old", "live"),
            None,
            5,
        )

        await message_manager.commit(message_context, (system, old_user, live_user))
        await message_manager.commit_compaction(
            message_context,
            first,
            expected_latest_compaction_id=None,
        )
        await message_manager.commit_compaction(
            message_context,
            first,
            expected_latest_compaction_id=None,
        )
        with pytest.raises(AdapterError) as captured:
            await message_manager.commit_compaction(
                message_context,
                stale,
                expected_latest_compaction_id=None,
            )
        assert captured.value.code == "compaction_conflict"
        await message_manager.commit_compaction(
            message_context,
            second,
            expected_latest_compaction_id="compaction-1",
        )

        source = await message_manager.read_context_source(message_context)
        assert source.latest_compaction_message == second
        assert source.retained_messages == ()


def _compaction(
    ctx: SessionContext,
    message_id: str,
    covered: tuple[str, ...],
    next_id: str | None,
    offset: int,
) -> Message:
    return Message(
        message_id=message_id,
        session_id=ctx.session_id,
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        model="summary-model",
        run_id="run-1",
        created_at=CONTRACT_NOW + timedelta(seconds=offset),
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=covered,
                next_retained_message_id=next_id,
            ),
        ),
        visibility=MessageVisibility.ALL,
    )


def _injection(
    ctx: SessionContext,
    message_id: str,
    target_id: str,
    offset: int,
) -> Message:
    return Message(
        message_id=message_id,
        session_id=ctx.session_id,
        role=Role.USER,
        origin=MessageOrigin.CONTEXT_PROVIDER,
        run_id="run-1",
        created_at=CONTRACT_NOW + timedelta(seconds=offset),
        content=(InjectedBlock(target_user_message_id=target_id),),
        visibility=MessageVisibility.MODEL,
    )
