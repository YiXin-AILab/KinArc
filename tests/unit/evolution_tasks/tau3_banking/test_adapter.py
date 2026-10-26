"""agentsystem SSE 到 Tau2 半双工消息的严格适配测试。"""

from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from agentsystem.common.errors import AdapterError
from evolution_tasks.tau3_banking._internal.adapter import (
    AgentsystemHttpSession,
    create_http_agent,
    parse_candidate_events,
)
from evolution_tasks.tau3_banking._internal.process import RunnerError
from evolution_tasks.tau3_banking._internal.tool_bridge import CaseToolBridge


def test_session_hands_off_before_candidate_run_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待完整 run 才返回工具会死锁；主线程必须先取到调用。"""
    bridge = CaseToolBridge()
    session = AgentsystemHttpSession(
        base_url="http://unused",
        app_id="a",
        agent_id="a",
        user_id="u",
        session_id="s",
        timeout_seconds=2,
    )
    call = SimpleNamespace(id="call", name="write", arguments={}, requestor="assistant")
    result = SimpleNamespace(id="call", content="official", requestor="assistant")

    def submit(content: str) -> str:
        assert content == "help"
        received = bridge.submit(call).result(timeout=1)
        assert received is result
        return "done"

    monkeypatch.setattr(session, "submit", submit)
    session.start("help")
    try:
        assert session.next_output(bridge) is call
        bridge.resolve(result)
        assert session.next_output(bridge) == "done"
    finally:
        bridge.close()
        session.close()


def test_failed_collector_still_cancels_active_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE reader 已异常退出也必须取消尚在服务端运行的 Candidate。"""
    session = AgentsystemHttpSession(
        base_url="http://unused",
        app_id="a",
        agent_id="a",
        user_id="u",
        session_id="s",
        timeout_seconds=2,
    )
    canceled: list[str] = []
    client = _ReconnectClient()

    def post(url: str, *, json: object) -> _Response:
        canceled.append(url)
        return _Response()

    monkeypatch.setattr(client, "post", post)
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
        lambda **kwargs: client,
    )

    def fail(content: str) -> str:
        session._active.set()
        raise ValueError("bad SSE")

    monkeypatch.setattr(session, "submit", fail)
    session.start("help")
    bridge = CaseToolBridge()
    with pytest.raises(ValueError, match="bad SSE"):
        session.next_output(bridge)
    session.close()
    session.close()
    assert canceled == ["http://unused/v1/sessions/s/cancel"]


