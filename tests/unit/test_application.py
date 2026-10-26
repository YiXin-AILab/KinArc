"""ApplicationService 的 Inbox 提交与内部 resume 解析测试。"""

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from agentsystem.asset import AssetContent, AssetRecord
from agentsystem.asset.service import AssetService
from agentsystem.cancellation.models import CancellationCommand as BroadcastCancel
from agentsystem.cancellation.redis_bus import RedisCancellationBus
from agentsystem.common.errors import AdapterError, ServiceError, ServiceErrorKind
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentMediaConfig,
    MediaArchiveRuleConfig,
    PublicStreamConfig,
)
from agentsystem.inbox.models import InboundMessage, InboundRole, ResumeRequest
from agentsystem.llm.models import Usage
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
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserContentBlock,
    VideoBlock,
)
from agentsystem.message.sessions import SessionInfo
from agentsystem.outbound.event_bus import (
    EventReadBatch,
    SessionEventBus,
    StreamInspection,
)
from agentsystem.outbound.public_content import PublicContentPolicy
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
    PublicStreamLifecycle,
    RunSuspendedPayload,
    RunTerminalPayload,
)
from agentsystem.runtime.session import AgentSession
from agentsystem.runtime.state import (
    AgentState,
    RunTiming,
    complete_run,
    start_run,
    suspend_run,
)
from agentsystem.service.agents import AgentRegistry, AgentRuntimeBundle
from agentsystem.service.application import ApplicationService as _ApplicationService
from agentsystem.service.models import (
    ApplicationStatus,
    CancelCommand,
    MessageSubmissionResult,
    PendingInputsView,
    PendingInputView,
    ResumeCommand,
    SessionImageBlockView,
    SessionMessagesView,
    SessionPublicResult,
    SessionRunTiming,
    SessionTextBlockView,
    SessionTokenUsage,
    SubmitMessageCommand,
)
from agentsystem.service.presentation import present_message
from tests.unit.runtime.port_stubs import (
    InboxStub,
    MessageManagerStub,
    StateManagerStub,
)

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_ASSET_BODY = b"asset-body"
_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


class _Queue(InboxStub):
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []
        self.resumes: list[ResumeRequest] = []

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        assert ctx == _CONTEXT
        self.messages.append(message)

    async def push_resume(self, request: ResumeRequest) -> None:
        self.resumes.append(request)

    async def list_pending(
        self,
        ctx: SessionContext,
    ) -> tuple[InboundMessage, ...]:
        assert ctx == _CONTEXT
        return tuple(self.messages)


class _Agents(AgentRegistry):
    """只允许测试固定 Agent 的可用性端口。"""

    def __init__(
        self,
        rules: tuple[MediaArchiveRuleConfig, ...] = (),
        session: object | None = None,
    ) -> None:
        self._selection = AgentRuntimeBundle(
            agent_config=AgentConfig.model_validate(
                {
                    "agent_id": "agent",
                    "agent_name": "Agent",
                    "prompt": "prompt.md",
                    "outbound": {"type": "test"},
                    "llm_model": {
                        "type": "litellm",
                        "model": "openai/test",
                    },
                    "media": AgentMediaConfig(archive_rules=rules),
                }
            ),
            session=cast(AgentSession, session or object()),
        )

    def require(self, agent_id: str) -> AgentRuntimeBundle:
        if agent_id != _CONTEXT.agent_id:
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="agent_not_found",
                message="Agent 不存在",
            )
        return self._selection


def _bundle(
    agent_id: str,
    agent_name: str,
    output_mode: OutputMode,
    metadata: dict[str, str | int],
) -> AgentRuntimeBundle:
    """构造目录测试所需的最小真实 Registry Bundle。"""
    return AgentRuntimeBundle(
        agent_config=AgentConfig.model_validate(
            {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "prompt": "prompt.md",
                "output_mode": output_mode,
                "metadata": metadata,
                "outbound": {"type": "test"},
                "llm_model": {
                    "type": "litellm",
                    "model": "openai/test",
                },
            }
        ),
        session=cast(AgentSession, object()),
    )


