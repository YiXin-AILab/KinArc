"""Message 与 Block 跨字段约束测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import RunError
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
    StoredBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    VideoBlock,
    is_inbound_message,
    is_model_visible,
    is_public_visible,
)
from pydantic import TypeAdapter, ValidationError

NOW = datetime(2026, 8, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("visibility", "model_visible", "public_visible"),
    [
        (MessageVisibility.MODEL, True, False),
        (MessageVisibility.PUBLIC, False, True),
        (MessageVisibility.ALL, True, True),
    ],
)
def test_message_visibility_is_explicit(
    visibility: MessageVisibility,
    model_visible: bool,
    public_visible: bool,
) -> None:
    """Visibility 必须独立控制模型上下文与公开投影。"""
    role = Role.ASSISTANT if visibility is MessageVisibility.PUBLIC else Role.USER
    message = Message(
        message_id="message",
        session_id="session",
        role=role,
        content=(TextBlock(text="hello"),),
        origin=MessageOrigin.USER,
        visibility=visibility,
        run_id="run",
        created_at=NOW,
        error=(
            RunError(code="failed", message="failed")
            if visibility is MessageVisibility.PUBLIC
            else None
        ),
    )

    assert is_model_visible(message) is model_visible
    assert is_public_visible(message) is public_visible


def test_message_requires_non_null_role_and_visibility() -> None:
    """持久化 Message 不得省略 visibility 或使用空 role。"""
    payload = {
        "message_id": "message",
        "session_id": "session",
        "role": None,
        "content": ({"type": "text", "text": "hello"},),
        "origin": "user",
        "run_id": "run",
        "created_at": NOW,
    }

    with pytest.raises(ValidationError):
        Message.model_validate(payload)


def test_compaction_block_accepts_pure_pruning_snapshot() -> None:
    """纯剪枝快照不需要摘要边界。"""
    block = CompactionBlock(action="pruned", pruned_message_ids=("tool-1",))

    assert block.summary is None
    assert block.compacted_message_ids == ()
    assert block.next_retained_message_id is None


def test_compaction_block_accepts_summary_then_pruning_snapshot() -> None:
    """摘要后的剪枝快照必须能够继承完整摘要。"""
    block = CompactionBlock(
        action="pruned",
        summary="old summary",
        compacted_message_ids=("old-user", "old-assistant"),
        next_retained_message_id="live-user",
        pruned_message_ids=("tool-1",),
    )

    assert block.summary == "old summary"
    assert block.pruned_message_ids == ("tool-1",)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": "summarized",
            "summary": "x",
            "compacted_message_ids": ("a",),
            "pruned_message_ids": ("b",),
        },
        {
            "action": "summarized",
            "summary": None,
            "compacted_message_ids": ("a",),
        },
        {
            "action": "pruned",
            "summary": None,
            "next_retained_message_id": "live",
        },
        {"action": "pruned", "pruned_message_ids": ("a", "a")},
        {
            "action": "pruned",
            "summary": "x",
            "compacted_message_ids": ("a",),
            "next_retained_message_id": "live",
            "pruned_message_ids": ("a",),
        },
    ],
)
def test_compaction_block_rejects_incoherent_snapshot(
    payload: dict[str, object],
) -> None:
    """摘要、边界和剪枝集合必须形成完整且互斥的快照。"""
    with pytest.raises(ValidationError):
        CompactionBlock.model_validate(payload)


def test_media_block_round_trips_nested_source() -> None:
    """StoredBlock 必须保留媒体与嵌套 Source 的判别类型。"""
    block = ImageBlock(
        source=Base64Source(media_type="image/png", data="aGVsbG8="),
        detail="high",
    )

    parsed: StoredBlock = TypeAdapter(StoredBlock).validate_python(
        block.model_dump(mode="json")
    )

    assert parsed == block


def test_asset_source_is_immutable() -> None:
    """持久化资产引用不得被原地替换。"""
    source = AssetSource(
        asset_id="sha256-" + "a" * 64,
        media_type="video/mp4",
        filename="clip.mp4",
    )

    field_name = "asset_id"
    with pytest.raises(ValidationError):
        setattr(source, field_name, "changed")


@pytest.mark.parametrize(
    "block",
    [
        ImageBlock(source=Base64Source(data="eA==", media_type="image/png")),
        AudioBlock(source=Base64Source(data="eA==", media_type="audio/mpeg")),
        VideoBlock(source=Base64Source(data="eA==", media_type="video/mp4")),
        DocumentBlock(source=Base64Source(data="eA==", media_type="application/pdf")),
    ],
)
def test_user_message_accepts_all_media_blocks(
    block: ImageBlock | AudioBlock | VideoBlock | DocumentBlock,
) -> None:
    """USER Message 必须接受四类媒体且保持 Block 原值。"""
    message = Message(
        message_id="user-message",
        session_id="session",
        role=Role.USER,
        origin=MessageOrigin.USER,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=NOW,
        content=(block,),
    )

    assert message.content == (block,)


@pytest.mark.parametrize("role", [Role.ASSISTANT, Role.TOOL])
def test_non_user_roles_reject_media(role: Role) -> None:
    """媒体不能越过 USER 角色边界。"""
    kwargs = {
        "message_id": f"{role}-message",
        "session_id": "session",
        "role": role,
        "origin": {
            Role.ASSISTANT: MessageOrigin.MODEL,
            Role.TOOL: MessageOrigin.TOOL,
        }[role],
        "created_at": NOW,
        "content": (
            ImageBlock(source=Base64Source(data="eA==", media_type="image/png")),
        ),
    }
    kwargs["run_id"] = "run"

    with pytest.raises(ValidationError):
        Message.model_validate(kwargs)


@pytest.mark.parametrize(
    ("block_type", "media_type"),
    [
        (ImageBlock, "audio/mpeg"),
        (AudioBlock, "video/mp4"),
        (VideoBlock, "image/png"),
        (DocumentBlock, "application/octet-stream"),
    ],
)
def test_media_block_rejects_mismatched_mime(
    block_type: type[ImageBlock | AudioBlock | VideoBlock | DocumentBlock],
    media_type: str,
) -> None:
    """Block 类别与声明 MIME 不一致时必须在领域边界失败。"""
    with pytest.raises(ValidationError):
        block_type(source=Base64Source(data="eA==", media_type=media_type))


def test_assistant_rejects_tool_result() -> None:
    """ASSISTANT 只能保存文本、思考和 ToolCall。"""
    with pytest.raises(ValidationError):
        Message(
            message_id="assistant-message",
            session_id="session",
            role=Role.ASSISTANT,
            origin=MessageOrigin.MODEL,
            visibility=MessageVisibility.ALL,
            run_id="run",
            created_at=NOW,
            content=(
                ToolResultBlock(
                    tool_call_id="call",
                    tool_name="search",
                    content="result",
                ),
            ),
        )


def test_user_rejects_tool_call() -> None:
    """USER 不能伪造模型产生的 ToolCall。"""
    with pytest.raises(ValidationError):
        Message(
            message_id="user-message",
            session_id="session",
            role=Role.USER,
            origin=MessageOrigin.USER,
            visibility=MessageVisibility.ALL,
            run_id="run",
            created_at=NOW,
            content=(
                ToolCallBlock(
                    tool_call_id="call",
                    tool_name="search",
                    raw_arguments="{}",
                    arguments={},
                ),
            ),
        )


def test_tool_message_requires_one_matching_tool_result() -> None:
    """TOOL Message 必须只包含一个 ToolResultBlock。"""
    with pytest.raises(ValidationError):
        Message(
            message_id="tool-message",
            session_id="session",
            role=Role.TOOL,
            origin=MessageOrigin.TOOL,
            visibility=MessageVisibility.ALL,
            run_id="run",
            created_at=NOW,
            content=(TextBlock(text="not a tool result"),),
        )

    result = ToolResultBlock(
        tool_call_id="call",
        tool_name="search",
        content="result",
    )
    with pytest.raises(ValidationError):
        Message(
            message_id="duplicate-tool-message",
            session_id="session",
            role=Role.TOOL,
            origin=MessageOrigin.TOOL,
            visibility=MessageVisibility.ALL,
            run_id="run",
            created_at=NOW,
            content=(result, result),
        )

    message = Message(
        message_id="valid-tool-message",
        session_id="session",
        role=Role.TOOL,
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=NOW,
        content=(result,),
    )
    assert message.content == (result,)


def test_injection_uses_user_role_and_model_visibility() -> None:
    """Injection 必须是只供模型消费的 USER Message。"""
    injected = InjectedBlock(target_user_message_id="user-message")
    with pytest.raises(ValidationError):
        Message(
            message_id="injection-message",
            session_id="session",
            role=Role.USER,
            origin="context_provider",
            visibility=MessageVisibility.MODEL,
            run_id="run",
            created_at=NOW,
            content=(injected, TextBlock(text="mixed")),
        )

    with pytest.raises(ValidationError):
        Message(
            message_id="service-injection-message",
            session_id="session",
            role=Role.USER,
            origin=MessageOrigin.SERVICE,
            visibility=MessageVisibility.MODEL,
            run_id="run",
            created_at=NOW,
            content=(injected,),
        )

    message = Message(
        message_id="valid-injection-message",
        session_id="session",
        role=Role.USER,
        origin="context_provider",
        visibility=MessageVisibility.MODEL,
        run_id="run",
        created_at=NOW,
        content=(injected,),
    )
    assert is_model_visible(message)
    assert not is_public_visible(message)
    assert not is_inbound_message(message)
    assert message.origin == "context_provider"


def test_compaction_uses_user_role_and_all_visibility() -> None:
    """Compaction 对模型和公开安全占位均可见，但不是 Inbox 输入。"""
    with pytest.raises(ValidationError):
        Message(
            message_id="service-compaction",
            session_id="session",
            role=Role.USER,
            content=(
                CompactionBlock(
                    action="summarized",
                    summary="summary",
                    compacted_message_ids=("old",),
                ),
            ),
            origin=MessageOrigin.SERVICE,
            visibility=MessageVisibility.ALL,
            run_id="run",
            created_at=NOW,
        )

    message = Message(
        message_id="compaction",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="summarized",
                summary="summary",
                compacted_message_ids=("old",),
            ),
        ),
        origin="compactor",
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=NOW,
    )

    assert is_model_visible(message)
    assert is_public_visible(message)
    assert not is_inbound_message(message)
    assert message.origin == "compactor"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "message_id": "injection-all",
            "role": Role.USER,
            "content": (InjectedBlock(target_user_message_id="user"),),
            "visibility": MessageVisibility.ALL,
            "run_id": "run",
        },
        {
            "message_id": "compaction-model",
            "role": Role.USER,
            "content": (
                CompactionBlock(
                    action="summarized",
                    summary="summary",
                    compacted_message_ids=("old",),
                ),
            ),
            "visibility": MessageVisibility.MODEL,
            "run_id": "run",
        },
        {
            "message_id": "tool-model",
            "role": Role.TOOL,
            "content": (
                ToolResultBlock(
                    tool_call_id="call",
                    tool_name="tool",
                    content="result",
                ),
            ),
            "visibility": MessageVisibility.MODEL,
            "run_id": "run",
        },
        {
            "message_id": "assistant-error-all",
            "role": Role.ASSISTANT,
            "content": (TextBlock(text="failed"),),
            "visibility": MessageVisibility.ALL,
            "run_id": "run",
            "error": RunError(code="failed", message="failed"),
        },
        {
            "message_id": "base-system-all",
            "role": Role.SYSTEM,
            "content": (TextBlock(text="prompt"),),
            "visibility": MessageVisibility.ALL,
            "run_id": None,
        },
    ],
)
def test_message_rejects_visibility_that_conflicts_with_structure(
    payload: dict[str, object],
) -> None:
    """结构语义与 visibility 冲突时必须在持久化边界失败。"""
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "session_id": "session",
                "origin": MessageOrigin.SERVICE,
                "created_at": NOW,
                **payload,
            }
        )


def test_dynamic_system_requires_run_and_accepts_user_media() -> None:
    """动态 SYSTEM 输入绑定 run，并接受用户媒体内容。"""
    dynamic = Message(
        message_id="system-input",
        session_id="session",
        role=Role.SYSTEM,
        origin="order-service",
        visibility=MessageVisibility.ALL,
        run_id="run-1",
        created_at=NOW,
        content=(ImageBlock(source=Base64Source(data="eA==", media_type="image/png")),),
    )

    assert is_inbound_message(dynamic)


def test_base_system_uses_system_origin_and_is_model_only() -> None:
    """基础 SYSTEM 由 system 生产且只参与模型上下文。"""
    with pytest.raises(ValidationError):
        Message(
            message_id="service-base-system",
            session_id="session",
            role=Role.SYSTEM,
            origin=MessageOrigin.SERVICE,
            visibility=MessageVisibility.MODEL,
            created_at=NOW,
            content=(TextBlock(text="prompt"),),
        )

    base = Message(
        message_id="base-system",
        session_id="session",
        role=Role.SYSTEM,
        origin="system",
        visibility=MessageVisibility.MODEL,
        created_at=NOW,
        content=(TextBlock(text="prompt"),),
    )

    assert is_model_visible(base)
    assert not is_public_visible(base)
    assert not is_inbound_message(base)
    assert base.origin == "system"


@pytest.mark.parametrize("role", [Role.USER, Role.SYSTEM])
def test_service_origin_does_not_change_inbound_visibility(role: Role) -> None:
    """origin 的字符串值不参与可见性判断。"""
    message = Message(
        message_id=f"{role.value}-message",
        session_id="session",
        role=role,
        origin="service",
        visibility=MessageVisibility.ALL,
        run_id="run-1",
        created_at=NOW,
        content=(TextBlock(text="visible"),),
    )

    assert is_model_visible(message)
    assert is_public_visible(message)
    assert is_inbound_message(message)


def test_error_is_allowed_only_on_assistant() -> None:
    """结构化运行时错误只能附着在 ASSISTANT Message。"""
    error = RunError(code="bad", message="bad")
    with pytest.raises(ValidationError):
        Message(
            message_id="user-message",
            session_id="session",
            role=Role.USER,
            origin=MessageOrigin.USER,
            visibility=MessageVisibility.ALL,
            run_id="run",
            created_at=NOW,
            content=(TextBlock(text="hello"),),
            error=error,
        )

    message = Message(
        message_id="assistant-message",
        session_id="session",
        role=Role.ASSISTANT,
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.PUBLIC,
        run_id="run",
        created_at=NOW,
        content=(TextBlock(text="failed"),),
        error=error,
    )
    assert message.error == error


def test_message_and_blocks_are_immutable() -> None:
    """Message 与其中的 Block 均不得原地修改。"""
    block = ToolResultBlock(
        tool_call_id="call",
        tool_name="search",
        content="result",
    )
    message = Message(
        message_id="tool-message",
        session_id="session",
        role=Role.TOOL,
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id="run",
        created_at=NOW,
        content=(block,),
    )

    message_field = "run_id"
    block_field = "content"
    with pytest.raises(ValidationError):
        setattr(message, message_field, "changed")
    with pytest.raises(ValidationError):
        setattr(block, block_field, "changed")
