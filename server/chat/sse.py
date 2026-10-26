"""FastAPI SSE frame、连接循环与进程内资源限制。"""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from time import monotonic

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import StrictModel
from agentsystem.config.models import PublicStreamConfig
from agentsystem.service.streaming import PublicEventRecord, SessionEventSubscription
from fastapi import Request


class SseDiagnostic(StrictModel):
    """不含原始 scope、cursor 或 payload 的连接诊断。"""

    scope_hash: str
    duration_ms: float
    bytes_sent: int
    reconnected: bool
    close_reason: str


type SseDiagnosticSink = Callable[[SseDiagnostic], None]


class SseConnectionLease:
    """确保连接取消、异常和正常 EOF 都只归还一次计数。"""

    def __init__(
        self,
        limiter: "SseConnectionLimiter",
        user_id: str,
        session_id: str,
    ) -> None:
        self._limiter = limiter
        self._user_id = user_id
        self._session_id = session_id
        self._closed = False

    async def close(self) -> None:
        """幂等归还三层连接计数。"""
        if self._closed:
            return
        self._closed = True
        await self._limiter.release(self._user_id, self._session_id)


class SseConnectionLimiter:
    """单 API 进程内 total/user/session 三层并发连接限制。"""

    def __init__(self, *, total: int, per_user: int, per_session: int) -> None:
        """创建由单 asyncio lock 保护的低成本计数器。"""
        if min(total, per_user, per_session) <= 0:
            raise ValueError("SSE 连接限制必须为正数")
        self._total_limit = total
        self._user_limit = per_user
        self._session_limit = per_session
        self._active_total = 0
        self._active_users: Counter[str] = Counter()
        self._active_sessions: Counter[str] = Counter()
        self._lock = asyncio.Lock()

    async def acquire(self, *, user_id: str, session_id: str) -> SseConnectionLease:
        """原子检查三层上限并占用一个连接名额。"""
        async with self._lock:
            if (
                self._active_total >= self._total_limit
                or self._active_users[user_id] >= self._user_limit
                or self._active_sessions[session_id] >= self._session_limit
            ):
                raise ServiceError(
                    kind=ServiceErrorKind.CAPACITY,
                    code="sse_capacity_exceeded",
                    message="SSE connection limit exceeded",
                )
            self._active_total += 1
            self._active_users[user_id] += 1
            self._active_sessions[session_id] += 1
        return SseConnectionLease(self, user_id, session_id)

    async def release(self, user_id: str, session_id: str) -> None:
        """归还已经成功 acquire 的名额并清除零计数 key。"""
        async with self._lock:
            self._active_total -= 1
            _decrement(self._active_users, user_id)
            _decrement(self._active_sessions, session_id)


def encode_sse_record(record: PublicEventRecord) -> bytes:
    """把持久化公开事件编码为带 cursor 的规范单行 SSE frame。"""
    data = record.event.model_dump_json()
    return (
        f"id: {record.cursor.render()}\nevent: {record.event.type}\ndata: {data}\n\n"
    ).encode()


def encode_sse_comment(text: str) -> bytes:
    """编码不携带 cursor 的安全 SSE comment。"""
    normalized = text.replace("\r", " ").replace("\n", " ")
    return f": {normalized}\n\n".encode()


def encode_sse_rotation() -> bytes:
    """编码不持久化、无 id 的连接轮换控制帧。"""
    data = json.dumps(
        {"schema_version": 1, "reason": "connection_rotation"},
        separators=(",", ":"),
    )
    return f"event: stream.rotate\ndata: {data}\n\n".encode()


async def stream_sse(
    *,
    request: Request,
    subscription: SessionEventSubscription,
    lease: SseConnectionLease,
    config: PublicStreamConfig,
    user_id: str,
    session_id: str,
    is_reconnect: bool,
    diagnostic_sink: SseDiagnosticSink | None = None,
) -> AsyncIterator[bytes]:
    """跨 run 有限读取、心跳、轮换及取消安全清理。"""
    started = monotonic()
    last_heartbeat = started - config.heartbeat_seconds
    bytes_sent = 0
    close_reason = "client_disconnect"
    try:
        while True:
            if await request.is_disconnected():
                return
            batch = await subscription.read_next()
            for record in batch.records:
                frame = encode_sse_record(record)
                bytes_sent += len(frame)
                yield frame
            if subscription.should_close:
                close_reason = "session_idle"
                return
            now = monotonic()
            if now - started >= config.max_connection_seconds:
                frame = encode_sse_rotation()
                bytes_sent += len(frame)
                close_reason = "rotation"
                yield frame
                return
            if not batch.records and now - last_heartbeat >= config.heartbeat_seconds:
                frame = encode_sse_comment("ping")
                bytes_sent += len(frame)
                last_heartbeat = now
                yield frame
    finally:
        await subscription.close()
        await lease.close()
        if diagnostic_sink is not None:
            diagnostic_sink(
                SseDiagnostic(
                    scope_hash=_scope_hash(user_id, session_id),
                    duration_ms=(monotonic() - started) * 1_000,
                    bytes_sent=bytes_sent,
                    reconnected=is_reconnect,
                    close_reason=close_reason,
                )
            )


def _scope_hash(user_id: str, session_id: str) -> str:
    encoded = f"{user_id}\0{session_id}".encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _decrement(counter: Counter[str], key: str) -> None:
    counter[key] -= 1
    if counter[key] <= 0:
        del counter[key]