class _PreparingAssets(AssetService):
    """记录 Application 调用顺序的媒体准备端口。"""

    def __init__(
        self,
        result: tuple[UserContentBlock, ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[
            tuple[
                tuple[UserContentBlock, ...],
                tuple[MediaArchiveRuleConfig, ...],
            ]
        ] = []
        self.uploads: list[tuple[bytes, str | None, str | None]] = []
        self.opened: list[str] = []

    async def prepare(
        self,
        content: tuple[UserContentBlock, ...],
        rules: tuple[MediaArchiveRuleConfig, ...],
    ) -> tuple[UserContentBlock, ...]:
        """记录参数后返回替换内容或抛出指定错误。"""
        self.calls.append((content, rules))
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else content

    async def upload(
        self,
        body: AsyncIterable[bytes],
        *,
        declared_media_type: str | None,
        filename: str | None,
    ) -> AssetRecord:
        """记录传输无关上传并返回稳定记录。"""
        value = b"".join([chunk async for chunk in body])
        self.uploads.append((value, declared_media_type, filename))
        return AssetRecord(
            asset_id="group2/M00/demo.png",
            media_type="image/png",
            filename=filename,
            size_bytes=len(value),
            checksum_sha256="a" * 64,
        )

    @asynccontextmanager
    async def open(self, asset_id: str) -> AsyncIterator[AssetContent]:
        """记录 opaque ID 并返回固定正文。"""
        self.opened.append(asset_id)
        yield AssetContent(media_type="image/png", body=_asset_body())


async def _asset_body() -> AsyncIterator[bytes]:
    yield _ASSET_BODY


class _States(StateManagerStub):
    def __init__(
        self,
        state: AgentState | None,
        error: AdapterError | None = None,
    ) -> None:
        self.state = state
        self.error = error

    async def get(self, ctx: SessionContext) -> AgentState | None:
        assert ctx == _CONTEXT
        if self.error is not None:
            raise self.error
        return self.state


class _SessionControl:
    def __init__(self) -> None:
        self.calls: list[tuple[SessionContext, str]] = []

    async def cancel_pending(
        self,
        ctx: SessionContext,
        run_id: str,
    ) -> bool:
        self.calls.append((ctx, run_id))
        return True


class _CancellationBus:
    def __init__(self, error: Exception | None = None) -> None:
        self.commands: list[BroadcastCancel] = []
        self.error = error

    async def publish(self, command: BroadcastCancel) -> int:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return 1


class _Messages(MessageManagerStub):
    async def ensure_session(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        if self.error is not None:
            raise self.error
        return SessionInfo(ctx=ctx, title=title, created_at=now, updated_at=now)

    def __init__(
        self,
        messages: tuple[Message, ...],
        error: AdapterError | None = None,
    ) -> None:
        self.messages = messages
        self.error = error
        self.get_calls: list[tuple[SessionContext, str]] = []
        self.list_calls: list[SessionContext] = []

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        self.get_calls.append((ctx, message_id))
        if self.error is not None:
            raise self.error
        return next(
            (message for message in self.messages if message.message_id == message_id),
            None,
        )

    async def list_messages(
        self,
        ctx: SessionContext,
    ) -> tuple[Message, ...]:
        assert ctx == _CONTEXT
        self.list_calls.append(ctx)
        if self.error is not None:
            raise self.error
        return self.messages


class _EventBus:
    def __init__(
        self,
        inspection: StreamInspection,
        batch: EventReadBatch | None = None,
    ) -> None:
        self.inspection = inspection
        self.batch = batch
        self.inspected: list[PublicEventCursor | None] = []

    async def inspect(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
    ) -> StreamInspection:
        assert ctx == _CONTEXT
        self.inspected.append(cursor)
        return self.inspection

    async def read(
        self,
        ctx: SessionContext,
        cursor: PublicEventCursor | None,
        *,
        count: int,
        block_ms: int,
    ) -> EventReadBatch:
        assert ctx == _CONTEXT
        del cursor, count, block_ms
        if self.batch is None:
            raise AssertionError("本测试不读取 batch")
        return self.batch

    async def append(
        self,
        ctx: SessionContext,
        event: PublicStreamEvent,
    ) -> PublicEventRecord:
        del ctx, event
        raise AssertionError("Application 不追加事件")


def ApplicationService(
    *,
    agents: AgentRegistry | _Agents,
    inbox: _Queue,
    states: _States,
    asset_service: AssetService | None = None,
    messages: MessageManagerStub | None = None,
    event_bus: object | None = None,
    cancellation_bus: _CancellationBus | None = None,
    **options: Any,
) -> _ApplicationService:
    """为单元测试补齐生产必需端口，避免构造非法半对象。"""
    return _ApplicationService(
        agents=agents,
        inbox=inbox,
        states=states,
        asset_service=asset_service or _PreparingAssets(),
        messages=messages or _Messages(()),
        event_bus=cast(SessionEventBus, event_bus or object()),
        cancellation_bus=cast(
            RedisCancellationBus,
            cancellation_bus or _CancellationBus(),
        ),
        **options,
    )


def test_agent_catalog_uses_registered_bundles_in_stable_order() -> None:
    """目录必须捕获排序、字段泄漏和非法描述回退。"""
    registry = AgentRegistry(
        (
            _bundle("z-agent", "Zed", OutputMode.COMPLETE, {"description": 7}),
            _bundle(
                "a-agent",
                "Alpha",
                OutputMode.STREAM,
                {"description": "  first agent  "},
            ),
        )
    )
    app = ApplicationService(inbox=_Queue(), states=_States(None), agents=registry)

    assert app.list_agents().model_dump(mode="json") == {
        "agents": [
            {
                "agent_id": "a-agent",
                "agent_name": "Alpha",
                "description": "first agent",
                "output_mode": "stream",
            },
            {
                "agent_id": "z-agent",
                "agent_name": "Zed",
                "description": "",
                "output_mode": "complete",
            },
        ]
    }


async def test_submit_message_maps_public_text_to_inbound() -> None:
    """Application 生成 Inbox DTO，不生成或公开 run_id。"""
    queue = _Queue()
    app = ApplicationService(
        agents=_Agents(), inbox=queue, states=_States(None), clock=lambda: _NOW
    )
    request = SubmitMessageCommand(
        message_id="system-1",
        role=InboundRole.SYSTEM,
        origin="crm.workflow",
        content=(TextBlock(text="updated"),),
    )

    result = await app.submit_message(_CONTEXT, request)

    assert result == MessageSubmissionResult(message_id="system-1")
    assert queue.messages[0].message_id == "system-1"
    assert queue.messages[0].role is InboundRole.SYSTEM
    assert queue.messages[0].origin == "crm.workflow"
    assert queue.messages[0].created_at == _NOW
    block = queue.messages[0].content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "updated"


async def test_asset_operations_delegate_without_session_identity() -> None:
    """错误地要求 Agent/session 会阻止前端先上传再组装消息。"""
    assets = _PreparingAssets()
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        asset_service=assets,
    )

    record = await app.upload_asset(
        _asset_body(),
        declared_media_type="image/png",
        filename="demo.png",
    )
    async with app.open_asset("group2/M00/demo.png") as content:
        downloaded = b"".join([chunk async for chunk in content.body])

    assert record.asset_id == "group2/M00/demo.png"
    assert assets.uploads == [(_ASSET_BODY, "image/png", "demo.png")]
    assert assets.opened == ["group2/M00/demo.png"]
    assert downloaded == _ASSET_BODY


async def test_submit_prepares_media_with_selected_agent_rules() -> None:
    """Application 必须在 queue push 前按目标 Agent 规则准备媒体。"""
    rule = MediaArchiveRuleConfig(
        block_types=("image",),
        source_types=("base64",),
    )
    original = ImageBlock(source=Base64Source(data="aGVsbG8=", media_type="image/png"))
    archived = ImageBlock(
        source=AssetSource(
            asset_id="sha256-" + "a" * 64,
            media_type="image/png",
        )
    )
    assets = _PreparingAssets(result=(TextBlock(text="look"), archived))
    queue = _Queue()
    app = ApplicationService(
        agents=_Agents((rule,)),
        inbox=queue,
        states=_States(None),
        asset_service=assets,
        clock=lambda: _NOW,
    )
    content: tuple[UserContentBlock, ...] = (TextBlock(text="look"), original)

    await app.submit_message(
        _CONTEXT,
        SubmitMessageCommand(
            message_id="message-1",
            role=InboundRole.USER,
            origin="user",
            content=content,
        ),
    )

    assert assets.calls == [(content, (rule,))]
    assert queue.messages[0].content == (TextBlock(text="look"), archived)


async def test_media_error_prevents_queue_write() -> None:
    """媒体准备失败不能产生 Inbox 或 Ready 写入。"""
    queue = _Queue()
    assets = _PreparingAssets(
        error=AdapterError(
            operation="asset.prepare",
            code="media_too_large",
            message="too large",
        )
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=queue,
        states=_States(None),
        asset_service=assets,
    )

    with pytest.raises(ServiceError) as captured:
        await app.submit_message(
            _CONTEXT,
            SubmitMessageCommand(
                message_id="message-1",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="hello"),),
            ),
        )
    assert captured.value.code == "media_too_large"

    assert queue.messages == []


