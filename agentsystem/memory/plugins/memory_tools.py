"""三个绑定当前用户身份的记忆工具。"""

import json
import logging
from collections.abc import Awaitable
from dataclasses import asdict
from typing import Annotated

from pydantic import AfterValidator, Field, StrictBool, StrictStr

from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.memory.service import (
    MemoryReadResult,
    MemoryService,
    MemoryUpdateResult,
)
from agentsystem.memory.store import MemoryScope
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.tool.context import get_tool_context
from agentsystem.tool.models import ToolOutput

LOGGER = logging.getLogger(__name__)


def _nonblank(value: str) -> str:
    value.encode("utf-8")
    if not value.strip():
        raise ValueError("new_content must contain non-whitespace text")
    return value


async def _output(
    operation: Awaitable[MemoryReadResult | MemoryUpdateResult],
) -> ToolOutput:
    try:
        result = await operation
    except AdapterError as error:
        log_unexpected_exception(
            LOGGER, "memory_tool_failed", error, operation=error.operation
        )
        raise ToolError(code=error.code, message=error.message) from error
    payload: dict[str, object] = {"scope": result.scope.value}
    if isinstance(result, MemoryReadResult):
        payload["content"] = result.content
    else:
        payload["changed"] = result.changed
        payload.update(asdict(result.length))
        payload["notice"] = (
            f"当前 {result.length.length_chars} / {result.length.limit_chars} 字符，"
            f"剩余 {result.length.remaining_chars} 字符"
        )
    return ToolOutput(content=json.dumps(payload, ensure_ascii=False))


def register(context: PluginSetupContext) -> None:
    """仅在当前 Agent 启用 Memory 时注册工具。

    Args:
        context: 插件装配上下文，提供依赖查询及注册入口。
    """
    service = context.get(MemoryService)
    if service is None or not service.enabled:
        return

    async def read(
        scope: Annotated[
            MemoryScope,
            Field(
                description=(
                    "记忆范围：user_global 为当前用户跨 Agent 共享的记忆；"
                    "user_agent 为当前用户仅供当前 Agent 使用的记忆。"
                )
            ),
        ],
    ) -> ToolOutput:
        """读取指定范围的记忆。

        Args:
            scope: 用户记忆范围，Service 会校验是否启用。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        return await _output(service.read(get_tool_context().session_context, scope))

    async def add(
        scope: Annotated[
            MemoryScope,
            Field(
                description=(
                    "记忆范围：user_global 跨 Agent 共享，"
                    "user_agent 仅当前 Agent 使用；"
                    "两者均属于当前用户。"
                )
            ),
        ],
        new_content: Annotated[
            StrictStr,
            AfterValidator(_nonblank),
            Field(
                description=(
                    "要追加的原始文本，必须包含非空白字符。已有正文时以一个换行分隔，"
                    "不覆盖或去重；无需先读取。"
                )
            ),
        ],
    ) -> ToolOutput:
        """向指定范围追加记忆。

        Args:
            scope: 用户记忆范围，Service 会校验是否启用。
            new_content: 含非空白字符的追加原文，已有正文时用单个换行分隔。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        return await _output(
            service.add(get_tool_context().session_context, scope, new_content)
        )

    async def update(
        scope: Annotated[
            MemoryScope,
            Field(
                description=(
                    "记忆范围：user_global 跨 Agent 共享，"
                    "user_agent 仅当前 Agent 使用；"
                    "两者均属于当前用户。"
                )
            ),
        ],
        old_content: Annotated[
            StrictStr,
            Field(
                min_length=1,
                description=(
                    "要替换的非空原文，精确区分大小写、空白和换行。"
                    "默认必须唯一匹配；找不到或匹配多处时可先读取完整记忆。"
                ),
            ),
        ],
        new_content: Annotated[
            StrictStr,
            Field(
                description=(
                    "替换后的文本；空字符串表示删除匹配内容，纯空白文本按原样保留。"
                )
            ),
        ],
        replace_all: Annotated[
            StrictBool,
            Field(
                description=(
                    "默认 false，只允许唯一匹配；true 替换全部非重叠匹配，"
                    "不会再次匹配本次生成的新内容。"
                )
            ),
        ] = False,
    ) -> ToolOutput:
        """通过精确匹配原文，替换或删除指定范围的记忆。

        Args:
            scope: 用户记忆范围，Service 会校验是否启用。
            old_content: 精确匹配的非空原文，区分大小写和空白。
            new_content: 新正文片段；替换时空字符串表示删除。
            replace_all: 是否替换全部非重叠匹配；否则要求唯一匹配起点。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        return await _output(
            service.update(
                get_tool_context().session_context,
                scope,
                old_content,
                new_content,
                replace_all,
            )
        )

    context.register_tool(read, name="memory_read", is_concurrency_safe=True)
    context.register_tool(add, name="memory_add", is_concurrency_safe=True)
    context.register_tool(
        update,
        name="memory_update",
        is_concurrency_safe=True,
    )
