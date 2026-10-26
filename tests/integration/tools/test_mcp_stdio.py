"""MCP stdio Tool handler 的真实协议集成测试。"""

import sys
from pathlib import Path

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.tool.mcp import MCPStdioToolHandler


async def test_stdio_handler_calls_real_mcp_server() -> None:
    """Adapter 必须完成 initialize/call_tool 并归一化 TextContent。"""
    server = Path(__file__).with_name("mcp_server.py")
    handler = MCPStdioToolHandler(
        command=sys.executable,
        args=(str(server),),
        tool_name="add",
        timeout_seconds=10,
    )

    output = await handler.invoke(
        ctx=SessionContext(
            app_id="app",
            user_id="user",
            agent_id="agent",
            session_id="session",
        ),
        arguments={"left": 2, "right": 3},
    )

    assert output.content == "5"


async def test_stdio_handler_maps_remote_failure_to_tool_error() -> None:
    """MCP isError 结果必须成为模型可处理的 ToolError。"""
    server = Path(__file__).with_name("mcp_server.py")
    handler = MCPStdioToolHandler(
        command=sys.executable,
        args=(str(server),),
        tool_name="reject",
        timeout_seconds=10,
    )

    with pytest.raises(ToolError) as raised:
        await handler.invoke(
            ctx=SessionContext(
                app_id="app",
                user_id="user",
                agent_id="agent",
                session_id="session",
            ),
            arguments={},
        )

    assert raised.value.code == "mcp_tool_error"