async def test_list_pending_inputs_returns_minimal_fifo_view() -> None:
    """Pending 查询只公开内容、类型、ID 和接收时间。"""
    queue = _Queue()
    queue.messages.extend(
        (
            InboundMessage(
                message_id="m-1",
                session_id=_CONTEXT.session_id,
                content=(TextBlock(text="first"),),
                origin=MessageOrigin.USER,
                mode="queue",
                created_at=_NOW,
            ),
            InboundMessage(
                message_id="m-2",
                session_id=_CONTEXT.session_id,
                content=(TextBlock(text="second"),),
                origin=MessageOrigin.USER,
                mode="steer",
                created_at=_NOW + timedelta(seconds=1),
            ),
        )
    )
    app = ApplicationService(agents=_Agents(), inbox=queue, states=_States(None))

    result = await app.list_pending_inputs(_CONTEXT)

    assert result == PendingInputsView(
        session_id=_CONTEXT.session_id,
        inputs=(
            PendingInputView(
                message_id="m-1",
                role=InboundRole.USER,
                origin="user",
                content="first",
                blocks=(SessionTextBlockView(text="first"),),
                mode="queue",
                created_at=_NOW,
            ),
            PendingInputView(
                message_id="m-2",
                role=InboundRole.USER,
                origin="user",
                content="second",
                blocks=(SessionTextBlockView(text="second"),),
                mode="steer",
                created_at=_NOW + timedelta(seconds=1),
            ),
        ),
    )


