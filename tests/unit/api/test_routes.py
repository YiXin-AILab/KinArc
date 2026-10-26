"""全新 session 级 HTTP API 合约测试。"""

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

import pytest
from agentsystem.asset import AssetContent, AssetRecord
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import InboundRole
from agentsystem.message.models import ImageBlock, TextBlock, UrlSource
from agentsystem.runtime.state import SessionStatus
from agentsystem.service.application import ApplicationService
from agentsystem.service.models import (
    AgentCatalogItem,
    AgentCatalogView,
    ApplicationStatus,
    CancelCommand,
    MessageSubmissionResult,
    PendingInputsView,
    PendingInputView,
    ResumeCommand,
    SessionCompactionBlockView,
    SessionImageBlockView,
    SessionMessagesView,
    SessionMessageView,
    SessionRunTiming,
    SessionSnapshot,
    SessionTextBlockView,
    SessionTokenUsage,
    SubmitMessageCommand,
)
from agentsystem.service.streaming import SessionEventSubscription
from fastapi.testclient import TestClient
from pydantic import ValidationError
from server.app import create_api_app
from server.chat.mapper import to_submit_command
from server.chat.schemas import MessageSubmissionRequest, SessionMessageResponse


def _service_error(kind: ServiceErrorKind, code: str) -> ServiceError:
    return ServiceError(kind=kind, code=code, message="安全错误")


class _Service(ApplicationService):
    def __init__(self) -> None:
        self.submitted: list[tuple[SessionContext, SubmitMessageCommand]] = []
        self.resumed: list[tuple[SessionContext, ResumeCommand]] = []
        self.canceled: list[tuple[SessionContext, CancelCommand]] = []

    def list_agents(self) -> AgentCatalogView:
        return AgentCatalogView(
            agents=(
                AgentCatalogItem(
                    agent_id="a-agent",
                    agent_name="Alpha",
                    description="first agent",
                    output_mode="stream",
                ),
                AgentCatalogItem(
                    agent_id="z-agent",
                    agent_name="Zed",
                    output_mode="complete",
                ),
            )
        )

    async def submit_message(
        self,
        ctx: SessionContext,
        request: SubmitMessageCommand,
    ) -> MessageSubmissionResult:
        assert isinstance(request, SubmitMessageCommand)
        self.submitted.append((ctx, request))
        return MessageSubmissionResult(message_id=request.message_id)

    async def resume(
        self,
        ctx: SessionContext,
        request: ResumeCommand,
    ) -> ApplicationStatus:
        assert isinstance(request, ResumeCommand)
        self.resumed.append((ctx, request))
        return ApplicationStatus.PENDING

    async def cancel(
        self,
        ctx: SessionContext,
        request: CancelCommand,
    ) -> ApplicationStatus:
        assert isinstance(request, CancelCommand)
        self.canceled.append((ctx, request))
        return ApplicationStatus.ACCEPTED

    async def snapshot(self, ctx: SessionContext) -> SessionSnapshot | None:
        return SessionSnapshot(
            session_id=ctx.session_id,
            status=SessionStatus.RUNNING,
        )

    async def list_messages(self, ctx: SessionContext) -> SessionMessagesView:
        return SessionMessagesView(
            session_id=ctx.session_id,
            messages=(
                SessionMessageView(
                    message_id="system-1",
                    role="system",
                    origin="workflow.notice",
                    content="业务通知",
                    blocks=(SessionTextBlockView(text="业务通知"),),
                    created_at=datetime(2026, 8, 3, tzinfo=UTC),
                ),
                SessionMessageView(
                    message_id="assistant-1",
                    role="assistant",
                    origin="model",
                    content="公开回答",
                    blocks=(SessionTextBlockView(text="公开回答"),),
                    created_at=datetime(2026, 8, 3, tzinfo=UTC),
                ),
            ),
        )

    async def list_pending_inputs(self, ctx: SessionContext) -> PendingInputsView:
        return PendingInputsView(
            session_id=ctx.session_id,
            inputs=(
                PendingInputView(
                    message_id="pending-1",
                    role=InboundRole.SYSTEM,
                    origin="workflow.pending",
                    content="later",
                    blocks=(SessionTextBlockView(text="later"),),
                    mode="steer",
                    created_at=datetime(2026, 8, 3, tzinfo=UTC),
                ),
            ),
        )

    async def open_event_stream(
        self,
        ctx: SessionContext,
        cursor: str | None,
    ) -> SessionEventSubscription:
        del ctx, cursor
        raise AssertionError("普通 route 测试不应建立 SSE")

    async def upload_asset(
        self,
        body: AsyncIterable[bytes],
        *,
        declared_media_type: str | None,
        filename: str | None,
    ) -> AssetRecord:
        value = b"".join([chunk async for chunk in body])
        return AssetRecord(
            asset_id="asset-1",
            media_type=declared_media_type or "image/png",
            filename=filename,
            size_bytes=len(value),
            checksum_sha256="a" * 64,
        )

    def open_asset(
        self,
        asset_id: str,
    ) -> AbstractAsyncContextManager[AssetContent]:
        return self._open_asset(asset_id)

    @asynccontextmanager
    async def _open_asset(self, asset_id: str) -> AsyncIterator[AssetContent]:
        del asset_id
        yield AssetContent(media_type="image/png", body=_asset_body())


