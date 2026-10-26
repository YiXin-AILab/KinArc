"""SqlMessageManager 的真实 MySQL 基础合约与存储专属测试。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import (
    AssetSource,
    CompactionBlock,
    ImageBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.message.sql.keys import session_scope_key
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.message.sql.schema import message_sessions, messages
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.contract.message_manager_contract import (
    BasicMessageManagerContract,
    CompactionMessageManagerContract,
)

NOW = datetime(2026, 8, 10, tzinfo=UTC)


@pytest.fixture
def message_context() -> SessionContext:
    """为每项合约提供独立的数据库 scope。"""
    suffix = uuid4().hex
    return SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id=f"mysql-contract-{suffix}",
    )


@pytest.fixture
async def message_manager(
    mysql_engine: AsyncEngine,
    message_context: SessionContext,
) -> AsyncIterator[MessageManager]:
    """提供注入共享 Engine 且按精确 scope 清理的 SQL Manager。"""
    manager = SqlMessageManager(mysql_engine, operation_timeout_seconds=5.0)
    try:
        yield manager
    finally:
        scope_key = session_scope_key(message_context)
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(messages).where(messages.c.session_scope_key == scope_key)
            )
            await connection.execute(
                delete(message_sessions).where(
                    message_sessions.c.scope_key == scope_key
                )
            )


class TestSqlBasicMessageManager(BasicMessageManagerContract):
    """在真实 MySQL 上运行共享基础合约。"""


class TestSqlCompactionMessageManager(CompactionMessageManagerContract):
    """在真实 MySQL 上运行共享上下文与 Compaction 合约。"""


async def test_empty_commit_does_not_create_session_head(
    mysql_engine: AsyncEngine,
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """空批次必须是完全无副作用的成功。"""
    await message_manager.commit(message_context, ())

    async with mysql_engine.connect() as connection:
        count = await connection.scalar(
            select(func.count())
            .select_from(message_sessions)
            .where(message_sessions.c.scope_key == session_scope_key(message_context))
        )
    assert count == 0


async def test_multimodal_payload_round_trips(
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """规范 payload 保留媒体引用和 Block 顺序。"""
    expected = Message(
        message_id="media-1",
        session_id=message_context.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(
            TextBlock(text="分析图片"),
            ImageBlock(
                source=AssetSource(
                    asset_id="sha256-" + "a" * 64,
                    media_type="image/png",
                    filename="image.png",
                )
            ),
        ),
        visibility=MessageVisibility.ALL,
    )

    await message_manager.commit(message_context, (expected,))

    assert await message_manager.get(message_context, expected.message_id) == expected


async def test_wrong_message_session_is_rejected_before_sql(
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """跨 session Message 不得进入事务。"""
    wrong = Message(
        message_id="wrong-session",
        session_id="another-session",
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text="wrong"),),
        visibility=MessageVisibility.ALL,
    )

    with pytest.raises(ValueError, match="session_id"):
        await message_manager.commit(message_context, (wrong,))


async def test_identity_and_message_ids_beyond_255_characters_work(
    mysql_engine: AsyncEngine,
) -> None:
    """原始身份与消息 ID 使用 TEXT，不受传统索引长度限制。"""
    suffix = uuid4().hex
    context = SessionContext(
        app_id="a" * 300,
        user_id="u" * 300,
        agent_id="g" * 300,
        session_id=f"{suffix}-" + "s" * 300,
    )
    manager = SqlMessageManager(mysql_engine, operation_timeout_seconds=5.0)
    expected = Message(
        message_id="m" * 300,
        session_id=context.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text="long"),),
        visibility=MessageVisibility.ALL,
    )
    try:
        await manager.commit(context, (expected,))
        assert await manager.get(context, expected.message_id) == expected
    finally:
        scope_key = session_scope_key(context)
        async with mysql_engine.begin() as connection:
            await connection.execute(
                delete(messages).where(messages.c.session_scope_key == scope_key)
            )
            await connection.execute(
                delete(message_sessions).where(
                    message_sessions.c.scope_key == scope_key
                )
            )


async def test_denormalized_origin_mismatch_is_corruption(
    mysql_engine: AsyncEngine,
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """筛选列与 payload 不一致时不得返回误导性结果。"""
    expected = Message(
        message_id="origin-mismatch",
        session_id=message_context.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text="hello"),),
        visibility=MessageVisibility.ALL,
    )
    await message_manager.commit(message_context, (expected,))
    async with mysql_engine.begin() as connection:
        await connection.execute(
            update(messages)
            .where(messages.c.session_scope_key == session_scope_key(message_context))
            .values(origin=MessageOrigin.SERVICE.value)
        )

    with pytest.raises(AdapterError, match="origin") as captured:
        await message_manager.list_messages(message_context)
    assert captured.value.code == "message_payload_invalid"


async def test_message_hash_collision_checks_raw_id(
    monkeypatch: pytest.MonkeyPatch,
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """摘要键碰撞必须由原始 message_id 校验识别。"""
    monkeypatch.setattr(
        "agentsystem.message.sql.manager.message_key",
        lambda _message_id: b"x" * 32,
    )
    first = Message(
        message_id="collision-a",
        session_id=message_context.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text="first"),),
        visibility=MessageVisibility.ALL,
    )
    second = first.model_copy(
        update={"message_id": "collision-b", "content": (TextBlock(text="second"),)}
    )
    await message_manager.commit(message_context, (first,))

    with pytest.raises(AdapterError, match="message_id") as captured:
        await message_manager.commit(message_context, (second,))
    assert captured.value.code == "message_payload_invalid"


async def test_compaction_same_id_changed_payload_conflicts(
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """Compaction 幂等判定必须先验证同 ID payload。"""
    first = Message(
        message_id="compaction-same-id",
        session_id=message_context.session_id,
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        run_id="run-1",
        created_at=NOW,
        content=(
            CompactionBlock(
                action="summarized",
                summary="first",
                compacted_message_ids=("old",),
            ),
        ),
        visibility=MessageVisibility.ALL,
    )
    changed = first.model_copy(
        update={
            "content": (
                CompactionBlock(
                    action="summarized",
                    summary="changed",
                    compacted_message_ids=("old",),
                ),
            )
        }
    )
    await message_manager.commit_compaction(
        message_context,
        first,
        expected_latest_compaction_id=None,
    )

    with pytest.raises(AdapterError) as captured:
        await message_manager.commit_compaction(
            message_context,
            changed,
            expected_latest_compaction_id=None,
        )
    assert captured.value.code == "message_conflict"


async def test_context_read_rejects_invalid_system_cardinality(
    message_manager: MessageManager,
    message_context: SessionContext,
) -> None:
    """SQL 快照仍须服从存储无关的 SYSTEM 唯一性不变量。"""
    user = Message(
        message_id="user-without-system",
        session_id=message_context.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text="hello"),),
        visibility=MessageVisibility.ALL,
    )
    await message_manager.commit(message_context, (user,))

    with pytest.raises(AdapterError) as captured:
        await message_manager.read_context_source(message_context)
    assert captured.value.code == "message_context_invalid"
