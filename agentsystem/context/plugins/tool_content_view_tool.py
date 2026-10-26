"""注册并实现当前 session 的 tool_content_view Tool。"""

import json
from typing import Annotated, Literal

from pydantic import Field

from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.common.models import FrozenModel
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import Message, ToolCallBlock, ToolResultBlock
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.tool.context import get_tool_context
from agentsystem.tool.models import ToolOutput


class ToolContentSource(FrozenModel):
    """指向持久化的原始工具调用，恢复页不改变来源。"""

    tool_call_id: str = Field(min_length=1)
    part: Literal["arguments", "result"]


class ToolContentRange(FrozenModel):
    """原文中的左闭右开字符区间，next_offset 为空表示已读至末尾。"""

    offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    next_offset: int | None = Field(ge=0)


class ToolContentPage(FrozenModel):
    """将来源和区间置于正文前，优先保留补读原文的定位信息。"""

    source: ToolContentSource
    range: ToolContentRange
    content: str


def _find_content(
    messages: tuple[Message, ...],
    *,
    tool_call_id: str,
    part: Literal["arguments", "result"],
) -> tuple[str, str] | None:
    """按稳定存储顺序从新到旧返回第一个匹配 Block。"""
    for message in reversed(messages):
        for block in reversed(message.content):
            if (
                part == "arguments"
                and isinstance(block, ToolCallBlock)
                and block.tool_call_id == tool_call_id
            ):
                return block.tool_name, block.raw_arguments
            if (
                part == "result"
                and isinstance(block, ToolResultBlock)
                and block.tool_call_id == tool_call_id
            ):
                return block.tool_name, block.content
    return None


def register(context: PluginSetupContext) -> None:
    """存在 MessageManager capability 时注册 builtin Tool。

    Args:
        context: 当前 Agent 的能力查找和工具注册入口。

    未提供 MessageManager 时不注册；运行时通过调用上下文限定到当前 session。
    """
    # Protocol class 仅作为运行时 capability key，不会被实例化。
    message_manager = context.get(MessageManager)  # type: ignore[type-abstract]
    if message_manager is None:
        return

    async def tool_content_view(
        tool_call_id: Annotated[str, Field(min_length=1)],
        part: Literal["arguments", "result"] = "result",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=8_000)] = 8_000,
    ) -> ToolOutput:
        """分页读取当前 session 中指定工具调用的原始参数或结果。

        Args:
            tool_call_id: 目标调用 ID，匹配最新存储的对应块。
            part: 读取原始 arguments 或 result。
            offset: 文本字符起始偏移。
            limit: 单页字符数上限。

        Returns:
            带内容、总字符数和后续分页位置的 JSON 文本。

        Raises:
            ToolError: 历史读取失败或找不到目标调用内容。
        """
        invocation = get_tool_context()
        try:
            messages = await message_manager.list_messages(invocation.session_context)
        except AdapterError as error:
            raise ToolError(
                code="tool_history_unavailable",
                message="工具历史暂时不可用",
            ) from error
        match = _find_content(
            messages,
            tool_call_id=tool_call_id,
            part=part,
        )
        if match is None:
            raise ToolError(
                code="tool_content_not_found",
                message=f"未找到指定工具调用的 {part} 内容",
            )
        tool_name, original = match
        if tool_name == "tool_content_view":
            raise ToolError(
                code="tool_content_recursive_read",
                message="不能读取恢复工具自身的调用内容，请直接读取原始工具调用",
            )
        content = original[offset : offset + limit]
        end_offset = offset + len(content)
        page = ToolContentPage(
            source=ToolContentSource(tool_call_id=tool_call_id, part=part),
            range=ToolContentRange(
                offset=offset,
                end_offset=end_offset,
                total_chars=len(original),
                next_offset=end_offset if end_offset < len(original) else None,
            ),
            content=content,
        )
        return ToolOutput(
            content=json.dumps(
                page.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    context.register_tool(
        tool_content_view,
        name="tool_content_view",
        description=(
            "读取指定工具调用的原始参数或结果，仅用于当前上下文中已被剪枝的工具内容。"
        ),
        rules=(
            "读取被剪枝的工具原始参数或结果，首次读取必须依据对应的 "
            "<pruned-tool-content> 标记。"
            "后续始终使用 source 中原始调用的 tool_call_id 和 part。"
            "禁止读取未被剪枝的完整内容或 tool_content_view 自身的调用内容，"
            "禁止用本工具重试失败调用或替代业务工具。"
        ),
    )


__all__ = ["register"]