def test_http_agent_converts_candidate_failure_to_tau2_agent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run.failed 应生成 Tau2 可收敛为 AGENT_ERROR 的合法中间消息。"""

    class HalfDuplexAgent:
        def __init__(self, *, tools: list[object], domain_policy: str) -> None:
            del tools, domain_policy

    class AssistantMessage:
        def __init__(
            self,
            *,
            role: str,
            content: str | None,
            tool_calls: list[object] | None = None,
        ) -> None:
            self.role = role
            self.content = content
            self.tool_calls = tool_calls
            self.timestamp: str | None = None

    class UserMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class ToolMessage:
        pass

    class MultiToolMessage:
        pass

    class ToolCall:
        pass

    modules = {
        "tau2.agent.base_agent": type(
            "AgentModule", (), {"HalfDuplexAgent": HalfDuplexAgent}
        ),
        "tau2.data_model.message": type(
            "MessageModule",
            (),
            {
                "AssistantMessage": AssistantMessage,
                "MultiToolMessage": MultiToolMessage,
                "ToolCall": ToolCall,
                "ToolMessage": ToolMessage,
                "UserMessage": UserMessage,
            },
        ),
        "tau2.utils.utils": type(
            "UtilsModule", (), {"get_now": staticmethod(lambda: "now")}
        ),
    }
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.importlib.import_module",
        lambda name: modules[name],
    )

    class FailingSession:
        def start(self, content: str) -> None:
            del content
            raise AdapterError(
                operation="tau3.candidate.run",
                code="context_compaction_failed",
                message="Context compaction failed",
            )

    agent = create_http_agent(
        tools=[],
        domain_policy="policy",
        session=cast(Any, FailingSession()),
        bridge=CaseToolBridge(),
    )
    state = cast(Any, agent).get_init_state()

    with pytest.raises(AdapterError, match="Context compaction failed"):
        cast(Any, agent).generate_next_message(UserMessage("help"), state)


def _event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {"type": event_type, "event_id": f"event-{event_type}", "payload": payload}


def test_parse_candidate_events_validates_tools_and_returns_text() -> None:
    """校验完整 Tool 证据，聚合文本保持原顺序。"""
    events = (
        _event("message.block.delta", {"block_type": "text", "delta": "完"}),
        _event(
            "message.block.completed",
            {"block_type": "text", "content": "完成"},
        ),
        _event(
            "tool.started",
            {
                "tool_call_id": "call-1",
                "tool_name": "lookup",
                "arguments": {"account_id": "a-1"},
                "raw_arguments": None,
                "truncated": False,
            },
        ),
        _event(
            "tool.started",
            {
                "tool_call_id": "call-2",
                "tool_name": "update",
                "arguments": None,
                "raw_arguments": '{"enabled":true}',
                "truncated": False,
            },
        ),
        _event(
            "tool.completed",
            {
                "tool_call_id": "call-1",
                "tool_name": "lookup",
                "content": '{"balance":10}',
                "is_error": False,
                "truncated": False,
            },
        ),
        _event(
            "tool.completed",
            {
                "tool_call_id": "call-2",
                "tool_name": "update",
                "content": "denied",
                "is_error": True,
                "truncated": False,
            },
        ),
        _event("message.block.delta", {"block_type": "text", "delta": "好"}),
        _event(
            "message.block.completed",
            {"block_type": "text", "content": "好了"},
        ),
        _event("run.completed", {"status": "succeeded"}),
    )

    turn = parse_candidate_events(events)

    assert turn == "完成好了"


def test_parse_candidate_events_requires_aggregated_text_event() -> None:
    """流式 delta 不得作为独立消息证据或聚合文本的 fallback。"""
    events = (
        _event("message.block.delta", {"block_type": "text", "delta": "partial"}),
        _event("run.completed", {"status": "succeeded"}),
    )

    with pytest.raises(ValueError, match="没有可重放输出"):
        parse_candidate_events(events)


@pytest.mark.parametrize(
    "events",
    [
        (
            _event(
                "tool.started",
                {
                    "tool_call_id": "call-1",
                    "tool_name": "lookup",
                    "arguments": None,
                    "raw_arguments": None,
                    "truncated": False,
                },
            ),
            _event("run.completed", {"status": "succeeded"}),
        ),
        (
            _event(
                "tool.started",
                {
                    "tool_call_id": "call-1",
                    "tool_name": "lookup",
                    "arguments": None,
                    "raw_arguments": "not-json",
                    "truncated": False,
                },
            ),
            _event("run.completed", {"status": "succeeded"}),
        ),
        (
            _event(
                "tool.started",
                {
                    "tool_call_id": "call-1",
                    "tool_name": "lookup",
                    "arguments": {},
                    "truncated": True,
                },
            ),
            _event("run.completed", {"status": "succeeded"}),
        ),
        (
            _event(
                "tool.completed",
                {
                    "tool_call_id": "call-1",
                    "tool_name": "lookup",
                    "content": "x",
                    "is_error": False,
                    "truncated": True,
                },
            ),
            _event("run.completed", {"status": "succeeded"}),
        ),
        (_event("run.failed", {"status": "failed"}),),
        (_event("run.canceled", {"status": "canceled"}),),
    ],
)
def test_parse_candidate_events_fails_closed(
    events: tuple[dict[str, object], ...],
) -> None:
    """缺失、截断、非法 JSON 和非成功终态均不得被猜测修复。"""
    with pytest.raises((AdapterError, ValueError)):
        parse_candidate_events(events)


def test_http_session_records_public_run_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate 失败诊断应由 Evolution 保存，不扩展 Runtime 异常。"""
    client = _ReconnectClient()
    client._streams = [
        _StreamResponse(
            [
                "id: cursor-1",
                "data: "
                + _event_json(
                    "run.failed",
                    {
                        "status": "failed",
                        "error": {
                            "code": "llm_timeout",
                            "message": "模型调用超时",
                            "exception_type": "TimeoutError",
                            "stacktrace": (
                                "Traceback (most recent call last):\n"
                                "TimeoutError: timed out\n"
                            ),
                            "error_id": "error-123",
                        },
                    },
                ),
                "",
            ]
        )
    ]
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
        lambda **_: client,
    )
    session = AgentsystemHttpSession(
        base_url="http://127.0.0.1:32123",
        app_id="tau3",
        agent_id="banking-agent",
        user_id="user-1",
        session_id="session-1",
        timeout_seconds=5,
    )

    with pytest.raises(AdapterError) as raised:
        session.submit("hello")

    assert raised.value.code == "llm_timeout"
    assert raised.value.message == "模型调用超时"
    assert session.failure == RunnerError(
        code="llm_timeout",
        message="模型调用超时",
        exception_type="TimeoutError",
        stacktrace="Traceback (most recent call last):\nTimeoutError: timed out\n",
        error_id="error-123",
    )