async def _asset_body() -> AsyncIterator[bytes]:
    yield b"asset"


def test_agent_catalog_exposes_runtime_registered_agents() -> None:
    """Agent 目录必须使用稳定公开字段而非泄漏完整配置。"""
    response = TestClient(create_api_app(_Service())).get("/v1/agents")

    assert response.status_code == 200
    assert response.json() == {
        "agents": [
            {
                "id": "a-agent",
                "name": "Alpha",
                "description": "first agent",
                "output_mode": "stream",
            },
            {
                "id": "z-agent",
                "name": "Zed",
                "description": "",
                "output_mode": "complete",
            },
        ]
    }


def test_agent_catalog_allows_zero_registered_agents() -> None:
    """无可用 Agent 是合法空目录而不是服务错误。"""

    class EmptyService(_Service):
        def list_agents(self) -> AgentCatalogView:
            return AgentCatalogView(agents=())

    response = TestClient(create_api_app(EmptyService())).get("/v1/agents")

    assert response.status_code == 200
    assert response.json() == {"agents": []}


def test_message_submission_returns_only_session_and_message_ids() -> None:
    """公开提交响应不得出现内部 run_id/trace_id。"""
    service = _Service()
    client = TestClient(create_api_app(service))

    response = client.post(
        "/v1/sessions/session-1/messages",
        json={
            "app_id": "app",
            "user_id": "user",
            "agent_id": "agent",
            "message_id": "message-1",
            "content": [{"type": "text", "text": "hello"}],
            "mode": "queue",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "session_id": "session-1",
        "message_id": "message-1",
    }
    assert "run_id" not in response.text
    assert service.submitted[0][0] == SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="session-1",
    )
    assert service.submitted[0][1] == SubmitMessageCommand(
        message_id="message-1",
        role=InboundRole.USER,
        origin="user",
        content=(TextBlock(text="hello"),),
    )


def test_submit_defaults_role_and_origin_to_user() -> None:
    """省略 role/origin 时映射为普通 USER 输入。"""
    request = MessageSubmissionRequest.model_validate(
        {
            "app_id": "app",
            "user_id": "user",
            "agent_id": "agent",
            "message_id": "message-1",
            "content": [{"type": "text", "text": "hello"}],
        }
    )

    command = to_submit_command(request)

    assert command.role is InboundRole.USER
    assert command.origin == "user"


def test_submit_preserves_system_role_and_custom_origin() -> None:
    """显式 SYSTEM role 与自定义 origin 原样进入应用命令。"""
    request = MessageSubmissionRequest.model_validate(
        {
            "app_id": "app",
            "user_id": "customer",
            "agent_id": "agent",
            "message_id": "system-1",
            "role": "system",
            "origin": "order.workflow/refund",
            "content": [{"type": "text", "text": "refunded"}],
            "mode": "steer",
        }
    )

    command = to_submit_command(request)

    assert (command.role, command.origin) == (
        InboundRole.SYSTEM,
        "order.workflow/refund",
    )


