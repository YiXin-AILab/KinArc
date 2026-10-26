"""Candidate 工具请求与官方 Tau2 ToolMessage 的逐次交接。"""

import asyncio
import json
import os
import socket
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import RLock, Thread
from types import TracebackType
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class _CachedResult:
    """缓存调用身份与结果 Future，确保同一调用只进入队列一次。"""

    identity: tuple[str, str, str, str]
    result: Future[object]


class CaseToolBridge:
    """只交接请求与结果；不持有或执行官方 Environment。"""

    def __init__(self) -> None:
        """初始化幂等结果缓存、待执行队列与并发保护锁。"""
        self._results: dict[str, _CachedResult] = {}
        self._lock = RLock()
        self._calls: Queue[object] = Queue()
        self._pending: set[str] = set()
        self._closed = False

    def submit(self, tool_call: object) -> Future[object]:
        """登记请求并等待官方结果，相同请求只排队一次。"""
        identity = _tool_identity(tool_call)
        with self._lock:
            if self._closed:
                raise ValueError("Tool Bridge 已关闭")
            cached = self._results.get(identity[0])
            if cached is not None:
                _require_same_identity(cached.identity, identity)
                return cached.result
            result: Future[object] = Future()
            self._results[identity[0]] = _CachedResult(identity, result)
            self._calls.put(tool_call)
            return result

    def next_call(self, timeout: float = 0.02) -> object | None:
        """由 Tau2 Agent 领取一个尚未执行的调用。"""
        try:
            call = self._calls.get(timeout=timeout)
        except Empty:
            return None
        with self._lock:
            if (
                self._closed
                or self._results[_tool_identity(call)[0]].result.cancelled()
            ):
                return None
            self._pending.add(_tool_identity(call)[0])
        return call

    def resolve(self, result: object) -> None:
        """交付已记录在官方轨迹中的 ToolMessage。"""
        call_id = getattr(result, "id", None)
        with self._lock:
            if self._closed or call_id not in self._pending:
                raise ValueError("Tau2 ToolResult 没有对应的待处理调用")
            if getattr(result, "requestor", None) != "assistant":
                raise ValueError("Tau2 ToolResult requestor 不匹配")
            cached = self._results[call_id]
            self._pending.remove(call_id)
            if cached.result.done():
                raise ValueError("Tool Bridge 请求已超时或取消")
            cached.result.set_result(result)

    def close(self) -> None:
        """幂等关闭入口并唤醒所有等待者，不执行任何剩余调用。"""
        with self._lock:
            self._closed = True
            for cached in self._results.values():
                if not cached.result.done():
                    cached.result.set_exception(ValueError("Tool Bridge 已关闭"))


class _ToolCallRequest(BaseModel):
    """Unix socket 接口接收的 Candidate 工具调用请求。"""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, object]
    requestor: Literal["assistant"] = "assistant"


class ToolBridgeServer:
    """通过 case 私有 Unix socket 暴露 Candidate Tool 执行入口。"""

    def __init__(
        self,
        bridge: CaseToolBridge,
        socket_path: Path,
        *,
        startup_timeout_seconds: float = 5,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        """绑定 Case Bridge 与私有 socket，并校验启停时间预算。"""
        if startup_timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("Tool Bridge timeout 必须为正数")
        self._bridge = bridge
        self._socket_path = socket_path
        self._startup_timeout_seconds = startup_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._socket: socket.socket | None = None
        self._thread: Thread | None = None
        self._server: uvicorn.Server | None = None

    def __enter__(self) -> "ToolBridgeServer":
        """绑定 Unix socket 并等待 Uvicorn 接受请求。"""
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文时停止 Bridge 服务并清理私有 socket。"""
        del exception_type, exception, traceback
        self.close()

    def start(self) -> None:
        """启动只服务一个 case 的后台 HTTP Server。"""
        if self._thread is not None:
            raise RuntimeError("Tool Bridge Server 已启动")
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600)
            listener.listen()
            server = uvicorn.Server(
                uvicorn.Config(
                    _build_app(self._bridge),
                    log_config=None,
                    access_log=False,
                    lifespan="off",
                )
            )
            thread = Thread(
                target=server.run,
                kwargs={"sockets": [listener]},
                daemon=True,
                name="tau2-tool-bridge",
            )
            self._socket = listener
            self._server = server
            self._thread = thread
            thread.start()
            _wait_started(server, thread, self._startup_timeout_seconds)
        except BaseException:
            listener.close()
            self._socket_path.unlink(missing_ok=True)
            self._socket = None
            self._server = None
            self._thread = None
            raise

    def close(self) -> None:
        """有限等待后台 Server 退出并清理私有 socket。"""
        self._bridge.close()
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        shutdown_timed_out = False
        try:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=self._shutdown_timeout_seconds)
                shutdown_timed_out = thread.is_alive()
        finally:
            listener = self._socket
            self._socket = None
            try:
                if listener is not None:
                    listener.close()
            finally:
                self._socket_path.unlink(missing_ok=True)
        if shutdown_timed_out:
            raise TimeoutError("Tool Bridge Server 关闭超时")


def _build_app(bridge: CaseToolBridge) -> FastAPI:
    """创建 Case 私有的工具交接接口，不在 HTTP 层执行业务工具。"""
    app = FastAPI()

    @app.post("/tools/execute")
    async def execute(request: _ToolCallRequest) -> object:
        """登记或复用调用，在有限时间内等待官方工具结果。"""
        try:
            future = bridge.submit(request)
            async with asyncio.timeout(30):
                return await asyncio.wrap_future(future)
        except TimeoutError as error:
            raise HTTPException(
                status_code=504, detail="Tool Bridge 等待结果超时"
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


def _wait_started(
    server: uvicorn.Server,
    thread: Thread,
    timeout_seconds: float,
) -> None:
    """等待后台服务就绪，线程提前退出或超过预算时抛错。"""
    from time import monotonic, sleep

    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError("Tool Bridge Server 启动失败")
        sleep(0.01)
    raise TimeoutError("Tool Bridge Server 启动超时")


def _tool_identity(tool_call: object) -> tuple[str, str, str, str]:
    """验证调用字段并规范化参数，生成幂等身份元组。"""
    tool_call_id = getattr(tool_call, "id", None)
    name = getattr(tool_call, "name", None)
    arguments = getattr(tool_call, "arguments", None)
    requestor = getattr(tool_call, "requestor", None)
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ValueError("ToolCall id 必须是非空字符串")
    if not isinstance(name, str) or not name:
        raise ValueError("ToolCall name 必须是非空字符串")
    if not isinstance(arguments, dict):
        raise ValueError("ToolCall arguments 必须是 object")
    if requestor not in {"assistant", "user"}:
        raise ValueError("ToolCall requestor 非法")
    encoded_arguments = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return tool_call_id, name, encoded_arguments, requestor


def _require_same_identity(
    cached: tuple[str, str, str, str],
    incoming: tuple[str, str, str, str],
) -> None:
    """拒绝相同调用 ID 携带不同工具名、参数或请求方。"""
    if cached != incoming:
        raise ValueError("重复 ToolCall id 的调用身份不一致")


__all__ = ["CaseToolBridge", "ToolBridgeServer"]
