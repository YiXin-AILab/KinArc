"""验证本机真实 SSE MCP 协议，不使用公网凭证或调用模型。"""

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agentsystem.common.models import SessionContext
from application.evoagentbench_research_agent.plugins.sse import sse_tools


@pytest.fixture
async def sse_server(tmp_path: Path) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = tmp_path / "server.py"
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        f"app = FastMCP('search', host='127.0.0.1', port={port}, sse_path='/mcp')\n"
        "@app.tool()\n"
        "def search(query: str) -> str:\n"
        "    return 'corpus result for ' + query\n"
        "@app.tool()\n"
        "def shell(command: str) -> str:\n"
        "    return command\n"
        "app.run(transport='sse')\n"
    )
    with (tmp_path / "server.log").open("w") as output:
        child = subprocess.Popen(
            [sys.executable, str(script)], stdout=output, stderr=output
        )
        try:
            async with asyncio.timeout(10):
                while True:
                    if child.poll() is not None:
                        raise RuntimeError("Test SSE server exited")
                    try:
                        reader, writer = await asyncio.open_connection(
                            "127.0.0.1", port
                        )
                        del reader
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            child.terminate()
            await asyncio.to_thread(child.wait, 5)


async def test_actual_sse_schema_discovery_and_search_only(sse_server: str) -> None:
    tools = await sse_tools(sse_server)
    assert [tool.name for tool in tools] == ["search"]
    assert tools[0].parameters["required"] == ["query"]
    result = await tools[0].handler.invoke(
        ctx=SessionContext(
            app_id="test", agent_id="test", user_id="test", session_id="test"
        ),
        arguments={"query": "benchmark"},
    )
    assert result.content == "corpus result for benchmark"


async def test_missing_allowlist_entry_rejected(sse_server: str) -> None:
    with pytest.raises(ValueError, match="missing"):
        await sse_tools(sse_server, allowlist=("get_document",))
