"""绑定运行时身份的 Todo 工具。"""

import logging
from collections.abc import Awaitable
from typing import Annotated

from pydantic import Field

from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.todo.models import TodoItem, TodoList, encode_todos
from agentsystem.todo.service import TodoService
from agentsystem.tool.context import get_tool_context
from agentsystem.tool.models import ToolOutput

LOGGER = logging.getLogger(__name__)

_READ_DESCRIPTION = (
    "读取当前会话的任务清单及进度。继续之前的工作、上下文压缩后，"
    "或不确定清单当前内容时使用。"
)
_WRITE_DESCRIPTION = """创建或更新当前会话的任务清单，
用于跟踪需要多个步骤完成的工作，或用户明确要求记录的任务。简单问答无需创建清单。

每次提交完整清单：保留仍需跟踪的任务，更新实际进度，移除不再需要的任务。
整份清单都与当前工作无关时，可以传入空数组清空。

按有明确结果的实际工作组织任务，不要将“发送最终回复”单独列为任务。

状态含义：
- pending：尚未开始。
- in_progress：已开始执行，尚未完成。
- completed：工作已实际完成，且必要的验证已通过。

开始处理任务时标记为 in_progress，完成后及时标记为 completed。
并行处理的任务可以同时为 in_progress。"""

_WRITE_RULES = (
    "最终回复前核对清单，将已完成的任务，以及将在本次最终回复中直接完成交付的任务"
    "更新为已完成的状态。"
)


async def _output(operation: Awaitable[tuple[TodoItem, ...]]) -> ToolOutput:
    try:
        todos = await operation
    except AdapterError as error:
        log_unexpected_exception(
            LOGGER, "todo_tool_failed", error, operation=error.operation
        )
        raise ToolError(code=error.code, message=error.message) from error
    except ValueError as error:
        raise ToolError(
            code="todo_invalid", message="Todo 内容或身份参数无效"
        ) from error
    return ToolOutput(content='{"todos":' + encode_todos(todos) + "}")


def register(context: PluginSetupContext) -> None:
    """提供候选工具，实际启用由 Agent 的能力列表决定。

    Args:
        context: 插件装配上下文，提供依赖查询及注册入口。
    """
    service = context.get(TodoService)
    if service is None:
        return

    async def read() -> ToolOutput:
        """读取当前会话的完整 Todo 清单；无需输入参数。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        return await _output(service.read(get_tool_context().session_context))

    async def write(
        todos: Annotated[
            TodoList, Field(description="完整有序清单；空数组清空，未提交的条目移除")
        ],
    ) -> ToolOutput:
        """完整替换当前会话 Todo 清单，返回本次提交内容。

        Args:
            todos: 按展示顺序排列的完整清单，空清单表示清空。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        return await _output(
            service.replace(
                get_tool_context().session_context,
                tuple(TodoItem.model_validate(item) for item in todos),
            )
        )

    context.register_tool(
        read, name="todo_read", description=_READ_DESCRIPTION, is_concurrency_safe=False
    )
    context.register_tool(
        write,
        name="todo_write",
        description=_WRITE_DESCRIPTION,
        rules=_WRITE_RULES,
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "maxItems": 50,
                    "items": TodoItem.model_json_schema(),
                    "description": (
                        "完整任务清单，按执行顺序排列。传入空数组可清空清单"
                    ),
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
    )