def test_http_session_reconnects_from_last_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE 断开后必须携带最后 cursor 继续，不能重复或漏掉证据。"""
    client = _ReconnectClient()
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
        lambda **_: client,
    )
    session = AgentsystemHttpSession(
        base_url="http://127.0.0.1:32123",
        app_id="tau3",
        agent_id="banking-agent",
        user_id="user-1",
        session_id="session-1",
        timeout_seconds=5,
    )

    transcript = session.submit("hello")

    assert transcript == "完成"
    assert client.stream_headers == [{}, {"Last-Event-ID": "cursor-1"}]
    assert [event["type"] for event in session.candidate_events] == [
        "model.started",
        "tool.started",
        "message.block.completed",
        "message.block.completed",
        "tool.completed",
        "run.completed",
    ]
    assert session.candidate_events[3]["payload"] == {
        "block_type": "thinking",
        "content": "内部推理",
    }


def test_http_session_reconnects_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实网络读错误也必须从最后 cursor 恢复。"""
    client = _ReconnectClient()
    first = client._streams[0]
    client._streams[0] = _BrokenStreamResponse(first._lines)
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
        lambda **_: client,
    )
    session = AgentsystemHttpSession(
        base_url="http://127.0.0.1:32123",
        app_id="tau3",
        agent_id="banking-agent",
        user_id="user-1",
        session_id="session-1",
        timeout_seconds=5,
    )

    transcript = session.submit("hello")

    assert transcript == "完成"
    assert client.stream_headers == [{}, {"Last-Event-ID": "cursor-1"}]


def test_http_session_retries_until_public_stream_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher 创建初始 State 后，SSE 首次建连的短暂 409 应重试。"""
    client = _ReconnectClient()
    client._streams.insert(0, _StreamNotReadyResponse())
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
        lambda **_: client,
    )
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.time.sleep", lambda _: None
    )
    session = AgentsystemHttpSession(
        base_url="http://127.0.0.1:32123",
        app_id="tau3",
        agent_id="banking-agent",
        user_id="user-1",
        session_id="session-1",
        timeout_seconds=5,
    )

    transcript = session.submit("hello")

    assert transcript == "完成"
    assert client.stream_headers == [{}, {}, {"Last-Event-ID": "cursor-1"}]


class _Response:
    status_code = 202

    def raise_for_status(self) -> None:
        return None


class _StreamResponse(_Response):
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self) -> "_StreamResponse":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def iter_lines(self) -> Iterable[str]:
        return self._lines


class _StreamNotReadyResponse(_StreamResponse):
    status_code = 409

    def __init__(self) -> None:
        super().__init__([])
        self._is_read = False

    def read(self) -> bytes:
        self._is_read = True
        return b'{"code":"snapshot_required"}'

    def json(self) -> dict[str, str]:
        if not self._is_read:
            raise httpx.ResponseNotRead()
        return {"code": "snapshot_required"}

    def raise_for_status(self) -> None:
        raise AssertionError("短暂的 snapshot_required 不应作为终态错误抛出")


class _BrokenStreamResponse(_StreamResponse):
    def iter_lines(self) -> Iterator[str]:
        yield from self._lines
        raise httpx.ReadError("connection lost")


class _ReconnectClient:
    def __init__(self) -> None:
        self.stream_headers: list[dict[str, str]] = []
        self._streams = [
            _StreamResponse(
                [
                    "id: cursor-model",
                    "data: "
                    + _event_json(
                        "model.started",
                        {"provider_name": "banking", "model": "test-model"},
                    ),
                    "",
                    "id: cursor-1",
                    "data: "
                    + _event_json(
                        "tool.started",
                        {
                            "tool_call_id": "call-1",
                            "tool_name": "lookup",
                            "arguments": {},
                            "truncated": False,
                        },
                    ),
                    "",
                ]
            ),
            _StreamResponse(
                [
                    "id: cursor-started",
                    "data: "
                    + _event_json(
                        "message.block.started",
                        {"block_type": "text"},
                    ),
                    "",
                    "id: cursor-delta",
                    "data: "
                    + _event_json(
                        "message.block.delta",
                        {"block_type": "text", "delta": "完"},
                    ),
                    "",
                    "id: cursor-completed",
                    "data: "
                    + _event_json(
                        "message.block.completed",
                        {"block_type": "text", "content": "完成"},
                    ),
                    "",
                    "id: cursor-thinking",
                    "data: "
                    + _event_json(
                        "message.block.completed",
                        {"block_type": "thinking", "content": "内部推理"},
                    ),
                    "",
                    "id: cursor-2",
                    "data: "
                    + _event_json(
                        "tool.completed",
                        {
                            "tool_call_id": "call-1",
                            "tool_name": "lookup",
                            "content": "ok",
                            "is_error": False,
                            "truncated": False,
                        },
                    ),
                    "",
                    "id: cursor-3",
                    "data: " + _event_json("run.completed", {"status": "succeeded"}),
                    "",
                ]
            ),
        ]

    def __enter__(self) -> "_ReconnectClient":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def post(self, url: str, *, json: object) -> _Response:
        del url, json
        return _Response()

    def stream(self, method: str, url: str, **kwargs: object) -> _StreamResponse:
        del method, url
        headers = kwargs["headers"]
        assert isinstance(headers, dict)
        self.stream_headers.append(headers)
        return self._streams.pop(0)


def _event_json(event_type: str, payload: dict[str, object]) -> str:
    import json

    return json.dumps(_event(event_type, payload))
