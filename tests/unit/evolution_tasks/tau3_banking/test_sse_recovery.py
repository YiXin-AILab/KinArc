"""跨回合 SSE 过期恢复与等待心跳的 HTTP 边界回归。"""

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
from agentsystem.common.errors import AdapterError
from evolution_tasks.tau3_banking._internal.adapter import AgentsystemHttpSession


def _session(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    timeout: float = 60,
) -> AgentsystemHttpSession:
    client_type = httpx.Client
    monkeypatch.setattr(
        "evolution_tasks.tau3_banking._internal.adapter.httpx.Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    return AgentsystemHttpSession(
        base_url="http://candidate.test",
        app_id="test",
        agent_id="banking",
        user_id="user",
        session_id="case",
        timeout_seconds=timeout,
    )


def _frame(cursor: str, kind: str, payload: dict[str, object]) -> str:
    event = {
        "schema_version": 1,
        "event_id": cursor,
        "type": kind,
        "created_at": "2026-09-10T00:00:00Z",
        "session_id": "case",
        "payload": payload,
    }
    return f"id: {cursor}\ndata: {json.dumps(event)}\n\n"


def _user(message_id: str) -> dict[str, object]:
    return {
        "message_id": message_id,
        "role": "user",
        "origin": "user",
        "content": "hello",
        "blocks": [{"type": "text", "text": "hello"}],
        "created_at": "2026-09-10T00:00:00Z",
        "is_error": False,
    }


def _reply(prefix: str, message_id: str, text: str) -> str:
    return (
        _frame(f"{prefix}-run", "run.started", {})
        + _frame(
            f"{prefix}-input",
            "message.started",
            {"mode": "queue", "message": _user(message_id)},
        )
        + _frame(
            f"{prefix}-text",
            "message.block.completed",
            {
                "message_id": f"{prefix}-answer",
                "block_id": "text",
                "index": 0,
                "block_type": "text",
                "content": text,
            },
        )
        + _frame(f"{prefix}-done", "run.completed", {"status": "succeeded"})
    )


@pytest.mark.parametrize("input_saved", [False, True])
def test_expired_terminal_cursor_recovers_same_submission(
    monkeypatch: pytest.MonkeyPatch,
    input_saved: bool,
) -> None:
    """旧流过期时，新消息可尚未领取或已入历史；恢复不能重复 POST/旧回复。"""
    submitted: list[str] = []
    cursors: list[str | None] = []
    snapshots: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.append(json.loads(request.content)["message_id"])
            return httpx.Response(202, json={"message_id": submitted[-1]})
        if request.url.path.endswith("/messages"):
            snapshots.append(request.url.path)
            messages = submitted if input_saved else submitted[:1]
            return httpx.Response(
                200,
                json={
                    "session_id": "case",
                    "messages": [_user(mid) for mid in messages],
                },
            )
        cursors.append(request.headers.get("Last-Event-ID"))
        if len(cursors) == 1:
            body = _reply("old", submitted[0], "old answer")
        elif len(cursors) == 2:
            return httpx.Response(400, json={"code": "cursor_expired"})
        elif len(cursors) == 3:
            # Same-epoch trimming can retain old terminal evidence. Do not accept it.
            body = _reply("old", submitted[0], "old answer")
        elif len(cursors) == 4:
            body = _reply("new", submitted[1], "new answer")
        else:
            pytest.fail("恢复意外循环")
        return httpx.Response(200, text=body)

    session = _session(monkeypatch, handle)
    assert session.submit("first") == "old answer"
    assert session.submit("second") == "new answer"
    assert len(submitted) == 2
    assert len(set(submitted)) == 2
    assert len(snapshots) == 1
    assert cursors == [None, "old-done", None, "old-done"]
    texts = []
    for event in session.candidate_events:
        if event["type"] == "message.block.completed":
            payload = event["payload"]
            assert isinstance(payload, dict)
            texts.append(payload["content"])
    assert texts == ["old answer", "new answer"]