async def test_pending_media_view_hides_payload_and_locator() -> None:
    """Pending 视图只公开媒体类型元数据，不公开 Base64 正文。"""
    queue = _Queue()
    queue.messages.append(
        InboundMessage(
            message_id="media-1",
            session_id=_CONTEXT.session_id,
            content=(
                ImageBlock(
                    source=Base64Source(
                        data="sensitive-base64",
                        media_type="image/png",
                    )
                ),
            ),
            origin=MessageOrigin.USER,
            created_at=_NOW,
        )
    )
    app = ApplicationService(agents=_Agents(), inbox=queue, states=_States(None))

    result = await app.list_pending_inputs(_CONTEXT)

    assert result.inputs[0].content == ""
    assert result.inputs[0].blocks == (
        SessionImageBlockView(
            source_type="base64",
            media_type="image/png",
        ),
    )
    assert result.inputs[0].blocks[0].asset_id is None
    assert "sensitive-base64" not in result.model_dump_json()


async def test_pending_asset_view_exposes_retrievable_asset_id() -> None:
    """Asset 视图公开读取所需 ID，但仍不扩展其他 Source locator。"""
    queue = _Queue()
    queue.messages.append(
        InboundMessage(
            message_id="asset-1",
            session_id=_CONTEXT.session_id,
            content=(
                ImageBlock(
                    source=AssetSource(
                        asset_id="group/M00/image.png",
                        media_type="image/png",
                        filename="image.png",
                    )
                ),
            ),
            origin=MessageOrigin.USER,
            created_at=_NOW,
        )
    )
    app = ApplicationService(agents=_Agents(), inbox=queue, states=_States(None))

    result = await app.list_pending_inputs(_CONTEXT)

    block = result.inputs[0].blocks[0]
    assert isinstance(block, SessionImageBlockView)
    assert block.asset_id == "group/M00/image.png"


async def test_resume_resolves_internal_run_id_from_pending_state() -> None:
    """客户端无需也不能提供 run_id，Application 从最新 PENDING State 获取。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    pending = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="opaque-token",
        now=_NOW,
    )
    queue = _Queue()
    app = ApplicationService(
        agents=_Agents(), inbox=queue, states=_States(pending), clock=lambda: _NOW
    )
    request = ResumeCommand(
        resume_id="resume-1",
        resume_token="opaque-token",
        input={"approved": True},
    )

    status = await app.resume(_CONTEXT, request)

    assert status is ApplicationStatus.PENDING
    assert queue.resumes == [
        ResumeRequest(
            ctx=_CONTEXT,
            resume_id="resume-1",
            run_id="internal-run",
            resume_token="opaque-token",
            input={"approved": True},
        )
    ]


async def test_resume_rejects_stale_token_without_queue_write() -> None:
    """失效 opaque token 不产生 ResumeReady。"""
    queue = _Queue()
    app = ApplicationService(
        agents=_Agents(), inbox=queue, states=_States(None), clock=lambda: _NOW
    )
    request = ResumeCommand(
        resume_id="resume-1",
        resume_token="stale",
    )

    with pytest.raises(ServiceError) as captured:
        await app.resume(_CONTEXT, request)
    assert captured.value.code == "invalid_resume"

    assert queue.resumes == []


async def test_cancel_resolves_internal_run_and_snapshot_is_sanitized() -> None:
    """取消内部解析 run_id；公开快照不包含内部标识。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    control = _SessionControl()
    cancellation_bus = _CancellationBus()
    app = ApplicationService(
        agents=_Agents(session=control),
        inbox=_Queue(),
        states=_States(running),
        cancellation_bus=cancellation_bus,
        clock=lambda: _NOW,
    )
    request = CancelCommand()

    status = await app.cancel(_CONTEXT, request)
    snapshot = await app.snapshot(_CONTEXT)

    assert status is ApplicationStatus.ACCEPTED
    assert control.calls == []
    assert cancellation_bus.commands == [
        BroadcastCancel(
            **_CONTEXT.model_dump(),
            run_id="internal-run",
            requested_at=_NOW,
        )
    ]
    assert snapshot is not None
    assert snapshot.model_dump() == {
        "session_id": "session",
        "status": running.status,
        "result": None,
        "usage": None,
        "timing": None,
    }


async def test_cancel_pending_settles_without_publishing() -> None:
    """PENDING 没有活动 Task，只能在所属 AgentSession 锁内直接收口。"""
    pending = suspend_run(
        start_run(
            AgentState(session_context=_CONTEXT),
            run_id="internal-run",
            output_mode=OutputMode.COMPLETE,
            now=_NOW,
        ),
        tool_call_id="call",
        resume_token="token",
        now=_NOW,
    )
    control = _SessionControl()
    cancellation_bus = _CancellationBus()
    app = ApplicationService(
        agents=_Agents(session=control),
        inbox=_Queue(),
        states=_States(pending),
        cancellation_bus=cancellation_bus,
    )

    status = await app.cancel(_CONTEXT, CancelCommand())

    assert status is ApplicationStatus.CANCELED
    assert control.calls == [(_CONTEXT, "internal-run")]
    assert cancellation_bus.commands == []


