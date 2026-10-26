"""case 私有的 UDS 传输；业务工具只由 Tau2 执行。"""

import json
import math
import tempfile
import time
from concurrent.futures import Future
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from queue import Empty, Queue
from socketserver import ThreadingMixIn, UnixStreamServer
from threading import Lock, Thread
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError


class ToolCall(BaseModel):
    """保留 Runtime 工具调用身份，直到收到官方结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    requestor: Literal["assistant"] = "assistant"


class ToolMessage(BaseModel):
    """返回给应用工具客户端的官方结果。"""

    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(min_length=1)
    role: Literal["tool"] = "tool"
    content: str | None = None
    requestor: Literal["assistant"] = "assistant"
    error: bool


@dataclass
class _Pending:
    call: ToolCall
    future: Future[ToolMessage]
    deadline: float
    issued: bool = False


class _Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


class CaseToolBridge:
    """排队交接工具请求，等待显式交付的官方结果。"""

    def __init__(self, *, timeout: float) -> None:
        """分配较短的私有 socket 路径，满足 macOS 的 UDS 长度限制。"""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("bridge timeout must be finite and positive")
        self.timeout = timeout
        self._directory = tempfile.TemporaryDirectory(prefix="t2b-", dir="/tmp")
        self.socket_path = Path(self._directory.name) / "bridge.sock"
        self._lock = Lock()
        self._queue: Queue[_Pending] = Queue()
        self._calls: dict[str, _Pending] = {}
        self._closed = False
        self._server: _Server | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        """仅启动一次 HTTP 传输，不创建业务 Environment。"""
        if self._closed or self._server is not None:
            raise ValueError("bridge cannot start twice")
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                bridge._serve(self)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = _Server(str(self.socket_path), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        handler.connection.settimeout(self.timeout)
        try:
            if handler.path != "/tools/execute":
                _respond(handler, 404, {"error": "unknown endpoint"})
                return
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_048_576:
                raise ValueError("invalid request size")
            call = ToolCall.model_validate_json(handler.rfile.read(length))
            pending = self._register(call)
            result = pending.future.result(
                timeout=max(0, pending.deadline - time.monotonic())
            )
            _respond(handler, 200, result.model_dump())
        except (ValueError, ValidationError):
            _respond(handler, 409, {"error": "invalid tool request or identity"})
        except (TimeoutError, RuntimeError):
            _respond(handler, 503, {"error": "tool bridge closed or timed out"})
        except (BrokenPipeError, ConnectionResetError):
            return

    def _register(self, call: ToolCall) -> _Pending:
        with self._lock:
            if self._closed:
                raise RuntimeError("bridge closed")
            previous = self._calls.get(call.id)
            if previous is not None:
                if previous.call != call:
                    raise ValueError("conflicting call identity")
                return previous
            pending = _Pending(call, Future(), time.monotonic() + self.timeout)
            self._calls[call.id] = pending
            self._queue.put(pending)
            return pending

    def next_call(self, timeout: float) -> ToolCall | None:
        """供官方 Orchestrator 领取至多一个待执行调用。

        Args:
            timeout: 等待队列中调用的最长时间，单位为秒。

        Returns:
            有效调用；队列为空、Bridge 已关闭或调用已过期时返回 None。
        """
        try:
            pending = self._queue.get(timeout=timeout)
        except Empty:
            return None
        with self._lock:
            if self._closed or time.monotonic() >= pending.deadline:
                return None
            pending.issued = True
            return pending.call

    def deliver(self, message: ToolMessage) -> None:
        """用官方工具结果完成此前已领取的调用。

        相同结果重复交付时保持幂等，不再次完成调用。

        Args:
            message: 官方 Tau2 Orchestrator 返回的结果。

        Raises:
            ValueError: 调用尚未领取，或重复交付的结果不一致。
        """
        with self._lock:
            pending = self._calls.get(message.id)
            if self._closed or pending is None or not pending.issued:
                raise ValueError("result does not match an issued call")
            if pending.future.done():
                if pending.future.result() != message:
                    raise ValueError("conflicting tool result")
            else:
                pending.future.set_result(message)

    def close(self) -> None:
        """立即拒绝新调用，并允许重试未完成的资源清理。"""
        with self._lock:
            self._closed = True
            for pending in self._calls.values():
                if not pending.future.done():
                    pending.future.set_exception(RuntimeError("bridge closed"))
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise RuntimeError("tool bridge server thread did not stop")
            self._thread = None
        self._directory.cleanup()

    def __enter__(self) -> Self:
        """启动当前作用域拥有的传输服务。"""
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """正常退出或异常退出时均释放传输资源。"""
        self.close()


def _respond(handler: BaseHTTPRequestHandler, status: int, body: object) -> None:
    encoded = json.dumps(body).encode()
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)
    except (BrokenPipeError, ConnectionResetError):
        return
