"""HTTP/SSE consumption is transient and preserves conversation identity."""

import json
from threading import Event, Thread

import httpx
import pytest
from benchmarks.tau2bench.session import HttpSession


def test_failed_close_can_finish_and_releases_client() -> None:
    """A reader still exiting must not make all subsequent closes no-ops."""
    session = HttpSession(
        "http://runtime", "s", agent_id="test", timeout=30, http_timeout=0.01
    )
    release = Event()
    reader = Thread(target=lambda: release.wait(5), daemon=True)
    session._thread = reader
    reader.start()
    try:
        with pytest.raises(RuntimeError, match="reader did not stop"):
            session.close()
        assert session.client.is_closed
        release.set()
        session.close()
        assert not reader.is_alive()
    finally:
        release.set()
        reader.join(timeout=2)
        session.client.close()


def test_session_reconnects_and_reuses_identity() -> None:
    """SSE rotation resumes after the last committed cursor without duplicate text."""
    posts: list[dict[str, object]] = []
    cursors: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(202, json={})
        cursors.append(request.headers.get("Last-Event-ID"))
        if len(cursors) == 1:
            return httpx.Response(
                200,
                text=(
                    'id: 1-0\ndata: {"type":"message.block.completed",'
                    '"payload":{"block_type":"text","content":"hello"}}\n\n'
                ),
            )
        return httpx.Response(
            200, text='id: 2-0\ndata: {"type":"run.completed","payload":{}}\n\n'
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        session = HttpSession(
            "http://runtime",
            "session-1",
            agent_id="banking-golden-agent",
            timeout=2,
            http_timeout=1,
            client=client,
        )
        assert session.submit("user") == "hello"
        assert cursors == [None, "1-0"]
        assert posts[0]["agent_id"] == "banking-golden-agent"
        session.close()


def test_terminal_usage_replaced_each_turn() -> None:
    """Reconnects do not double-count and a new run replaces the prior total."""
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.method == "POST":
            return httpx.Response(202, json={})
        count += 1
        events = [
            {
                "type": "message.block.completed",
                "payload": {"block_type": "text", "content": "ok"},
            },
            {
                "type": "run.completed",
                "payload": {
                    "usage": {
                        "input_tokens": count * 10,
                        "output_tokens": 5,
                        "total_tokens": count * 10 + 5,
                        "accounting": "exact",
                    }
                },
            },
        ]
        return httpx.Response(
            200,
            text="".join(
                f"id: {count}-{i}\ndata: {json.dumps(event)}\n\n"
                for i, event in enumerate(events)
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        session = HttpSession(
            "http://runtime",
            "s",
            agent_id="banking-golden-agent",
            timeout=2,
            http_timeout=1,
            client=client,
        )
        for expected in (10, 20):
            assert session.submit("hello") == "ok"
            assert session.usage is not None
            assert session.usage.input_tokens == expected
        session.close()


def test_runtime_failure_is_not_an_assistant_reply() -> None:
    """Do not turn Runtime failures into normal text seen by the simulator."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={})
        return httpx.Response(
            200, text='id: 1-0\ndata: {"type":"run.failed","payload":{}}\n\n'
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        session = HttpSession(
            "http://runtime",
            "s",
            agent_id="banking-golden-agent",
            timeout=2,
            http_timeout=1,
            client=client,
        )
        with pytest.raises(RuntimeError, match="run.failed"):
            session.submit("user")
        session.close()


def test_initial_stream_waits_for_run_projection() -> None:
    """An accepted input can precede creation of its first public stream."""
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "POST":
            return httpx.Response(202, json={})
        attempts += 1
        if attempts == 1:
            return httpx.Response(409, json={"code": "snapshot_required"})
        return httpx.Response(
            200,
            text=(
                'id: 1-0\ndata: {"type":"message.block.completed",'
                '"payload":{"block_type":"text","content":"ok"}}\n\n'
                'id: 2-0\ndata: {"type":"run.completed","payload":{}}\n\n'
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        session = HttpSession(
            "http://runtime",
            "s",
            agent_id="banking-golden-agent",
            timeout=2,
            http_timeout=1,
            client=client,
        )
        assert session.submit("user") == "ok"
        assert attempts == 2
        session.close()


def test_lost_stream_after_cursor_fails() -> None:
    """A lost existing stream must not silently restart or replay text."""
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            return httpx.Response(202, json={})
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                text=(
                    'id: 1-0\ndata: {"type":"message.block.completed",'
                    '"payload":{"block_type":"text","content":"partial"}}\n\n'
                ),
            )
        return httpx.Response(409, json={"code": "snapshot_required"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        session = HttpSession(
            "http://runtime",
            "s",
            agent_id="banking-golden-agent",
            timeout=2,
            http_timeout=1,
            client=client,
        )
        with pytest.raises(httpx.HTTPStatusError):
            session.submit("user")
        assert calls == 2
        session.close()