async def test_cancel_running_does_not_accept_failed_publish() -> None:
    """RUNNING 取消广播失败时必须向上游暴露服务错误。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    failure = RuntimeError("redis unavailable")
    cancellation_bus = _CancellationBus(error=failure)
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(running),
        cancellation_bus=cancellation_bus,
    )

    with pytest.raises(RuntimeError) as captured:
        await app.cancel(_CONTEXT, CancelCommand())

    assert captured.value is failure
    assert len(cancellation_bus.commands) == 1


async def test_snapshot_includes_sanitized_public_result() -> None:
    """公开终态只暴露回答，不泄露 event/run/trace 或内部 Message 字段。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    ).model_copy(update={"usage_total": Usage(total_tokens=7)})
    succeeded = complete_run(
        running,
        final_message_id="assistant",
        now=_NOW + timedelta(seconds=5),
    )
    message = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="最终回答"),),
        origin=MessageOrigin.MODEL,
        run_id="internal-run",
        model="private-model",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    messages = _Messages((message,))
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(succeeded),
        messages=messages,
    )

    snapshot = await app.snapshot(_CONTEXT)

    assert snapshot is not None
    assert snapshot.result == SessionPublicResult(content="最终回答")
    assert messages.get_calls == [(_CONTEXT, "assistant")]
    assert messages.list_calls == []
    assert snapshot.usage == SessionTokenUsage(total_tokens=7)
    assert snapshot.timing == SessionRunTiming(
        started_at=_NOW,
        ended_at=_NOW + timedelta(seconds=5),
        elapsed_ms=5_000,
        active_ms=5_000,
    )
    serialized = snapshot.model_dump_json()
    assert "internal-run" not in serialized
    assert "internal-trace" not in serialized
    assert "private-model" not in serialized


async def test_snapshot_maps_state_adapter_failure_to_service_unavailable() -> None:
    """同步状态查询不得把存储 adapter 异常泄漏到 HTTP 边界。"""
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(
            None,
            AdapterError(
                operation="state.read",
                code="adapter_timeout",
                message="状态存储暂不可用",
                retryable=True,
            ),
        ),
    )

    with pytest.raises(ServiceError) as captured:
        await app.snapshot(_CONTEXT)

    assert captured.value.kind is ServiceErrorKind.UNAVAILABLE
    assert captured.value.code == "adapter_timeout"


async def test_list_messages_maps_adapter_failure_to_service_unavailable() -> None:
    """同步消息查询将 Redis/SQL 失败统一映射为 ServiceError。"""
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=_Messages(
            (),
            AdapterError(
                operation="message.list",
                code="adapter_error",
                message="消息存储暂不可用",
                retryable=True,
            ),
        ),
    )

    with pytest.raises(ServiceError) as captured:
        await app.list_messages(_CONTEXT)

    assert captured.value.kind is ServiceErrorKind.UNAVAILABLE
    assert captured.value.code == "adapter_error"


