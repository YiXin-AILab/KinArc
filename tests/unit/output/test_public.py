"""PublicProjector 映射与 Registry 测试。"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from agentsystem.common.errors import RunError
from agentsystem.common.models import FinishReason, JsonValue, SessionContext
from agentsystem.config.models import PublicStreamConfig
from agentsystem.llm.models import Usage
from agentsystem.message.models import (
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ThinkingBlock,
)
from agentsystem.outbound.event_bus import SessionEventBus
from agentsystem.outbound.mapper import to_public_run_result
from agentsystem.outbound.projector import (
    LoggingProjectorFactory,
    LoggingPublicProjector,
)
from agentsystem.outbound.projector_registry import PublicProjectorRegistry
from agentsystem.outbound.public_content import PublicContentPolicy
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
)
from agentsystem.outbound.stream_projector import (
    StreamProjectorFactory,
    StreamPublicProjector,
)
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    AgentEventFactory,
    AgentEventPayload,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunSuspended,
)
from agentsystem.runtime.state import RunTiming, SessionStatus

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_TIMING = RunTiming(started_at=_NOW, ended_at=_NOW, elapsed_ms=0, active_ms=0)
_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


def test_public_content_policy_drops_oversized_structured_arguments() -> None:
    """超限参数只保留截断 JSON，不得同时暴露完整 mapping。"""
    policy = PublicContentPolicy(
        PublicStreamConfig(
            include_tool_arguments=True,
            max_tool_payload_bytes=16,
        )
    )

    arguments, raw_arguments, truncated = policy.tool_arguments({"query": "x" * 100})

    assert arguments is None
    assert raw_arguments is not None
    assert len(raw_arguments.encode("utf-8")) <= 16
    assert truncated is True


def event(payload: AgentEventPayload) -> AgentEventEnvelope:
    """构造固定公开映射输入。"""
    return AgentEventFactory(
        session_id="session",
        run_id="run",
        trace_id="trace",
        clock=lambda: _NOW,
    ).create(payload)


def _user_message() -> Message:
    """构造 RunStarted 所需的固定初始消息。"""
    return Message(
        message_id="user",
        session_id="session",
        role=Role.USER,
        content=(TextBlock(text="question"),),
        origin=MessageOrigin.USER,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )


def _public_event(status: SessionStatus) -> AgentEventEnvelope:
    """构造带敏感公开正文的挂起或终态事件。"""
    if status is SessionStatus.PENDING:
        return event(RunSuspended(resume_token="secret-resume-token"))
    if status is SessionStatus.SUCCEEDED:
        final = Message(
            message_id="assistant",
            session_id="session",
            role=Role.ASSISTANT,
            content=(TextBlock(text="secret-answer"),),
            origin=MessageOrigin.MODEL,
            run_id="run",
            created_at=_NOW,
            visibility=MessageVisibility.ALL,
        )
        return event(RunCompleted(final_message=final, usage=Usage(), timing=_TIMING))
    error = RunError(
        code="failed",
        message="secret-error",
        error_id="opaque-error-id",
    )
    if status is SessionStatus.FAILED:
        return event(RunFailed(error=error, usage=Usage(), timing=_TIMING))
    return event(RunCanceled(error=error, usage=Usage(), timing=_TIMING))


@pytest.mark.parametrize(
    ("status", "event_name"),
    [
        (SessionStatus.PENDING, "RunSuspended"),
        (SessionStatus.SUCCEEDED, "RunCompleted"),
        (SessionStatus.FAILED, "RunFailed"),
        (SessionStatus.CANCELED, "RunCanceled"),
    ],
)
async def test_logging_projector_records_only_safe_terminal_metadata(
    caplog: pytest.LogCaptureFixture,
    status: SessionStatus,
    event_name: str,
) -> None:
    """日志 projector 跳过过程事件且不泄露公开结果正文。"""
    projector = LoggingPublicProjector()

    with caplog.at_level(logging.INFO):
        await projector.project(
            _CONTEXT,
            event(RunStarted(initial_messages=(_user_message(),))),
        )
        await projector.project(_CONTEXT, _public_event(status))

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == f"public_projector {event_name} → {status.name}"
    assert record.__dict__["event_name"] == event_name
    assert record.__dict__["status"] == status.name
    assert record.__dict__["session_id"] == "session"
    assert record.__dict__["run_id"] == "run"
    assert record.__dict__["event_id"]
    rendered = caplog.text
    assert "secret-answer" not in rendered
    assert "secret-error" not in rendered
    assert "secret-resume-token" not in rendered


def test_public_mapper_only_maps_terminal_and_removes_thinking() -> None:
    """公开结果忽略过程事件，并保留可关联日志的安全错误。"""
    assert (
        to_public_run_result(event(RunStarted(initial_messages=(_user_message(),))))
        is None
    )
    final = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(ThinkingBlock(thinking="secret"), TextBlock(text="answer")),
        origin=MessageOrigin.MODEL,
        run_id="run",
        created_at=_NOW,
        stop_reason=FinishReason.STOP,
        visibility=MessageVisibility.ALL,
    )
    completed = to_public_run_result(
        event(RunCompleted(final_message=final, usage=Usage(), timing=_TIMING))
    )
    assert completed is not None
    assert completed.status is SessionStatus.SUCCEEDED
    assert completed.message is not None
    assert completed.message.content == (TextBlock(text="answer"),)

    failed = to_public_run_result(
        event(
            RunFailed(
                error=RunError(
                    code="auth_failed",
                    message="safe message",
                    error_id="opaque-error-id",
                    exception_type="agentsystem.common.errors.RunFailure",
                    stacktrace="Traceback ... private provider detail\n",
                ),
                usage=Usage(),
                timing=_TIMING,
            )
        )
    )
    assert failed is not None
    assert failed.error is not None
    assert failed.error.error_id == "opaque-error-id"
    assert failed.error.exception_type == "agentsystem.common.errors.RunFailure"
    assert failed.error.stacktrace == "Traceback ... private provider detail\n"


def test_public_projector_registry_creates_code_registered_implementation() -> None:
    """输出器注册表按稳定 type 创建实例并冻结注册入口。"""
    received_options: list[Mapping[str, JsonValue]] = []

    class Projector:
        async def project(
            self,
            ctx: SessionContext,
            event: AgentEventEnvelope,
        ) -> None:
            del ctx, event

    def factory(options: Mapping[str, JsonValue]) -> Projector:
        received_options.append(options)
        return Projector()

    registry = PublicProjectorRegistry()
    registry.register("custom", factory)
    with pytest.raises(ValueError, match="重复"):
        registry.register("custom", factory)
    registry.freeze()

    projector = registry.create("custom", {"topic": "events"})

    assert isinstance(projector, Projector)
    assert received_options == [{"topic": "events"}]
    with pytest.raises(KeyError, match="未知"):
        registry.create("missing", {})
    with pytest.raises(RuntimeError, match="冻结"):
        registry.register("late", factory)


def test_builtin_projector_factories_strictly_validate_their_options() -> None:
    """每个内置输出器只接受自己的严格 options。"""
    logging_factory = LoggingProjectorFactory()
    assert isinstance(logging_factory({}), LoggingPublicProjector)
    with pytest.raises(ValueError, match="不支持 options"):
        logging_factory({"unknown": True})

    class EventBus:
        async def append(
            self,
            ctx: SessionContext,
            public_event: PublicStreamEvent,
        ) -> PublicEventRecord:
            del ctx
            return PublicEventRecord(
                cursor=PublicEventCursor(epoch=UUID(int=1), stream_id="1-0"),
                event=public_event,
            )

    stream_factory = StreamProjectorFactory(
        cast(SessionEventBus, EventBus()),
        PublicStreamConfig(),
    )
    assert isinstance(stream_factory({}), StreamPublicProjector)
    with pytest.raises(ValueError, match="不支持 options"):
        stream_factory({"unknown": True})


async def test_stream_projector_appends_only_whitelisted_public_events() -> None:
    """STREAM projector 跳过内部保存事件，并只追加一次脱敏公开事件。"""
    received: list[PublicStreamEvent] = []

    class EventBus:
        async def append(
            self,
            ctx: SessionContext,
            public_event: PublicStreamEvent,
        ) -> PublicEventRecord:
            assert ctx == _CONTEXT
            received.append(public_event)
            return PublicEventRecord(
                cursor=PublicEventCursor(epoch=UUID(int=1), stream_id="1-0"),
                event=public_event,
            )

    projector = StreamPublicProjector(
        cast(SessionEventBus, EventBus()),
        PublicStreamConfig(),
    )
    started = event(RunStarted(initial_messages=(_user_message(),)))
    await projector.project(_CONTEXT, started)
    await projector.project(
        _CONTEXT,
        event(
            RunFailed(
                error=RunError(code="failed", message="safe"),
                usage=Usage(),
                timing=_TIMING,
            )
        ),
    )

    assert len(received) == 2
    assert received[0].type == "run.started"
    assert received[1].type == "run.failed"