@pytest.mark.parametrize("lose_boundary", [False, True])
def test_cursor_expiry_with_unverified_boundary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    lose_boundary: bool,
) -> None:
    """活跃 run 丢事件或历史被替换，均不得清空游标并掩盖证据缺口。"""
    submitted: list[str] = []
    streams = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal streams
        if request.method == "POST":
            submitted.append(json.loads(request.content)["message_id"])
            return httpx.Response(202)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "session_id": "case",
                    "messages": [_user("unrelated-message")],
                },
            )
        streams += 1
        if streams == 1:
            return httpx.Response(200, text=_reply("old", submitted[0], "old"))
        if streams == 2 and not lose_boundary:
            return httpx.Response(
                200,
                text=_frame(
                    "active",
                    "model.started",
                    {
                        "provider_name": "test",
                        "model": "test",
                    },
                ),
            )
        return httpx.Response(400, json={"code": "cursor_expired"})

    session = _session(monkeypatch, handle)
    session.submit("first")
    with pytest.raises(AdapterError) as raised:
        session.submit("second")
    assert raised.value.code == "cursor_expired"
    assert len(submitted) == 2
    assert streams == (2 if lose_boundary else 3)


def test_recovered_stream_disconnect_keeps_verified_boundary_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新输入边界确认后再次断流，也必须续接工具结果而非重新等待输入。"""
    submitted: list[str] = []
    cursors: list[str | None] = []

    class InterruptedBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield (
                _frame(
                    "new-input",
                    "message.started",
                    {
                        "mode": "queue",
                        "message": _user(submitted[1]),
                    },
                )
                + _frame(
                    "new-tool",
                    "tool.started",
                    {
                        "tool_call_id": "once",
                        "tool_name": "lookup",
                        "arguments": {},
                        "truncated": False,
                    },
                )
            ).encode()
            raise httpx.ReadError("disconnected after input and tool")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.append(json.loads(request.content)["message_id"])
            return httpx.Response(202)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "session_id": "case",
                    "messages": [_user(mid) for mid in submitted],
                },
            )
        cursors.append(request.headers.get("Last-Event-ID"))
        if len(cursors) == 1:
            return httpx.Response(200, text=_reply("old", submitted[0], "old"))
        if len(cursors) == 2:
            return httpx.Response(400, json={"code": "cursor_expired"})
        if len(cursors) == 3:
            return httpx.Response(200, stream=InterruptedBody())
        if len(cursors) == 4:
            return httpx.Response(
                200,
                text=(
                    _frame(
                        "result",
                        "tool.completed",
                        {
                            "tool_call_id": "once",
                            "tool_name": "lookup",
                            "content": "ok",
                            "is_error": False,
                            "truncated": False,
                        },
                    )
                    + _frame("done", "run.completed", {"status": "succeeded"})
                ),
            )
        pytest.fail("已经收到新消息边界后，不应再次等待 message.started")

    session = _session(monkeypatch, handle)
    session.submit("first")
    transcript = session.submit("second")
    assert transcript == ""
    calls = [
        event["payload"]
        for event in session.candidate_events
        if event["type"] == "tool.started"
    ]
    results = [
        event["payload"]
        for event in session.candidate_events
        if event["type"] == "tool.completed"
    ]
    assert len(calls) == len(results) == 1
    assert isinstance(calls[0], dict)
    assert isinstance(results[0], dict)
    assert calls[0]["tool_call_id"] == "once"
    assert results[0]["content"] == "ok"
    assert cursors == [None, "old-done", None, "new-tool"]
    assert len(submitted) == 2


@pytest.mark.parametrize("case_timeout", [5, 60])
def test_sse_read_waits_for_heartbeat_within_case_budget(
    monkeypatch: pytest.MonkeyPatch,
    case_timeout: float,
) -> None:
    """无 token 时应等待服务端心跳，且读超时仍受 case 剩余预算约束。"""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202)
        read_timeout = request.extensions["timeout"]["read"]
        assert min(25, case_timeout - 1) < read_timeout <= min(30, case_timeout)
        return httpx.Response(200, text=_reply("run", "input", "done"))

    assert _session(monkeypatch, handle, timeout=case_timeout).submit("hello") == "done"