async def test_snapshot_ignores_public_result_from_previous_run() -> None:
    """RUNNING 状态不能把历史 Assistant Message 当作当前公开结果。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="current-run",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    old_answer = Message(
        message_id="old-answer",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="过期回答"),),
        origin=MessageOrigin.MODEL,
        run_id="previous-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(running),
        messages=_Messages((old_answer,)),
    )

    snapshot = await app.snapshot(_CONTEXT)

    assert snapshot is not None
    assert snapshot.result is None


async def test_list_messages_uses_default_public_content_policy() -> None:
    """默认保留 thinking/工具身份，但隐藏工具参数与结果原文。"""
    service = Message(
        message_id="system",
        session_id="session",
        role=Role.SYSTEM,
        content=(TextBlock(text="secret prompt"),),
        origin=MessageOrigin.SYSTEM,
        created_at=_NOW,
        visibility=MessageVisibility.MODEL,
    )
    assistant = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(
            ThinkingBlock(thinking="visible reasoning", signature="secret-signature"),
            TextBlock(text="公开回答"),
            ToolCallBlock(
                tool_call_id="call-secret",
                tool_name="internal_tool",
                arguments={"secret": True},
                raw_arguments='{"secret":true}',
                thought_signature="secret-thought-signature",
            ),
        ),
        origin=MessageOrigin.MODEL,
        run_id="internal-run",
        model="private-model",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    tool = Message(
        message_id="tool",
        session_id="session",
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id="call-secret",
                tool_name="internal_tool",
                content="公开工具结果",
                is_error=False,
            ),
        ),
        origin=MessageOrigin.TOOL,
        run_id="internal-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=_Messages((service, assistant, tool)),
    )

    result = await app.list_messages(_CONTEXT)

    assert isinstance(result, SessionMessagesView)
    assert result.model_dump(mode="json") == {
        "session_id": "session",
        "messages": [
            {
                "message_id": "assistant",
                "role": "assistant",
                "origin": "model",
                "content": "公开回答",
                "blocks": [
                    {"type": "thinking", "thinking": "visible reasoning"},
                    {"type": "text", "text": "公开回答"},
                    {
                        "type": "tool_call",
                        "tool_call_id": "call-secret",
                        "tool_name": "internal_tool",
                        "arguments": None,
                        "raw_arguments": None,
                        "truncated": False,
                    },
                ],
                "created_at": "2026-08-03T00:00:00Z",
                "is_error": False,
            },
            {
                "message_id": "tool",
                "role": "tool",
                "origin": "tool",
                "content": "",
                "blocks": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-secret",
                        "tool_name": "internal_tool",
                        "content": None,
                        "is_error": False,
                        "truncated": False,
                    }
                ],
                "created_at": "2026-08-03T00:00:00Z",
                "is_error": False,
            },
        ],
    }
    serialized = result.model_dump_json()
    for secret in (
        "secret-signature",
        "secret-thought-signature",
        '{"secret":true}',
        "公开工具结果",
        "internal-run",
        "private-model",
    ):
        assert secret not in serialized


def test_dynamic_inbound_with_service_origin_remains_public() -> None:
    """origin=service 不得隐藏带 run_id 的 USER/SYSTEM 业务输入。"""
    user = Message(
        message_id="user-service",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="human handoff"),),
        origin="service",
        run_id="run-1",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    system = Message(
        message_id="system-service",
        session_id="session",
        role=Role.SYSTEM,
        content=(TextBlock(text="business notice"),),
        origin="service",
        run_id="run-1",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    base = Message(
        message_id="base-system",
        session_id="session",
        role=Role.SYSTEM,
        content=(TextBlock(text="secret prompt"),),
        origin="system",
        created_at=_NOW,
        visibility=MessageVisibility.MODEL,
    )

    policy = PublicContentPolicy(PublicStreamConfig())
    user_view = present_message(user, policy)
    system_view = present_message(system, policy)

    assert user_view is not None
    assert system_view is not None
    assert (user_view.role, user_view.origin) == ("user", "service")
    assert (system_view.role, system_view.origin) == ("system", "service")
    assert present_message(base, policy) is None


async def test_list_messages_preserves_sanitized_compaction_order() -> None:
    """公开消息沿用存储顺序，只放行脱敏 Compaction Message。"""
    system = Message(
        message_id="system",
        session_id="session",
        role=Role.SYSTEM,
        content=(TextBlock(text="secret prompt"),),
        origin=MessageOrigin.SYSTEM,
        created_at=_NOW,
        visibility=MessageVisibility.MODEL,
    )
    before = Message(
        message_id="before",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="压缩前"),),
        origin=MessageOrigin.MODEL,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    compaction = Message(
        message_id="compaction",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="summarized",
                summary="secret summary",
                compacted_message_ids=("before",),
                next_retained_message_id="after",
            ),
        ),
        origin="compactor",
        model="secret model",
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    injection = Message(
        message_id="injection",
        session_id="session",
        role=Role.USER,
        content=(
            InjectedBlock(
                target_user_message_id="user",
                items=(),
            ),
        ),
        origin=MessageOrigin.CONTEXT_PROVIDER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.MODEL,
    )
    after = Message(
        message_id="after",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="压缩后"),),
        origin=MessageOrigin.MODEL,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    messages = _Messages((system, before, compaction, injection, after))
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=messages,
    )

    result = await app.list_messages(_CONTEXT)

    assert messages.list_calls == [_CONTEXT]
    assert [message.message_id for message in result.messages] == [
        "before",
        "compaction",
        "after",
    ]
    assert result.messages[1].model_dump(mode="json") == {
        "message_id": "compaction",
        "role": "user",
        "origin": "compactor",
        "content": "",
        "blocks": [{"type": "compaction"}],
        "created_at": "2026-08-03T00:00:00Z",
        "is_error": False,
    }
    serialized = result.model_dump_json()
    for secret in (
        "secret prompt",
        "secret summary",
        "secret model",
        "injection",
        "compacted_message_ids",
        "next_retained_message_id",
    ):
        assert secret not in serialized


async def test_list_messages_can_enable_redacted_tool_payloads() -> None:
    """开启后历史与 SSE 共用脱敏和 UTF-8 截断口径。"""
    assistant = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(
            ToolCallBlock(
                tool_call_id="call-1",
                tool_name="search",
                arguments={"token": "real-secret", "query": "weather"},
                raw_arguments='{"token":"real-secret","query":"weather"}',
            ),
        ),
        origin=MessageOrigin.MODEL,
        run_id="run-1",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    tool = Message(
        message_id="tool",
        session_id="session",
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id="call-1",
                tool_name="search",
                content="x" * 300,
            ),
        ),
        origin=MessageOrigin.TOOL,
        run_id="run-1",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=_Messages((assistant, tool)),
        public_stream_config=PublicStreamConfig(
            include_tool_arguments=True,
            include_tool_results=True,
            max_tool_payload_bytes=256,
        ),
    )

    result = await app.list_messages(_CONTEXT)
    dumped = result.model_dump(mode="json")

    call = dumped["messages"][0]["blocks"][0]
    assert call["arguments"] == {"token": "[REDACTED]", "query": "weather"}
    assert "real-secret" not in call["raw_arguments"]
    tool_result = dumped["messages"][1]["blocks"][0]
    assert tool_result["content"] == "x" * 256
    assert tool_result["truncated"] is True


@pytest.mark.parametrize(
    ("block", "expected_type"),
    [
        (
            AudioBlock(
                source=AssetSource(
                    asset_id="audio-asset",
                    media_type="audio/mpeg",
                    filename="demo.mp3",
                )
            ),
            "audio",
        ),
        (
            VideoBlock(
                source=AssetSource(
                    asset_id="video-asset",
                    media_type="video/mp4",
                    filename="demo.mp4",
                )
            ),
            "video",
        ),
        (
            DocumentBlock(
                source=AssetSource(
                    asset_id="document-asset",
                    media_type="application/pdf",
                    filename="demo.pdf",
                )
            ),
            "document",
        ),
    ],
)
async def test_list_messages_exposes_only_safe_media_metadata(
    block: AudioBlock | VideoBlock | DocumentBlock,
    expected_type: str,
) -> None:
    """公开媒体 View 只包含安全元数据，不产生测试专用读取接口。"""
    message = Message(
        message_id="assistant",
        session_id="session",
        role=Role.USER,
        content=(block,),
        origin=MessageOrigin.USER,
        run_id="internal-run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=_Messages((message,)),
    )

    result = await app.list_messages(_CONTEXT)
    dumped = result.model_dump(mode="json")["messages"][0]["blocks"][0]

    assert dumped["type"] == expected_type
    assert dumped["source_type"] == "asset"
    assert dumped["asset_id"] == f"{expected_type}-asset"
    assert "internal-run" not in result.model_dump_json()


async def test_unknown_agent_is_rejected_before_any_adapter_access() -> None:
    """五类公开操作必须在 Redis 读写前拒绝未知 Agent。"""
    queue = _Queue()
    app = ApplicationService(
        agents=_Agents(),
        inbox=queue,
        states=_States(None),
        messages=_Messages(()),
    )
    ctx = _CONTEXT.model_copy(update={"agent_id": "missing"})

    with pytest.raises(ServiceError):
        await app.submit_message(
            ctx,
            SubmitMessageCommand(
                message_id="message",
                role=InboundRole.USER,
                origin="user",
                content=(TextBlock(text="hello"),),
            ),
        )
    with pytest.raises(ServiceError):
        await app.resume(
            ctx,
            ResumeCommand(
                resume_id="resume",
                resume_token="token",
            ),
        )
    with pytest.raises(ServiceError):
        await app.cancel(
            ctx,
            CancelCommand(),
        )
    with pytest.raises(ServiceError):
        await app.snapshot(ctx)
    with pytest.raises(ServiceError):
        await app.list_messages(ctx)
    with pytest.raises(ServiceError):
        await app.open_event_stream(ctx, None)
    assert queue.messages == []
    assert queue.resumes == []


async def test_event_stream_preflight_keeps_subscription_cursor() -> None:
    """Application 解析 cursor 并创建只在连接内推进的独立订阅。"""
    epoch = UUID(int=1)
    cursor = PublicEventCursor(epoch=epoch, stream_id="1-0")
    inspection = StreamInspection(
        exists=True,
        epoch=epoch,
        lifecycle=PublicStreamLifecycle.OPEN,
        first_cursor=cursor,
        last_cursor=cursor,
    )
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.STREAM,
        now=_NOW,
    )
    bus = _EventBus(inspection)
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(running),
        event_bus=bus,
    )

    await app.open_event_stream(_CONTEXT, cursor.render())

    assert bus.inspected == [None, cursor]


async def test_known_stream_session_without_events_can_wait() -> None:
    """已知 STREAM session 即使旧 Stream 清理也能建立无 cursor 订阅。"""
    succeeded = complete_run(
        start_run(
            AgentState(session_context=_CONTEXT),
            run_id="internal-run",
            output_mode=OutputMode.STREAM,
            now=_NOW,
        ),
        final_message_id="assistant",
        now=_NOW,
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(succeeded),
        event_bus=_EventBus(StreamInspection(exists=False)),
    )

    await app.open_event_stream(_CONTEXT, None)


async def test_terminal_stream_closes_when_session_is_idle() -> None:
    """终态且 Inbox 为空时，订阅标记当前连续运行链已经空闲。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.STREAM,
        now=_NOW,
    )
    succeeded = complete_run(running, final_message_id="assistant", now=_NOW)
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(succeeded),
        event_bus=_EventBus(
            _stream_inspection(PublicStreamLifecycle.TERMINAL),
            _terminal_batch(),
        ),
    )

    subscription = await app.open_event_stream(_CONTEXT, None)
    await subscription.read_next()

    assert subscription.should_close is True


