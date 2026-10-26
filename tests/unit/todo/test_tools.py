"""真实 Plugin/Executor 下的工具协议。"""

import json
from pathlib import Path

import pytest
from agentsystem.message.models import ToolCallBlock
from agentsystem.plugin.assembler import assemble_agent_capabilities
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.skill.index import SkillIndex
from agentsystem.todo.local.store import LocalTodoStore
from agentsystem.todo.plugins.todo_tools import register
from agentsystem.todo.service import TodoService
from agentsystem.tool.context import ToolInvocationContext
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolExecutionResult
from agentsystem.tool.registry import ToolRegistry
from tests.unit.todo.test_store import CTX


def registry(
    service: TodoService, patterns: tuple[str, ...] | None = None
) -> ToolRegistry:
    """装配真实工具，验证最终筛选结果。"""
    return assemble_agent_capabilities(
        agent_id="g",
        sources=(
            PluginSource(
                plugin_id="builtin.todo",
                origin=PluginOrigin.BUILTIN,
                path=Path(__file__),
                register=register,
            ),
        ),
        plugin_options={},
        context_provider_types=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1,
        max_content_chars=1000,
        plugin_capabilities=((TodoService, service),),
        enabled_tool_patterns=patterns,
    ).tool_registry


@pytest.mark.parametrize(
    "patterns,expected",
    [
        (None, {"todo_read", "todo_write"}),
        ((), set()),
        (("todo_write",), {"todo_write"}),
    ],
)
def test_agent_tool_selection(
    patterns: tuple[str, ...] | None, expected: set[str]
) -> None:
    assert set(registry(TodoService(LocalTodoStore()), patterns).names()) == expected


async def test_tools_read_write_clear_and_reject_extra() -> None:
    tools = registry(TodoService(LocalTodoStore()))
    read = tools.get("todo_read")
    assert read is not None and read.parameters["properties"] == {}
    assert set(tools.names()) == {"todo_read", "todo_write"}
    executor = ToolExecutor(tools)
    for name, arguments, expected in (
        ("todo_read", {}, {"todos": []}),
        (
            "todo_write",
            {"todos": [{"content": "x", "status": "pending"}]},
            {"todos": [{"content": "x", "status": "pending"}]},
        ),
        ("todo_read", {}, {"todos": [{"content": "x", "status": "pending"}]}),
        ("todo_write", {"todos": []}, {"todos": []}),
    ):
        context = ToolInvocationContext(
            session_context=CTX,
            run_id="r",
            runtime_trace_id="t",
            tool_call_id="c",
            tool_name=name,
            turn_index=0,
        )
        call = ToolCallBlock(
            tool_call_id="c",
            tool_name=name,
            arguments=arguments,
            raw_arguments=json.dumps(arguments),
        )
        result = await executor.execute(context, call)
        assert isinstance(result, ToolExecutionResult)
        assert not result.block.is_error
        assert json.loads(result.block.content) == expected
        definition = tools.get(name)
        assert definition is not None
        assert not definition.is_concurrency_safe and not definition.can_suspend
    assert read.invocation_model is not None
    with pytest.raises(ValueError):
        read.invocation_model.model_validate({"session_id": "other"})
    assert registry(TodoService(LocalTodoStore()), ("todo_read",)).names() == (
        "todo_read",
    )


def test_write_schema_exposes_array_and_items_without_references() -> None:
    tool = registry(TodoService(LocalTodoStore())).get("todo_write")
    assert tool is not None
    schema = json.loads(json.dumps(tool.llm_schema))
    todos = schema["function"]["parameters"]["properties"]["todos"]
    assert todos["type"] == "array"
    assert todos["items"]["type"] == "object"
    assert todos["items"]["required"] == ["content", "status"]
    assert "$ref" not in json.dumps(schema)
    assert tool.invocation_model is not None
    with pytest.raises(ValueError):
        tool.invocation_model.model_validate(
            {"todos": '[{"content":"x","status":"pending"}]'}
        )