def test_submit_rejects_assistant_role() -> None:
    """公开消息提交接口拒绝非 USER/SYSTEM role。"""
    response = TestClient(create_api_app(_Service())).post(
        "/v1/sessions/session-1/messages",
        json={
            "app_id": "app",
            "user_id": "user",
            "agent_id": "agent",
            "message_id": "assistant-1",
            "role": "assistant",
            "content": [{"type": "text", "text": "forged"}],
        },
    )

    assert response.status_code == 422


def test_submit_maps_ordered_text_and_image_blocks() -> None:
    """Server DTO 必须按原顺序形成 Runtime UserContentBlock。"""
    request = MessageSubmissionRequest.model_validate(
        {
            "app_id": "app",
            "user_id": "user",
            "agent_id": "agent",
            "message_id": "message-1",
            "content": [
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": "http://public.example/image.png",
                        "media_type": "image/png",
                    },
                },
            ],
        }
    )

    command = to_submit_command(request)

    assert command.content == (
        TextBlock(text="look"),
        ImageBlock(
            source=UrlSource(
                url="http://public.example/image.png",
                media_type="image/png",
            )
        ),
    )


def test_submit_rejects_scalar_and_unknown_source() -> None:
    """新 API 不兼容旧标量 content，也拒绝未知 Source discriminator。"""
    base = {
        "app_id": "app",
        "user_id": "user",
        "agent_id": "agent",
        "message_id": "message-1",
    }
    with pytest.raises(ValidationError):
        MessageSubmissionRequest.model_validate({**base, "content": "legacy"})
    with pytest.raises(ValidationError):
        MessageSubmissionRequest.model_validate(
            {
                **base,
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "unknown",
                            "media_type": "image/png",
                        },
                    }
                ],
            }
        )


