"""评测期 Banking Tool 到 Tau2 官方 Environment 的 UDS 客户端。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from agentsystem.common.errors import ToolError
from agentsystem.tool import get_tool_context
from agentsystem.tool.models import ToolOutput
from pydantic import BaseModel, Field, ValidationError


class _ToolMessage(BaseModel):
    id: str = Field(min_length=1)
    role: Literal["tool"]
    content: str | None = None
    requestor: Literal["assistant"]
    error: bool


class Tau2ToolClient:
    """把当前 Runtime ToolCall 同步转发到 case 私有 Tau2 Bridge。"""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 30,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("Tau2 Tool Bridge socket 必须是绝对路径")
        if timeout_seconds <= 0:
            raise ValueError("Tau2 Tool Bridge timeout 必须为正数")
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or self._create_client

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolOutput:
        """转发一次 ToolCall，并校验结果仍对应当前 Runtime call ID。"""
        invocation = get_tool_context()
        payload: dict[str, object] = {
            "id": invocation.tool_call_id,
            "name": tool_name,
            "arguments": arguments,
            "requestor": "assistant",
        }
        try:
            async with self._client_factory() as client:
                response = await client.post("/tools/execute", json=payload)
                response.raise_for_status()
                message = _ToolMessage.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, TypeError, ValueError) as error:
            raise ToolError(
                code="tau2_tool_bridge_failed",
                message="Tau2 工具桥调用失败",
            ) from error
        if message.id != invocation.tool_call_id:
            raise ToolError(
                code="tau2_tool_bridge_mismatch",
                message="Tau2 工具桥返回了错误的 ToolCall ID",
            )
        if message.error:
            raise ToolError(
                code="tau2_tool_failed",
                message=message.content or "Tau2 工具执行失败",
            )
        return ToolOutput(content=message.content or "")

    def _create_client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://tau2",
            timeout=self._timeout_seconds,
        )


__all__ = ["Tau2ToolClient"]
