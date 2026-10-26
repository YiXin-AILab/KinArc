"""RedisMessageManager 的真实 Redis 合约测试。"""

from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError
from agentsystem.common.models import FinishReason, SessionContext
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    Base64Source,
    CompactionBlock,
    DocumentBlock,
    ImageBlock,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UrlSource,
    VideoBlock,
)
from agentsystem.message.redis.keys import MessageRedisKeys
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.redis.client import await_redis
from redis.asyncio import Redis
from tests.contract.redis.conftest import delete_prefix

NOW = datetime(2026, 8, 3, tzinfo=UTC)
CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def message(
    message_id: str,
    role: Role,
    origin: str,
    text: str,
    offset: int,
) -> Message:
    """构造普通 Message。"""
    return Message(
        message_id=message_id,
        session_id=CONTEXT.session_id,
        role=role,
        origin=origin,
        run_id=None if role is Role.SYSTEM else "run-1",
        created_at=NOW + timedelta(seconds=offset),
        content=(TextBlock(text=text),),
        visibility=(
            MessageVisibility.MODEL if role is Role.SYSTEM else MessageVisibility.ALL
        ),
    )


async def test_batch_commit_is_idempotent_atomic_and_conflict_checked(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """相同内容幂等，冲突批次不得留下部分新消息。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    system = message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    user = message("user-1", Role.USER, MessageOrigin.USER, "hello", 1)
    changed = user.model_copy(update={"content": (TextBlock(text="changed"),)})
    new_user = message("user-2", Role.USER, MessageOrigin.USER, "new", 2)
    try:
        await manager.commit(CONTEXT, (system, user))
        await manager.commit(CONTEXT, (system, user))
        with pytest.raises(AdapterError) as captured:
            await manager.commit(CONTEXT, (new_user, changed))
        assert captured.value.code == "message_conflict"

        assert await manager.get(CONTEXT, "user-2") is None
        stored = await manager.list_messages(CONTEXT)
        assert [item.message_id for item in stored] == ["system", "user-1"]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_listing_returns_all_messages_and_preserves_order(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """权威查询返回全部消息，并保持稳定顺序。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    system = message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    user = message("user-1", Role.USER, MessageOrigin.USER, "hello", 1)
    assistant = message("assistant-1", Role.ASSISTANT, MessageOrigin.MODEL, "answer", 2)
    dynamic_system = Message(
        message_id="dynamic-system",
        session_id=CONTEXT.session_id,
        role=Role.SYSTEM,
        origin="service",
        run_id="run-1",
        created_at=NOW + timedelta(seconds=3),
        content=(TextBlock(text="business notice"),),
        visibility=MessageVisibility.ALL,
    )
    try:
        await manager.commit(CONTEXT, (system, user, assistant, dynamic_system))

        stored = await manager.list_messages(CONTEXT)

        assert [item.message_id for item in stored] == [
            "system",
            "user-1",
            "assistant-1",
            "dynamic-system",
        ]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_multimodal_sources_round_trip_without_asset_body(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """三种 Source 与四种 Block 保持顺序，Asset 引用不携带文件正文。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    mixed = Message(
        message_id="media-1",
        session_id=CONTEXT.session_id,
        role=Role.USER,
        origin=MessageOrigin.USER,
        run_id="run-1",
        created_at=NOW,
        content=(
            TextBlock(text="按顺序分析"),
            ImageBlock(
                source=UrlSource(
                    url="https://example.test/image.png?private=query",
                    media_type="image/png",
                )
            ),
            AudioBlock(source=Base64Source(data="UklGRg==", media_type="audio/wav")),
            VideoBlock(
                source=AssetSource(
                    asset_id="sha256-" + "a" * 64,
                    media_type="video/mp4",
                    filename="clip.mp4",
                )
            ),
            DocumentBlock(
                source=AssetSource(
                    asset_id="sha256-" + "b" * 64,
                    media_type="application/pdf",
                    filename="report.pdf",
                )
            ),
        ),
        visibility=MessageVisibility.ALL,
    )
    try:
        await manager.commit(CONTEXT, (mixed,))

        stored = await manager.get(CONTEXT, mixed.message_id)
        assert stored == mixed
        assert stored is not None
        raw = await await_redis(
            redis_client.hget(
                MessageRedisKeys(redis_prefix).data(CONTEXT),
                mixed.message_id,
            )
        )
        assert raw is not None
        raw_bytes = raw.encode() if isinstance(raw, str) else raw
        assert b"raw-video-body" not in raw_bytes
        assert [block.type for block in stored.content] == [
            "text",
            "image",
            "audio",
            "video",
            "document",
        ]
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_context_source_selects_latest_compaction_and_live_injection(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """上下文快照排除已压缩前缀和指向该前缀的 Injection。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    system = message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    old_user = message("user-old", Role.USER, MessageOrigin.USER, "old", 1)
    live_user = message("user-live", Role.USER, MessageOrigin.USER, "live", 2)
    compaction = Message(
        message_id="compaction-1",
        session_id=CONTEXT.session_id,
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=3),
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=("user-old",),
                next_retained_message_id="user-live",
            ),
        ),
        visibility=MessageVisibility.ALL,
    )
    old_injection = Message(
        message_id="injection-old",
        session_id=CONTEXT.session_id,
        role=Role.USER,
        origin=MessageOrigin.CONTEXT_PROVIDER,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=4),
        content=(InjectedBlock(target_user_message_id="user-old"),),
        visibility=MessageVisibility.MODEL,
    )
    live_injection = Message(
        message_id="injection-live",
        session_id=CONTEXT.session_id,
        role=Role.USER,
        origin=MessageOrigin.CONTEXT_PROVIDER,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=5),
        content=(InjectedBlock(target_user_message_id="user-live"),),
        visibility=MessageVisibility.MODEL,
    )
    try:
        await manager.commit(
            CONTEXT,
            (
                system,
                old_user,
                live_user,
                compaction,
                old_injection,
                live_injection,
            ),
        )

        context = await manager.read_context_source(CONTEXT)

        assert context.system_message == system
        assert context.latest_compaction_message == compaction
        assert context.retained_messages == (live_user,)
        assert context.injection_messages == (live_injection,)
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_compaction_commit_uses_latest_message_compare_and_set(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """两个并发摘要基于同一旧版本时，只允许一个推进 latest 指针。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    system = message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    old_user = message("old", Role.USER, MessageOrigin.USER, "old", 1)
    live_user = message("live", Role.USER, MessageOrigin.USER, "live", 2)
    first = _compaction("compaction-1", ("old",), "live", 3)
    stale = _compaction("compaction-stale", ("old",), "live", 4)
    second = _compaction("compaction-2", ("old", "live"), None, 5)
    try:
        await manager.commit(CONTEXT, (system, old_user, live_user))
        await manager.commit_compaction(
            CONTEXT,
            first,
            expected_latest_compaction_id=None,
        )
        await manager.commit_compaction(
            CONTEXT,
            first,
            expected_latest_compaction_id=None,
        )
        with pytest.raises(AdapterError) as captured:
            await manager.commit_compaction(
                CONTEXT,
                stale,
                expected_latest_compaction_id=None,
            )
        assert captured.value.code == "compaction_conflict"
        await manager.commit_compaction(
            CONTEXT,
            second,
            expected_latest_compaction_id="compaction-1",
        )

        source = await manager.read_context_source(CONTEXT)
        assert source.latest_compaction_message == second
        assert source.retained_messages == ()
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_pure_pruning_snapshot_keeps_raw_messages_retained(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """剪枝快照只保存投影状态，Redis 中的工具原文继续 retained。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    system = message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    user = message("user", Role.USER, MessageOrigin.USER, "question", 1)
    assistant, tool = _tool_round(offset=2)
    pruning = _pruning_compaction(
        "pruning",
        pruned_ids=(assistant.message_id, tool.message_id),
        offset=4,
    )
    try:
        await manager.commit(CONTEXT, (system, user, assistant, tool))
        await manager.commit_compaction(
            CONTEXT,
            pruning,
            expected_latest_compaction_id=None,
        )

        source = await manager.read_context_source(CONTEXT)

        assert source.latest_compaction_message == pruning
        assert source.retained_messages == (user, assistant, tool)
        stored_tool = await manager.get(CONTEXT, tool.message_id)
        assert stored_tool == tool
    finally:
        await delete_prefix(redis_client, redis_prefix)


async def test_summary_replaces_prior_pruning_snapshot(
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """新摘要快照清空旧剪枝状态并按 covered IDs 排除前缀。"""
    manager = RedisMessageManager(redis_client, prefix=redis_prefix)
    system = message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", 0)
    old_user = message("old", Role.USER, MessageOrigin.USER, "old", 1)
    assistant, tool = _tool_round(offset=2)
    live_user = message("live", Role.USER, MessageOrigin.USER, "live", 4)
    pruning = _pruning_compaction(
        "pruning",
        pruned_ids=(assistant.message_id, tool.message_id),
        offset=5,
    )
    summary = _compaction(
        "summary",
        (old_user.message_id, assistant.message_id, tool.message_id),
        live_user.message_id,
        6,
    )
    try:
        await manager.commit(CONTEXT, (system, old_user, assistant, tool, live_user))
        await manager.commit_compaction(
            CONTEXT,
            pruning,
            expected_latest_compaction_id=None,
        )
        await manager.commit_compaction(
            CONTEXT,
            summary,
            expected_latest_compaction_id=pruning.message_id,
        )

        source = await manager.read_context_source(CONTEXT)
        assert source.latest_compaction_message == summary
        latest = source.latest_compaction_message
        assert latest is not None
        block = latest.content[0]
        assert isinstance(block, CompactionBlock)
        assert block.pruned_message_ids == ()
        assert source.retained_messages == (live_user,)
    finally:
        await delete_prefix(redis_client, redis_prefix)


def _compaction(
    message_id: str,
    covered: tuple[str, ...],
    next_id: str | None,
    offset: int,
) -> Message:
    return Message(
        message_id=message_id,
        session_id=CONTEXT.session_id,
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        model="summary-model",
        run_id="run-1",
        created_at=NOW + timedelta(seconds=offset),
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


def _pruning_compaction(
    message_id: str,
    *,
    pruned_ids: tuple[str, ...],
    offset: int,
) -> Message:
    return Message(
        message_id=message_id,
        session_id=CONTEXT.session_id,
        role=Role.USER,
        origin=MessageOrigin.COMPACTOR,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=offset),
        content=(CompactionBlock(action="pruned", pruned_message_ids=pruned_ids),),
        visibility=MessageVisibility.ALL,
    )


def _tool_round(*, offset: int) -> tuple[Message, Message]:
    assistant = Message(
        message_id="assistant-tool",
        session_id=CONTEXT.session_id,
        role=Role.ASSISTANT,
        origin=MessageOrigin.MODEL,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=offset),
        stop_reason=FinishReason.TOOL_CALLS,
        content=(
            ToolCallBlock(
                tool_call_id="call",
                tool_name="lookup",
                arguments={"payload": "raw" * 1_000},
                raw_arguments='{"payload":"' + "raw" * 1_000 + '"}',
            ),
        ),
        visibility=MessageVisibility.ALL,
    )
    tool = Message(
        message_id="tool-result",
        session_id=CONTEXT.session_id,
        role=Role.TOOL,
        origin=MessageOrigin.TOOL,
        run_id="run-1",
        created_at=NOW + timedelta(seconds=offset + 1),
        content=(
            ToolResultBlock(
                tool_call_id="call",
                tool_name="lookup",
                content="raw" * 1_000,
            ),
        ),
        visibility=MessageVisibility.ALL,
    )
    return assistant, tool