def test_media_size_error_maps_to_stable_http_413() -> None:
    """同步媒体门禁错误只返回稳定 code。"""

    class OversizedService(_Service):
        async def submit_message(
            self,
            ctx: SessionContext,
            request: SubmitMessageCommand,
        ) -> MessageSubmissionResult:
            del ctx, request
            raise ServiceError(
                kind=ServiceErrorKind.PAYLOAD_TOO_LARGE,
                code="media_too_large",
                message="媒体超过单文件上限",
            )

    response = TestClient(create_api_app(OversizedService())).post(
        "/v1/sessions/session/messages",
        json={
            "app_id": "app",
            "user_id": "user",
            "agent_id": "agent",
            "message_id": "message",
            "content": [{"type": "text", "text": "hello"}],
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "code": "media_too_large",
        "message": "媒体超过单文件上限",
        "error_id": None,
    }
    assert "must-not-leak" not in response.text


@pytest.mark.parametrize(
    ("operation", "error", "expected_status", "expected_code"),
    [
        (
            "read",
            _service_error(ServiceErrorKind.INVALID, "invalid_asset_id"),
            400,
            "invalid_asset_id",
        ),
        (
            "read",
            _service_error(ServiceErrorKind.NOT_FOUND, "asset_not_found"),
            404,
            "asset_not_found",
        ),
        (
            "upload",
            _service_error(ServiceErrorKind.PAYLOAD_TOO_LARGE, "media_too_large"),
            413,
            "media_too_large",
        ),
        (
            "upload",
            _service_error(ServiceErrorKind.INVALID, "media_type_mismatch"),
            400,
            "media_type_mismatch",
        ),
        (
            "upload",
            _service_error(ServiceErrorKind.UNAVAILABLE, "asset_store_unavailable"),
            503,
            "asset_store_unavailable",
        ),
        (
            "upload",
            _service_error(ServiceErrorKind.UNAVAILABLE, "asset_upload_failed"),
            503,
            "asset_upload_failed",
        ),
    ],
)
def test_asset_errors_map_to_stable_http_codes(
    operation: str,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """资产接口不得泄露 adapter 异常，也不能把所有失败混成 500。"""

    class FailingService(_Service):
        async def upload_asset(
            self,
            body: AsyncIterable[bytes],
            *,
            declared_media_type: str | None,
            filename: str | None,
        ) -> AssetRecord:
            del body, declared_media_type, filename
            raise error

        def open_asset(
            self,
            asset_id: str,
        ) -> AbstractAsyncContextManager[AssetContent]:
            del asset_id
            raise error

    client = TestClient(create_api_app(FailingService()))
    response = (
        client.get("/v1/assets/opaque-id")
        if operation == "read"
        else client.post(
            "/v1/assets",
            files={"file": ("demo.png", b"png", "image/png")},
        )
    )

    assert response.status_code == expected_status
    assert response.json() == {
        "code": expected_code,
        "message": "安全错误",
        "error_id": None,
    }
    assert "secret" not in response.text


def test_pending_inputs_return_minimal_items() -> None:
    """Pending API 不公开 claim 或消息状态。"""
    response = TestClient(create_api_app(_Service())).get(
        "/v1/sessions/session-1/inputs/pending",
        params={"app_id": "app", "user_id": "user", "agent_id": "agent"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "inputs": [
            {
                "message_id": "pending-1",
                "role": "system",
                "origin": "workflow.pending",
                "content": "later",
                "blocks": [{"type": "text", "text": "later"}],
                "mode": "steer",
                "created_at": "2026-08-03T00:00:00Z",
            }
        ],
    }
    assert "status" not in response.text
    assert "claim" not in response.text


def test_resume_returns_session_level_status_and_rejects_extra_run_id() -> None:
    """Resume 使用 opaque token，外部请求/响应均不接受 run_id。"""
    service = _Service()
    client = TestClient(create_api_app(service))
    payload = {
        "app_id": "app",
        "user_id": "user",
        "agent_id": "agent",
        "resume_id": "resume-1",
        "resume_token": "token-1",
        "input": {"approved": True},
    }

    response = client.post("/v1/sessions/session-1/resume", json=payload)
    invalid = client.post(
        "/v1/sessions/session-1/resume",
        json={**payload, "run_id": "must-not-be-public"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "session_id": "session-1",
        "status": "pending",
    }
    assert "run_id" not in response.text
    assert invalid.status_code == 422


def test_openapi_submission_response_schema_has_no_run_id() -> None:
    """OpenAPI 本身也不能向客户端宣告内部 run 标识。"""
    client = TestClient(create_api_app(_Service()))

    document = client.get("/openapi.json").json()
    schema = document["components"]["schemas"]["SessionStatusResponse"]

    assert set(schema["properties"]) == {"session_id", "status"}


def test_cancel_and_snapshot_never_expose_internal_run_fields() -> None:
    """取消和查询同样只使用 session 级公开协议。"""
    client = TestClient(create_api_app(_Service()))
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}

    canceled = client.post(
        "/v1/sessions/session-1/cancel",
        json=identity,
    )
    snapshot = client.get(
        "/v1/sessions/session-1",
        params=identity,
    )

    assert canceled.status_code == 202
    assert canceled.json() == {"session_id": "session-1", "status": "accepted"}
    assert snapshot.status_code == 200
    assert snapshot.json() == {"session_id": "session-1", "status": "running"}
    assert "run_id" not in canceled.text + snapshot.text
    assert "trace_id" not in canceled.text + snapshot.text

    rejected_reason = client.post(
        "/v1/sessions/session-1/cancel",
        json={**identity, "reason": "must-not-be-accepted"},
    )
    assert rejected_reason.status_code == 422


def test_message_list_uses_sanitized_public_schema() -> None:
    """消息查询响应只声明公开消息字段。"""
    client = TestClient(create_api_app(_Service()))
    identity = {"app_id": "app", "user_id": "user", "agent_id": "agent"}

    response = client.get(
        "/v1/sessions/session-1/messages",
        params=identity,
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "messages": [
            {
                "message_id": "system-1",
                "role": "system",
                "origin": "workflow.notice",
                "content": "业务通知",
                "blocks": [{"type": "text", "text": "业务通知"}],
                "created_at": "2026-08-03T00:00:00Z",
                "is_error": False,
            },
            {
                "message_id": "assistant-1",
                "role": "assistant",
                "origin": "model",
                "content": "公开回答",
                "blocks": [{"type": "text", "text": "公开回答"}],
                "created_at": "2026-08-03T00:00:00Z",
                "is_error": False,
            },
        ],
    }
    for internal_name in ("run_id", "trace_id", "signature"):
        assert internal_name not in response.text


def test_message_list_returns_compaction_in_existing_message_schema() -> None:
    """Compaction 使用现有 messages 列表和脱敏 Block，不增加顶层协议。"""

    class CompactionService(_Service):
        async def list_messages(self, ctx: SessionContext) -> SessionMessagesView:
            return SessionMessagesView(
                session_id=ctx.session_id,
                messages=(
                    SessionMessageView(
                        message_id="compaction",
                        role="user",
                        origin="compactor",
                        content="",
                        blocks=(SessionCompactionBlockView(),),
                        created_at=datetime(2026, 8, 3, tzinfo=UTC),
                    ),
                ),
            )

    response = TestClient(create_api_app(CompactionService())).get(
        "/v1/sessions/session-1/messages",
        params={"app_id": "app", "user_id": "user", "agent_id": "agent"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "messages": [
            {
                "message_id": "compaction",
                "role": "user",
                "origin": "compactor",
                "content": "",
                "blocks": [{"type": "compaction"}],
                "created_at": "2026-08-03T00:00:00Z",
                "is_error": False,
            }
        ],
    }


def test_public_message_schemas_reject_service_role() -> None:
    """公开 Service 与 HTTP DTO 都不得重新引入 SERVICE role。"""
    payload = {
        "message_id": "compaction",
        "role": "service",
        "origin": "compactor",
        "content": "",
        "blocks": ({"type": "compaction"},),
        "created_at": datetime(2026, 8, 3, tzinfo=UTC),
    }

    with pytest.raises(ValidationError):
        SessionMessageView.model_validate(payload)
    with pytest.raises(ValidationError):
        SessionMessageResponse.model_validate(payload)


def test_message_list_exposes_asset_id_only_for_asset_media() -> None:
    """Asset 媒体返回回读 ID，非 Asset locator 仍由应用投影层隐藏。"""

    class MediaService(_Service):
        async def list_messages(self, ctx: SessionContext) -> SessionMessagesView:
            return SessionMessagesView(
                session_id=ctx.session_id,
                messages=(
                    SessionMessageView(
                        message_id="user-asset",
                        role="user",
                        origin="user",
                        content="",
                        blocks=(
                            SessionImageBlockView(
                                source_type="asset",
                                media_type="image/png",
                                filename="image.png",
                                asset_id="group/M00/image.png",
                            ),
                        ),
                        created_at=datetime(2026, 8, 3, tzinfo=UTC),
                    ),
                ),
            )

    response = TestClient(create_api_app(MediaService())).get(
        "/v1/sessions/session-1/messages",
        params={"app_id": "app", "user_id": "user", "agent_id": "agent"},
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["blocks"][0] == {
        "type": "image",
        "source_type": "asset",
        "media_type": "image/png",
        "filename": "image.png",
        "asset_id": "group/M00/image.png",
    }


def test_message_block_openapi_declares_only_configurable_public_tool_fields() -> None:
    """公开 ToolCall schema 不声明签名或解析错误等内部字段。"""
    document = TestClient(create_api_app(_Service())).get("/openapi.json").json()
    schemas = document["components"]["schemas"]

    assert set(schemas["SessionToolCallBlockResponse"]["properties"]) == {
        "type",
        "tool_call_id",
        "tool_name",
        "arguments",
        "raw_arguments",
        "truncated",
    }
    assert set(schemas["SessionThinkingBlockResponse"]["properties"]) == {
        "type",
        "thinking",
    }


def test_unknown_agent_maps_to_stable_http_404() -> None:
    """领域层未知 Agent 异常映射为稳定公开协议。"""

    class MissingAgentService(_Service):
        async def submit_message(
            self,
            ctx: SessionContext,
            request: SubmitMessageCommand,
        ) -> MessageSubmissionResult:
            del ctx, request
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="agent_not_found",
                message="Agent 不存在",
            )

    response = TestClient(create_api_app(MissingAgentService())).post(
        "/v1/sessions/session/messages",
        json={
            "app_id": "app",
            "user_id": "user",
            "agent_id": "missing",
            "message_id": "message",
            "content": [{"type": "text", "text": "hello"}],
        },
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "agent_not_found",
        "message": "Agent 不存在",
        "error_id": None,
    }


def test_unexpected_http_error_is_safe_and_logs_one_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未知程序异常只公开 error_id，并在 HTTP 所有权边界保存完整栈。"""

    class BrokenService(_Service):
        async def submit_message(
            self,
            ctx: SessionContext,
            request: SubmitMessageCommand,
        ) -> MessageSubmissionResult:
            del ctx, request
            raise RuntimeError("secret response body")

    client = TestClient(
        create_api_app(BrokenService()),
        raise_server_exceptions=False,
    )
    with caplog.at_level("ERROR"):
        response = client.post(
            "/v1/sessions/session/messages",
            json={
                "app_id": "app",
                "user_id": "user",
                "agent_id": "agent",
                "message_id": "message",
                "content": [{"type": "text", "text": "hello"}],
            },
        )

    payload = response.json()
    assert response.status_code == 500
    assert payload["code"] == "internal_error"
    assert payload["message"] == "服务内部错误"
    assert payload["error_id"]
    assert "secret response body" not in response.text
    assert caplog.text.count("Traceback (most recent call last)") == 1
    assert f"error_id={payload['error_id']}" in caplog.text


def test_terminal_snapshot_maps_usage_and_dual_timing_without_internal_ids() -> None:
    """HTTP 终态快照公开 token 与双口径耗时，但不增加 run/trace 标识。"""

    class TerminalService(_Service):
        async def snapshot(self, ctx: SessionContext) -> SessionSnapshot | None:
            return SessionSnapshot(
                session_id=ctx.session_id,
                status=SessionStatus.SUCCEEDED,
                usage=SessionTokenUsage(total_tokens=9),
                timing=SessionRunTiming(
                    started_at=datetime(2026, 8, 3, tzinfo=UTC),
                    ended_at=datetime(2026, 8, 3, 0, 1, tzinfo=UTC),
                    elapsed_ms=60_000,
                    active_ms=5_000,
                ),
            )

    response = TestClient(create_api_app(TerminalService())).get(
        "/v1/sessions/session-1",
        params={"app_id": "app", "user_id": "user", "agent_id": "agent"},
    )

    assert response.status_code == 200
    assert response.json()["usage"]["total_tokens"] == 9
    assert response.json()["timing"]["elapsed_ms"] == 60_000
    assert response.json()["timing"]["active_ms"] == 5_000
    assert "run_id" not in response.text
    assert "trace_id" not in response.text


def test_http_session_status_preserves_runtime_wire_values() -> None:
    """Server 自有状态枚举不得改变现有公开字符串。"""
    from server.chat.schemas import SessionStatus as HttpSessionStatus

    assert {item.value for item in HttpSessionStatus} == {
        "idle",
        "running",
        "pending",
        "succeeded",
        "failed",
        "canceled",
    }


@pytest.mark.parametrize("field", ["app_id", "user_id", "agent_id", "session_id"])
def test_http_rejects_trailing_space_identity_as_client_error(field: str) -> None:
    """新增身份限制应返回客户端错误，不能产生 500。"""
    values = dict(app_id="app", user_id="user", agent_id="agent", session_id="session")
    values[field] += " "
    client = TestClient(create_api_app(_Service()), raise_server_exceptions=False)
    session_id = values.pop("session_id")
    response = client.get(f"/v1/sessions/{session_id}", params=values)
    assert response.status_code == 422
