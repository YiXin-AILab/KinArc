"""Real Unix HTTP transport verifies pending handoff and call identity."""

import asyncio
from pathlib import Path

import httpx
import pytest
from benchmarks.tau2bench.tool_bridge import CaseToolBridge, ToolCall, ToolMessage


@pytest.mark.parametrize("stage", ["shutdown", "server_close", "cleanup"])
def test_failed_close_can_resume_cleanup(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """A failed close rejects calls immediately but can still finish real cleanup."""
    bridge = CaseToolBridge(timeout=1)
    bridge.start()
    server, thread = bridge._server, bridge._thread
    assert server is not None and thread is not None
    pending = bridge._register(ToolCall(id="pending", name="read", arguments={}))
    target = bridge._directory if stage == "cleanup" else server
    original = getattr(target, stage)
    attempts = 0

    def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected cleanup failure")
        original()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(target, stage, fail_once)
            with pytest.raises(OSError, match="injected cleanup failure"):
                bridge.close()
            assert pending.future.done()
            with pytest.raises(RuntimeError, match="bridge closed"):
                bridge._register(ToolCall(id="new", name="read", arguments={}))
            bridge.close()
            assert attempts == 2
            assert server.socket.fileno() == -1
            assert not thread.is_alive()
            assert not bridge.socket_path.parent.exists()
            bridge.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        bridge._directory.cleanup()


@pytest.mark.asyncio
async def test_waits_and_deduplicates(tmp_path: Path) -> None:
    """Both identical HTTP requests wait for one official tool result."""
    # macOS limits UDS paths; tmp_path may exceed that limit.
    with CaseToolBridge(timeout=3) as bridge:
        transport = httpx.AsyncHTTPTransport(uds=str(bridge.socket_path))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://tau2", timeout=5
        ) as client:
            payload = dict(
                id="call-1", name="get_balance", arguments={}, requestor="assistant"
            )
            first = asyncio.create_task(client.post("/tools/execute", json=payload))
            call = await asyncio.to_thread(bridge.next_call, 2)
            assert call is not None and call.id == "call-1"
            assert not first.done()
            second = asyncio.create_task(client.post("/tools/execute", json=payload))
            bridge.deliver(ToolMessage(id="call-1", content="42", error=False))
            responses = await asyncio.gather(first, second)
            assert [r.json()["content"] for r in responses] == ["42", "42"]
            assert bridge.next_call(0.01) is None
            conflict = await client.post(
                "/tools/execute", json=payload | {"name": "other"}
            )
            assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_close_releases_waiter() -> None:
    """Closing a bridge must unblock active HTTP requests."""
    bridge = CaseToolBridge(timeout=3)
    bridge.start()
    transport = httpx.AsyncHTTPTransport(uds=str(bridge.socket_path))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://tau2", timeout=5
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    "/tools/execute",
                    json={
                        "id": "pending",
                        "name": "read",
                        "arguments": {},
                        "requestor": "assistant",
                    },
                )
            )
            assert await asyncio.to_thread(bridge.next_call, 2) is not None
            await asyncio.to_thread(bridge.close)
            assert (await pending).status_code == 503
    finally:
        bridge.close()


def test_unsolicited_result_rejected() -> None:
    """A result cannot complete an unknown call."""
    with CaseToolBridge(timeout=1) as bridge, pytest.raises(ValueError):
        bridge.deliver(ToolMessage(id="unknown", content="bad", error=False))


@pytest.mark.asyncio
async def test_expired_request_cannot_execute_later() -> None:
    """Transport timeout must not leave a business call queued for later execution."""
    with CaseToolBridge(timeout=0.1) as bridge:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(bridge.socket_path)),
            base_url="http://tau2",
            timeout=2,
        ) as client:
            response = await client.post(
                "/tools/execute",
                json={
                    "id": "expired",
                    "name": "write",
                    "arguments": {},
                },
            )
            assert response.status_code == 503
            assert bridge.next_call(0.01) is None
