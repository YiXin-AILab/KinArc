"""供真实 stdio MCP adapter 合约测试启动的最小 Server。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("agentsystem-test")


@server.tool()
def add(left: int, right: int) -> int:
    """返回两个整数之和。"""
    return left + right


@server.tool()
def reject() -> None:
    """返回 MCP 协议级工具失败。"""
    raise ValueError("expected rejection")


if __name__ == "__main__":
    server.run(transport="stdio")
