"""Banking Plugin 的 Tau2 官方工具远程后端测试。"""

import inspect
from pathlib import Path

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.tool.context import ToolInvocationContext, _bind_tool_context
from agentsystem.tool.models import ToolOutput

from application.banking_golden_agent.plugins import banking_plugin
from application.banking_golden_agent.services.tau2_tool_client import Tau2ToolClient


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "id": "call-123",
            "role": "tool",
            "content": "official-result",
            "requestor": "assistant",
            "error": False,
        }


class _HttpClient:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    async def __aenter__(self) -> "_HttpClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def post(self, path: str, *, json: dict[str, object]) -> _Response:
        assert path == "/tools/execute"
        self.payload = json
        return _Response()


def _invocation() -> ToolInvocationContext:
    return ToolInvocationContext(
        session_context=SessionContext(
            app_id="tau3",
            agent_id="banking-agent",
            user_id="user-1",
            session_id="session-1",
        ),
        run_id="run-1",
        runtime_trace_id="trace-1",
        tool_call_id="call-123",
        tool_name="lookup",
        turn_index=0,
    )


async def test_client_forwards_runtime_call_identity_and_arguments() -> None:
    """丢失 Runtime tool_call_id 会使官方重放无法命中首次结果。"""
    http_client = _HttpClient()
    client = Tau2ToolClient(
        Path("/tmp/test-tau2.sock"),
        client_factory=lambda: http_client,
    )

    with _bind_tool_context(_invocation()):
        result = await client.execute("lookup", {"user_id": "125"})

    assert result == ToolOutput(content="official-result")
    assert http_client.payload == {
        "id": "call-123",
        "name": "lookup",
        "arguments": {"user_id": "125"},
        "requestor": "assistant",
    }


async def test_remote_handler_never_calls_local_banking_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """评测桥开启后回退本地函数会重新引入第二事实源。"""
    monkeypatch.setenv("TAU2_TOOL_BRIDGE_SOCKET", "/tmp/test-tau2.sock")

    def local_handler(user_id: str) -> ToolOutput:
        raise AssertionError(f"不得访问本地 DB：{user_id}")

    async def execute(
        self: object, name: str, arguments: dict[str, object]
    ) -> ToolOutput:
        return ToolOutput(content=f"{name}:{arguments['user_id']}")

    monkeypatch.setattr(Tau2ToolClient, "execute", execute)
    handler = banking_plugin._tool_handler(  # noqa: SLF001
        local_handler,
        name="get_user_information_by_id",
    )

    with _bind_tool_context(_invocation()):
        pending = handler(user_id="125")
        assert inspect.isawaitable(pending)
        result = await pending

    assert result.content == "get_user_information_by_id:125"
