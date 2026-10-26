"""管理临时 Runtime HTTP/SSE 会话，不保存私有评测产物。"""

import json
import time
from concurrent.futures import Future
from contextlib import suppress
from threading import Event, Thread
from uuid import uuid4

import httpx
from agentsystem.llm.models import Usage

from benchmarks.tau2bench.tool_bridge import CaseToolBridge, ToolCall


class HttpSession:
    """单次尝试的所有用户轮次共用一个 Runtime session。"""

    def __init__(
        self,
        base_url: str,
        session_id: str,
        *,
        agent_id: str,
        timeout: float,
        http_timeout: float,
        client: httpx.Client | None = None,
        cancel: Event | None = None,
    ) -> None:
        """为每次读取和整段对话设置有限截止时间。"""
        self.url = f"{base_url}/v1/sessions/{session_id}"
        self.identity = {
            "app_id": "tau2-native",
            "user_id": session_id,
            "agent_id": agent_id,
        }
        self.deadline = time.monotonic() + timeout
        self.http_timeout = http_timeout
        self.client = client or httpx.Client(timeout=http_timeout)
        self._owns_client = client is None
        self.cancel = cancel or Event()
        self._closed = Event()
        self._active = Event()
        self._cursor: str | None = None
        self._thread: Thread | None = None
        self._output: Future[str] | None = None
        self.usage: Usage | None = None
        self.models: set[str] = set()
        self.compacted = False

    def start(self, text: str) -> None:
        """后台读取 SSE，同时由 Tau2 工作线程执行工具。"""
        if self._thread is not None:
            self._thread.join(timeout=self.http_timeout + 1)
            if self._thread.is_alive():
                raise RuntimeError("previous Runtime turn still active")
        output: Future[str] = Future()
        self._output = output

        def collect() -> None:
            try:
                output.set_result(self.submit(text))
            except Exception as error:
                output.set_exception(error)

        self._thread = Thread(target=collect, name="tau2-runtime-sse", daemon=True)
        self._thread.start()

    def next_output(self, bridge: CaseToolBridge) -> ToolCall | str:
        """返回待交接的官方工具调用或已完成的助手文本。"""
        if self._output is None:
            raise RuntimeError("no active Runtime turn")
        while self.remaining() > 0:
            if self._output.done():
                return self._output.result()
            call = bridge.next_call(0.05)
            if call is not None:
                if self._output.done():
                    return self._output.result()
                return call
        raise TimeoutError("Runtime case timed out")

    def remaining(self) -> float:
        """返回剩余时间预算，并传播当前尝试的停止状态。"""
        if self.cancel.is_set() or self._closed.is_set():
            raise RuntimeError("Runtime session stopped")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Runtime case timed out")
        return remaining

    def submit(self, text: str) -> str:
        """提交一轮用户输入，只聚合已完成的文本块。"""
        if not text.strip():
            raise ValueError("user message must contain text")
        self.remaining()
        self.usage = None
        self.models.clear()
        self.compacted = False
        self._active.set()
        try:
            response = self.client.post(
                self.url + "/messages",
                json={
                    **self.identity,
                    "message_id": uuid4().hex,
                    "content": [{"type": "text", "text": text}],
                },
                timeout=min(self.http_timeout, self.remaining()),
            )
            response.raise_for_status()
            return self._read_turn()
        finally:
            self._active.clear()

    def _read_turn(self) -> str:
        parts: list[str] = []
        while self.remaining() > 0:
            headers = {"Last-Event-ID": self._cursor} if self._cursor else {}
            try:
                with self.client.stream(
                    "GET",
                    self.url + "/events",
                    params=self.identity,
                    headers=headers,
                    timeout=min(self.http_timeout, self.remaining()),
                ) as response:
                    # 输入被接受时，首次 run 的 STREAM 投影可能尚未建立。
                    # 已建立的 cursor 不得丢弃后重新读取，以免遗漏事件。
                    if self._cursor is None and response.status_code in {404, 409}:
                        response.read()
                        if response.json().get("code") in {
                            "session_not_found",
                            "snapshot_required",
                        }:
                            self._closed.wait(min(0.05, self.remaining()))
                            continue
                    response.raise_for_status()
                    if self._consume(response, parts):
                        return "".join(parts)
            except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                self.remaining()
            self._closed.wait(min(0.05, self.remaining()))
        raise TimeoutError("Runtime case timed out")

    def _consume(self, response: httpx.Response, parts: list[str]) -> bool:
        event_id: str | None = None
        data: list[str] = []
        for line in response.iter_lines():
            self.remaining()
            if line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line:
                if data and event_id != self._cursor:
                    complete = self._event(json.loads("\n".join(data)), parts)
                    self._cursor = event_id
                    if complete:
                        return True
                data = []
                event_id = None
        return False

    def _event(self, event: dict[str, object], parts: list[str]) -> bool:
        kind = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid Runtime event payload")
        if kind == "model.started" and isinstance(payload.get("model"), str):
            self.models.add(payload["model"])
        if kind == "context.compaction.completed":
            self.compacted = True
        if kind in {"run.completed", "run.failed", "run.canceled"}:
            raw_usage = payload.get("usage")
            self.usage = (
                Usage.model_validate(raw_usage) if raw_usage is not None else None
            )
        if kind == "message.block.completed" and payload.get("block_type") == "text":
            content = payload.get("content")
            if not isinstance(content, str):
                raise ValueError("invalid Runtime text block")
            parts.append(content)
        if kind in {"run.failed", "run.canceled", "run.suspended"}:
            raise RuntimeError(f"Runtime {kind}")
        if kind == "run.completed":
            if not "".join(parts).strip():
                raise RuntimeError("Runtime completed without assistant text")
            return True
        return False

    def request_stop(self) -> None:
        """关闭 Runtime 前先请求取消，不等待 SSE 读取结束。"""
        if self._closed.is_set():
            return
        self._closed.set()
        if self._active.is_set():
            with suppress(httpx.HTTPError):
                self.client.post(self.url + "/cancel", json=self.identity, timeout=1)

    def close(self, *, timeout: float | None = None) -> None:
        """每次都等待读取线程退出，并释放持有的 HTTP 资源。"""
        try:
            self.request_stop()
            if self._thread is not None:
                self._thread.join(
                    timeout=self.http_timeout + 1 if timeout is None else timeout
                )
                if self._thread.is_alive():
                    raise RuntimeError("Runtime SSE reader did not stop")
        finally:
            if self._owns_client:
                self.client.close()