async def test_terminal_metadata_does_not_close_before_terminal_record() -> None:
    """Lifecycle 可领先 XREAD 结果，未读到终态事件时必须继续读取。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.STREAM,
        now=_NOW,
    )
    succeeded = complete_run(running, final_message_id="assistant", now=_NOW)
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(succeeded),
        event_bus=_EventBus(
            _stream_inspection(PublicStreamLifecycle.TERMINAL),
            EventReadBatch(lifecycle=PublicStreamLifecycle.TERMINAL),
        ),
    )

    subscription = await app.open_event_stream(_CONTEXT, None)
    await subscription.read_next()

    assert subscription.should_close is False


async def test_old_terminal_record_does_not_close_newer_terminal_run() -> None:
    """旧 run 终态不能与尚未公开投影的新 run 终态组合后提前关流。"""
    next_run_at = _NOW + timedelta(seconds=1)
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="newer-run",
        output_mode=OutputMode.STREAM,
        now=next_run_at,
    )
    succeeded = complete_run(
        running,
        final_message_id="newer-assistant",
        now=next_run_at,
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(succeeded),
        event_bus=_EventBus(
            _stream_inspection(PublicStreamLifecycle.TERMINAL),
            _terminal_batch(),
        ),
    )

    subscription = await app.open_event_stream(_CONTEXT, None)
    await subscription.read_next()

    assert subscription.should_close is False


@pytest.mark.parametrize("next_run", ["queued", "running"])
async def test_terminal_stream_stays_open_for_continuous_run_chain(
    next_run: str,
) -> None:
    """已有排队输入或下一 run 已运行时继续复用当前连接。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.STREAM,
        now=_NOW,
    )
    queue = _Queue()
    state = running
    if next_run == "queued":
        state = complete_run(running, final_message_id="assistant", now=_NOW)
        queue.messages.append(
            InboundMessage(
                message_id="next-message",
                session_id=_CONTEXT.session_id,
                content=(TextBlock(text="下一轮"),),
                origin=MessageOrigin.USER,
                created_at=_NOW,
            )
        )
    app = ApplicationService(
        agents=_Agents(),
        inbox=queue,
        states=_States(state),
        event_bus=_EventBus(
            _stream_inspection(PublicStreamLifecycle.TERMINAL),
            EventReadBatch(lifecycle=PublicStreamLifecycle.TERMINAL),
        ),
    )

    subscription = await app.open_event_stream(_CONTEXT, None)
    await subscription.read_next()

    assert subscription.should_close is False


