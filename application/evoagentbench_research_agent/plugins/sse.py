"""基于标准 MCP 客户端的 SSE MCP 工具，显式限制允许使用的工具。"""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent


class SSEHandler:
    """每次调用使用独立且有超时约束的连接，不回退到 shell。"""

    def __init__(self, url: str, name: str, timeout: float) -> None:
        """将已发现的工具绑定到端点及单次调用时限。

        Args:
            url: 评测环境提供的 SSE MCP 端点。
            name: 服务端工具发现返回的精确工具名称。
            timeout: 建连与调用的总时长上限，单位为秒。
        """
        self.url, self.name, self.timeout = url, name, timeout

    async def invoke(
        self, *, ctx: SessionContext, arguments: Mapping[str, Any]
    ) -> ToolOutput:
        """通过新连接调用一个 MCP 工具，并返回文本结果。

        Args:
            ctx: Runtime 会话上下文，当前传输 adapter 不使用此参数。
            arguments: 已校验且待发送给 MCP 服务端的工具参数。

        Returns:
            工具结果中的文本内容，或序列化后的结构化内容。

        Raises:
            ToolError: 服务端报告工具错误，或返回不支持的媒体。
            TimeoutError: 建连或调用超过配置时限。
        """
        del ctx
        async with asyncio.timeout(self.timeout):
            async with sse_client(self.url, timeout=self.timeout) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(self.name, dict(arguments))
        if any(not isinstance(block, TextContent) for block in result.content):
            raise ToolError(
                code="unsupported_media", message="Expected text MCP result"
            )
        content = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        if not content and result.structuredContent is not None:
            content = json.dumps(result.structuredContent, ensure_ascii=False)
        if result.isError:
            raise ToolError(code="mcp_tool_error", message=content)
        return ToolOutput(content=content)


async def sse_tools(
    url: str, *, allowlist: tuple[str, ...] = ("search",), timeout: float = 30
) -> tuple[ToolDefinition, ...]:
    """发现服务端实际 Schema，缺少请求的工具时拒绝启动。

    Args:
        url: 公开检索环境提供的 SSE 端点。
        allowlist: 当前评测配置允许使用的不重复工具名称。
        timeout: 工具发现及每次调用的时长上限，单位为秒。

    Returns:
        使用服务端 Schema 和逐次有界 SSE handler 的工具定义。

    Raises:
        ValueError: 允许的工具列表、超时或发现的工具集合无效。
        TimeoutError: 服务端工具发现超过时限。
    """
    if not allowlist or len(set(allowlist)) != len(allowlist) or timeout <= 0:
        raise ValueError("A nonempty unique allowlist and bounded timeout are required")
    async with asyncio.timeout(timeout):
        async with sse_client(url, timeout=timeout) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.list_tools()
    available = {tool.name: tool for tool in result.tools}
    if not set(allowlist) <= available.keys():
        raise ValueError("SSE server is missing an allowlisted tool")
    return tuple(
        ToolDefinition(
            name=name,
            description=available[name].description or name,
            parameters=available[name].inputSchema,
            source=ToolSource.MCP,
            handler=SSEHandler(url, name, timeout),
        )
        for name in allowlist
    )
