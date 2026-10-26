"""Model Context Protocol stdio ToolHandler adapter。"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.tool.models import ToolOutput


class MCPStdioToolHandler:
    """每次调用建立并清理一个标准 stdio MCP client session。

    保存子进程启动配置，不跨调用复用连接；MCP SDK 类型只在此 adapter 内使用。
    """

    def __init__(
        self,
        *,
        command: str,
        args: Sequence[str],
        tool_name: str,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """保存 Server 启动参数，不在构造时启动子进程。

        Args:
            command: MCP Server 可执行命令。
            args: 传给子进程的命令行参数。
            tool_name: 远端 MCP 工具名。
            timeout_seconds: 建连、初始化和工具调用的总秒数上限。
            env: 传给 SDK 的可选子进程环境变量映射。

        Raises:
            ValueError: 命令、工具名为空或 timeout 不为正数。
        """
        if not command.strip() or not tool_name.strip() or timeout_seconds <= 0:
            raise ValueError("MCP command、tool_name 和 timeout 必须有效")
        self._parameters = StdioServerParameters(
            command=command,
            args=list(args),
            env=dict(env) if env is not None else None,
        )
        self._tool_name = tool_name
        self._timeout_seconds = timeout_seconds

    async def invoke(
        self,
        *,
        ctx: SessionContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutput:
        """初始化 MCP session、调用工具并归一化文本或结构化结果。

        Args:
            ctx: 统一 handler 接口的会话身份，本 adapter 不使用。
            arguments: 传给远端工具的参数。

        Returns:
            拼接后的文本块；无文本时使用结构化结果的 JSON 表示。

        Raises:
            ToolError: MCP 返回 isError。
            TimeoutError: 建连或调用超时。

        SDK 和传输异常继续传播，由 Executor 负责归一化和记录。
        """
        del ctx
        async with asyncio.timeout(self._timeout_seconds):
            async with stdio_client(self._parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        self._tool_name,
                        arguments=dict(arguments),
                    )
        text = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        if not text and result.structuredContent is not None:
            text = json.dumps(
                result.structuredContent,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if result.isError is True:
            raise ToolError(
                code="mcp_tool_error",
                message=text or "MCP 工具执行失败",
            )
        return ToolOutput(content=text)
