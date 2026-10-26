"""逐工具交接的幂等、停止和真实 UDS 测试。"""

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import httpx
import pytest
from evolution_tasks.tau3_banking._internal.tool_bridge import (
    CaseToolBridge,
    ToolBridgeServer,
)


@dataclass(frozen=True)
class _Call:
    id: str
    name: str
    arguments: dict[str, object]
    requestor: str = "assistant"


@dataclass
class _Result:
    id: str
    content: str
    error: bool = False
    requestor: str = "assistant"
    role: str = "tool"


def test_submit_waits_until_official_result_and_deduplicates() -> None:
    bridge = CaseToolBridge()
    call = _Call("call-1", "write", {"value": 10})
    future = bridge.submit(call)
    assert not future.done()
    assert bridge.submit(call) is future
    assert bridge.next_call() == call
    assert bridge.next_call(timeout=0) is None
    result = _Result("call-1", "written")
    bridge.resolve(result)
    assert future.result(timeout=1) == result
    assert bridge.submit(call).result(timeout=1) == result
    assert bridge.next_call(timeout=0) is None


def test_mismatched_id_arguments_or_results_are_rejected() -> None:
    bridge = CaseToolBridge()
    bridge.submit(_Call("call-1", "write", {"value": 10}))
    with pytest.raises(ValueError, match="身份不一致"):
        bridge.submit(_Call("call-1", "write", {"value": 20}))
    with pytest.raises(ValueError, match="待处理"):
        bridge.resolve(_Result("call-1", "not dispatched"))
    bridge.next_call()
    with pytest.raises(ValueError, match="requestor"):
        bridge.resolve(_Result("call-1", "wrong", requestor="user"))
    with pytest.raises(ValueError, match="待处理"):
        bridge.resolve(_Result("other-id", "wrong"))
    bridge.close()


def test_close_releases_waiters_and_never_dispatches_remaining_calls() -> None:
    bridge = CaseToolBridge()
    future = bridge.submit(_Call("call-1", "write", {}))
    bridge.close()
    bridge.close()
    with pytest.raises(ValueError, match="已关闭"):
        future.result(timeout=1)
    assert bridge.next_call(timeout=0) is None
    with pytest.raises(ValueError, match="已关闭"):
        bridge.submit(_Call("call-2", "write", {}))


def test_uds_request_waits_without_blocking_server_until_result() -> None:
    bridge = CaseToolBridge()
    with TemporaryDirectory(prefix="t3b-", dir="/tmp") as directory:
        socket_path = Path(directory) / "tools.sock"
        with ToolBridgeServer(bridge, socket_path), ThreadPoolExecutor() as pool:

            def request() -> httpx.Response:
                with httpx.Client(
                    transport=httpx.HTTPTransport(uds=str(socket_path)),
                    base_url="http://tau2",
                    timeout=3,
                ) as client:
                    return client.post(
                        "/tools/execute",
                        json={
                            "id": "call-uds",
                            "name": "write",
                            "arguments": {},
                            "requestor": "assistant",
                        },
                    )

            pending = pool.submit(request)
            call = bridge.next_call(timeout=2)
            assert call is not None
            assert not pending.done()
            bridge.resolve(_Result("call-uds", "official error", error=True))
            response = pending.result(timeout=2)
            assert response.status_code == 200
            assert response.json()["content"] == "official error"
            assert response.json()["error"] is True
        assert not socket_path.exists()


def test_close_cleans_socket_when_server_thread_does_not_stop() -> None:
    """后台线程关闭超时也不得泄漏 listener 和 UDS 路径。"""

    class _StuckThread:
        def join(self, *, timeout: float) -> None:
            assert timeout == 0.01

        def is_alive(self) -> bool:
            return True

    with TemporaryDirectory(prefix="t3b-", dir="/tmp") as directory:
        socket_path = Path(directory) / "tools.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        server = ToolBridgeServer(
            CaseToolBridge(),
            socket_path,
            shutdown_timeout_seconds=0.01,
        )
        server._socket = listener
        server._thread = cast(Any, _StuckThread())

        with pytest.raises(TimeoutError, match="关闭超时"):
            server.close()

        assert listener.fileno() == -1
        assert not socket_path.exists()
