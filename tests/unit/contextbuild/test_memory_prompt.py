"""Memory 提供原始限长副本，Builder 只渲染。"""

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import AgentMemoryConfig
from agentsystem.context.system_prompt import SystemPromptBuilder
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.service import MemoryReadResult, MemoryService
from agentsystem.memory.store import MemoryScope
from agentsystem.message.models import TextBlock
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePoint,
    SystemPromptBuildPayload,
)
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.registry import ToolRegistry
from tests.unit.contextbuild.test_pipeline import agent_config, context
from tests.unit.middleware.test_system_prompt import manager, registration


async def test_raw_memory_sections_and_metadata() -> None:
    service = MemoryService(LocalMemoryStore(), config=AgentMemoryConfig())
    raw = '<note>中文 & "quoted" &lt;</note>'
    await service.add(context(), MemoryScope.USER_AGENT, raw)
    await service.add(context(), MemoryScope.USER_GLOBAL, "文" * 1000)
    builder = SystemPromptBuilder(
        base_prompt="base",
        tool_registry=ToolRegistry(),
        skill_index=SkillIndex({}),
        memory_service=service,
    )
    message = await builder.build(context(), agent_config=agent_config())
    block = message.content[0]
    assert isinstance(block, TextBlock)
    assert raw in block.text and "&amp;lt;" not in block.text
    assert "<user_memory>" in block.text and "<agent_memory>" in block.text
    assert "文" * 1000 in block.text and "文" * 1001 not in block.text
    assert "length_chars" not in block.text and "included_chars" not in block.text
    assert "scope=" not in block.text and "truncated=" not in block.text
    assert "建议精简" not in block.text
    assert block.text.index("<user_memory>") < block.text.index("<agent_memory>")


@pytest.mark.parametrize("before", [True, False])
async def test_memory_respects_whole_prompt_replacement(before: bool) -> None:
    class CountingService(MemoryService):
        calls = 0

        async def read(
            self, ctx: SessionContext, scope: MemoryScope
        ) -> MemoryReadResult:
            self.calls += 1
            return await super().read(ctx, scope)

    service = CountingService(LocalMemoryStore(), config=AgentMemoryConfig())

    async def replace(ctx: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.UPDATE,
            payload=SystemPromptBuildPayload(prompt="replacement"),
        )

    point = (
        MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD
        if before
        else MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD
    )
    builder = SystemPromptBuilder(
        base_prompt="base",
        tool_registry=ToolRegistry(),
        skill_index=SkillIndex({}),
        memory_service=service,
        middleware_manager=manager(registration(point, replace)),
    )
    message = await builder.build(context(), agent_config=agent_config())
    assert message.content == (TextBlock(text="replacement"),)
    assert service.calls == (0 if before else 2)