async def test_suspended_stream_closes_without_queued_input() -> None:
    """PENDING 不占用 SSE，resume 时由客户端重新建立连接。"""
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="internal-run",
        output_mode=OutputMode.STREAM,
        now=_NOW,
    )
    pending = suspend_run(
        running,
        tool_call_id="call-1",
        resume_token="resume-token",
        now=_NOW,
    )
    cursor = PublicEventCursor(epoch=UUID(int=1), stream_id="2-0")
    suspended_record = PublicEventRecord(
        cursor=cursor,
        event=PublicStreamEvent(
            event_id="run-suspended",
            type="run.suspended",
            created_at=_NOW,
            session_id=_CONTEXT.session_id,
            payload=RunSuspendedPayload(resume_token="resume-token"),
        ),
    )
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(pending),
        event_bus=_EventBus(
            _stream_inspection(PublicStreamLifecycle.OPEN),
            EventReadBatch(
                records=(suspended_record,),
                next_cursor=cursor,
                lifecycle=PublicStreamLifecycle.OPEN,
            ),
        ),
    )

    subscription = await app.open_event_stream(_CONTEXT, None)
    await subscription.read_next()

    assert subscription.should_close is True


def _stream_inspection(lifecycle: PublicStreamLifecycle) -> StreamInspection:
    """构造带完整边界的测试 Stream inspection。"""
    cursor = PublicEventCursor(epoch=UUID(int=1), stream_id="1-0")
    return StreamInspection(
        exists=True,
        epoch=cursor.epoch,
        lifecycle=lifecycle,
        first_cursor=cursor,
        last_cursor=cursor,
    )


def _terminal_batch() -> EventReadBatch:
    """构造包含实际 run.completed record 的终态 batch。"""
    cursor = PublicEventCursor(epoch=UUID(int=1), stream_id="2-0")
    record = PublicEventRecord(
        cursor=cursor,
        event=PublicStreamEvent(
            event_id="run-completed",
            type="run.completed",
            created_at=_NOW,
            session_id=_CONTEXT.session_id,
            payload=RunTerminalPayload(
                status="succeeded",
                final_message_id="assistant",
                usage=Usage(),
                timing=RunTiming(
                    started_at=_NOW,
                    ended_at=_NOW,
                    elapsed_ms=0,
                    active_ms=0,
                ),
            ),
        ),
    )
    return EventReadBatch(
        records=(record,),
        next_cursor=cursor,
        lifecycle=PublicStreamLifecycle.TERMINAL,
    )
