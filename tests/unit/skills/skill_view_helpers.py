"""通过真实 builtin Plugin 构建和调用 Skill 阅读工具。"""

import json
from pathlib import Path
from typing import Any, cast

import agentsystem
from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.message.models import ToolCallBlock, ToolResultBlock
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.skill.models import SkillRoots
from agentsystem.tool.context import ToolInvocationContext
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolDefinition, ToolExecutionResult
from agentsystem.tool.registry import ToolRegistry


def skill_view_tools(roots: tuple[Path, ...] = ()) -> tuple[ToolDefinition, ...]:
    """不注入 SkillIndex，返回真实工具贡献。"""
    source = next(
        source
        for source in discover_builtin_plugins(Path(agentsystem.__file__).parent)
        if source.plugin_id == "builtin.skill.skill-view-tool"
    )
    services = PluginSetupServices()
    services.register(SkillRoots, SkillRoots(roots))
    services.freeze()
    return load_plugin_contributions(
        (source,),
        agent_id="agent",
        options={},
        services=services,
    )[0].tools


async def execute_tool(
    definition: ToolDefinition,
    arguments: dict[str, object],
) -> ToolResultBlock:
    """通过真实 Executor 执行参数校验、授权和 handler 调用。"""
    registry = ToolRegistry()
    registry.register(definition)
    registry.freeze()
    call = ToolCallBlock(
        tool_call_id="skill-call",
        tool_name=definition.name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )
    context = ToolInvocationContext(
        session_context=SessionContext(
            app_id="app",
            user_id="user",
            agent_id="agent",
            session_id="session",
        ),
        run_id="run",
        runtime_trace_id="trace",
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        turn_index=1,
    )
    result = await ToolExecutor(registry).execute(context, call)
    assert isinstance(result, ToolExecutionResult)
    return result.block


async def invoke_tool(
    definition: ToolDefinition,
    arguments: dict[str, object],
) -> dict[str, Any]:
    """解码 Executor 的成功结果，将错误页转为便于断言的 ToolError。"""
    block = await execute_tool(definition, arguments)
    if block.is_error:
        assert block.error_code is not None
        raise ToolError(code=block.error_code, message=block.content)
    return cast(dict[str, Any], json.loads(block.content))
