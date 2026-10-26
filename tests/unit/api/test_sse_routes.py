"""FastAPI 可恢复 SSE frame、错误、心跳与资源限制测试。"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import SessionContext
from agentsystem.config.models import PublicStreamConfig
from agentsystem.outbound.event_bus import (
    EventReadBatch,
)
from agentsystem.outbound.stream_models import (
    PublicEventCursor,
    PublicEventRecord,
    PublicStreamEvent,
    PublicStreamLifecycle,
    RunStartedPayload,
)
from agentsystem.service.application import ApplicationService
from agentsystem.service.streaming import SessionEventSubscription
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from server.app import create_api_app
from server.chat.sse import (
    SseConnectionLimiter,
    SseDiagnostic,
    encode_sse_record,
    stream_sse,
)


def _service_error(kind: ServiceErrorKind, code: str) -> ServiceError:
    return ServiceError(kind=kind, code=code, message="安全错误")


_NOW = datetime(2026, 8, 4, tzinfo=UTC)
_CURSOR = PublicEventCursor(epoch=UUID(int=1), stream_id="1-0")
_RECORD = PublicEventRecord(
    cursor=_CURSOR,
    event=PublicStreamEvent(
        event_id="event-1",
        type="run.started",
        created_at=_NOW,
        session_id="session",
        payload=RunStartedPayload(),
    ),
)
_IDENTITY = {"app_id": "app", "user_id": "user", "agent_id": "agent"}


class _Subscription:
    def __init__(
        self,
        batches: list[EventReadBatch],
        *,
        delay: float = 0,
        should_close: bool = False,
    ) -> None:
        self.batches = batches
        self.delay = delay
        self.should_close = should_close
        self.closed = False

    async def read_next(self) -> EventReadBatch:
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.batches:
            return EventReadBatch(lifecycle=PublicStreamLifecycle.OPEN)
        return self.batches.pop(0)

    async def close(self) -> None:
        self.closed = True


class _Service:
    def __init__(self, subscription: _Subscription) -> None:
        self.subscription = subscription
        self.cursor: str | None = None

    async def open_event_stream(
        self,
        ctx: SessionContext,
        cursor: str | None,
    ) -> SessionEventSubscription:
        assert ctx.session_id == "session"
        self.cursor = cursor
        return cast(SessionEventSubscription, self.subscription)


def _config(**overrides: float | int) -> PublicStreamConfig:
    return PublicStreamConfig.model_validate(
        {
            "heartbeat_seconds": 0.001,
            "max_connection_seconds": 10,
            **overrides,
        }
    )


def _create_test_app(
    service: object,
    config: PublicStreamConfig,
    diagnostics: list[SseDiagnostic] | None = None,
) -> FastAPI:
    return create_api_app(
        cast(ApplicationService, service),
        sse_config=config,
        sse_diagnostic_sink=diagnostics.append if diagnostics is not None else None,
    )


def test_sse_frame_and_route_headers_are_proxy_safe() -> None:
    """frame 使用单行 JSON，响应明确禁用缓存和代理缓冲。"""
    frame = encode_sse_record(_RECORD)
    subscription = _Subscription(
        [
            EventReadBatch(
                records=(_RECORD,),
                next_cursor=_CURSOR,
                lifecycle=PublicStreamLifecycle.TERMINAL,
            )
        ],
        delay=0.001,
    )
    diagnostics: list[SseDiagnostic] = []
    response = TestClient(
        _create_test_app(
            _Service(subscription),
            _config(max_connection_seconds=0.003),
            diagnostics,
        )
    ).get("/v1/sessions/session/events", params=_IDENTITY)

    assert frame.startswith(
        f"id: {_CURSOR.render()}\nevent: run.started\ndata: ".encode()
    )
    assert frame.endswith(b"\n\n")
    assert frame.count(b"\ndata:") == 1
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-length" not in response.headers
    assert subscription.closed is True
    assert len(diagnostics) == 1
    assert diagnostics[0].close_reason == "rotation"
    diagnostic_json = diagnostics[0].model_dump_json()
    assert "user" not in diagnostic_json
    assert "session" not in diagnostic_json
    assert "event-1" not in diagnostic_json


def test_last_event_id_is_forwarded_and_empty_batch_emits_heartbeat() -> None:
    """Last-Event-ID 只从 header 获取，空读取产生 SSE comment。"""
    subscription = _Subscription(
        [
            EventReadBatch(lifecycle=PublicStreamLifecycle.OPEN),
            EventReadBatch(lifecycle=PublicStreamLifecycle.TERMINAL),
        ]
    )
    service = _Service(subscription)
    response = TestClient(
        _create_test_app(service, _config(max_connection_seconds=0.001))
    ).get(
        "/v1/sessions/session/events",
        params=_IDENTITY,
        headers={"Last-Event-ID": _CURSOR.render()},
    )

    assert response.content.startswith(b": ping\n\n")
    assert b"event: stream.rotate\n" in response.content
    assert service.cursor == _CURSOR.render()

    openapi = (
        TestClient(_create_test_app(_Service(subscription), _config()))
        .get("/openapi.json")
        .json()
    )
    content = openapi["paths"]["/v1/sessions/{session_id}/events"]["get"]["responses"][
        "200"
    ]["content"]
    assert "text/event-stream" in content


@pytest.mark.asyncio
async def test_idle_batch_closes_session_stream_after_sending_records() -> None:
    """连续运行链空闲后完整发送当前 batch 并正常 EOF。"""

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    subscription = _Subscription(
        [EventReadBatch(records=(_RECORD,), next_cursor=_CURSOR)],
        should_close=True,
    )
    limiter = SseConnectionLimiter(total=1, per_user=1, per_session=1)
    lease = await limiter.acquire(user_id="user", session_id="session")
    body = stream_sse(
        request=cast(Request, ConnectedRequest()),
        subscription=cast(SessionEventSubscription, subscription),
        lease=lease,
        config=_config(),
        user_id="user",
        session_id="session",
        is_reconnect=False,
    )

    first = await anext(body)

    assert first == encode_sse_record(_RECORD)
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    assert subscription.closed is True


@pytest.mark.asyncio
async def test_non_idle_terminal_batch_keeps_session_stream_open() -> None:
    """已有下一 run 时，同一连接继续读取后续事件。"""

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    subscription = _Subscription(
        [
            EventReadBatch(records=(_RECORD,), next_cursor=_CURSOR),
            EventReadBatch(records=(_RECORD,), next_cursor=_CURSOR),
        ]
    )
    limiter = SseConnectionLimiter(total=1, per_user=1, per_session=1)
    lease = await limiter.acquire(user_id="user", session_id="session")
    body = stream_sse(
        request=cast(Request, ConnectedRequest()),
        subscription=cast(SessionEventSubscription, subscription),
        lease=lease,
        config=_config(),
        user_id="user",
        session_id="session",
        is_reconnect=False,
    )

    assert await anext(body) == encode_sse_record(_RECORD)
    assert await anext(body) == encode_sse_record(_RECORD)
    await cast(AsyncGenerator[bytes], body).aclose()

    assert subscription.closed is True


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            _service_error(ServiceErrorKind.INVALID, "invalid_cursor"),
            400,
            "invalid_cursor",
        ),
        (
            _service_error(ServiceErrorKind.INVALID, "cursor_expired"),
            400,
            "cursor_expired",
        ),
        (
            _service_error(ServiceErrorKind.CONFLICT, "cursor_ahead"),
            409,
            "cursor_ahead",
        ),
        (
            _service_error(ServiceErrorKind.CONFLICT, "snapshot_required"),
            409,
            "snapshot_required",
        ),
    ],
)
def test_cursor_errors_are_returned_before_stream_starts(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """cursor 预检异常必须仍能返回稳定 JSON 状态码。"""

    class ErrorService(_Service):
        async def open_event_stream(
            self,
            ctx: SessionContext,
            cursor: str | None,
        ) -> SessionEventSubscription:
            del ctx, cursor
            raise error

    response = TestClient(
        _create_test_app(
            ErrorService(_Subscription([])),
            _config(),
        )
    ).get("/v1/sessions/session/events", params=_IDENTITY)

    assert response.status_code == status_code
    assert response.json() == {
        "code": code,
        "message": "安全错误",
        "error_id": None,
    }


def test_connection_rotation_emits_non_persistent_control_frame() -> None:
    """连接达到时限后发送无 id 的 stream.rotate 并正常 EOF。"""
    subscription = _Subscription(
        [EventReadBatch(lifecycle=PublicStreamLifecycle.OPEN)],
        delay=0.01,
    )
    response = TestClient(
        _create_test_app(
            _Service(subscription),
            _config(max_connection_seconds=0.001),
        )
    ).get("/v1/sessions/session/events", params=_IDENTITY)

    assert b"event: stream.rotate\n" in response.content
    assert b"id:" not in response.content
    assert subscription.closed is True


@pytest.mark.asyncio
async def test_connection_limiter_enforces_total_user_and_session_limits() -> None:
    """进程内 limiter 原子限制三层计数，并在 lease 关闭后归还。"""
    limiter = SseConnectionLimiter(total=2, per_user=1, per_session=1)
    lease = await limiter.acquire(user_id="user", session_id="session")

    with pytest.raises(ServiceError) as user_limit:
        await limiter.acquire(user_id="user", session_id="other")
    assert user_limit.value.code == "sse_capacity_exceeded"
    with pytest.raises(ServiceError) as session_limit:
        await limiter.acquire(user_id="other", session_id="session")
    assert session_limit.value.code == "sse_capacity_exceeded"

    await lease.close()
    replacement = await limiter.acquire(user_id="user", session_id="session")
    await replacement.close()


@pytest.mark.asyncio
async def test_route_limit_returns_429_and_retry_after() -> None:
    """连接在 preflight 阶段占位，节点总上限映射为可重试 HTTP 429。"""

    class BlockingService(_Service):
        def __init__(self) -> None:
            super().__init__(
                _Subscription(
                    [EventReadBatch(lifecycle=PublicStreamLifecycle.TERMINAL)]
                )
            )
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def open_event_stream(
            self,
            ctx: SessionContext,
            cursor: str | None,
        ) -> SessionEventSubscription:
            del ctx, cursor
            self.started.set()
            await self.release.wait()
            return cast(SessionEventSubscription, self.subscription)

    service = BlockingService()
    app = _create_test_app(
        service,
        _config(max_connections_total=1, max_connection_seconds=0.001),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = asyncio.create_task(
            client.get("/v1/sessions/session/events", params=_IDENTITY)
        )
        await service.started.wait()
        limited = await client.get(
            "/v1/sessions/other/events",
            params=_IDENTITY,
        )
        service.release.set()
        await first

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "1"
