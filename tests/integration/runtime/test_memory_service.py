"""真实 Session 初始化屏障与 Memory 工具形成完整快照链路。"""

from typing import cast

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import AgentMemoryConfig
from agentsystem.context.system_prompt import SystemPromptBuilder, system_message_id
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.service import MemoryService
from agentsystem.memory.store import MemoryScope
from agentsystem.message.models import Message, TextBlock
from agentsystem.runtime.session import AgentSession
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.registry import ToolRegistry
from tests.unit.runtime.port_stubs import (
    InboxStub,
    MessageManagerStub,
    StateManagerStub,
)
from tests.unit.runtime.test_session import (
    _CONFIG,
    _CONTEXT,
    _Loop,
    _Pipeline,
    _UnusedLock,
)


class _Messages(MessageManagerStub):
    """支持多个 Session 的隔离快照夹具。"""

    def __init__(self) -> None:
        self.items: dict[str, Message] = {}
        self.commits = 0

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        return self.items.get(message_id)

    async def commit(self, ctx: SessionContext, messages: tuple[Message, ...]) -> None:
        self.commits += 1
        self.items.update((message.message_id, message) for message in messages)


def session(messages: _Messages, service: MemoryService) -> AgentSession:
    """沿用真实 Session 与最小事件端口。"""
    pipeline = _Pipeline()
    return AgentSession(
        message_projector=pipeline.message,
        state_projector=pipeline.state,
        trace_projector=pipeline.trace,
        public_projector=pipeline.public,
        projector_timeout_seconds=1,
        lock=_UnusedLock(),
        state_manager=StateManagerStub(),
        inbox=InboxStub(),
        loop_builder=lambda _: _Loop(),
        messages=messages,
        system_prompt_builder=SystemPromptBuilder(
            base_prompt="base",
            tool_registry=ToolRegistry(),
            skill_index=SkillIndex({}),
            memory_service=service,
        ),
        agent_config=_CONFIG,
    )


async def test_memory_snapshot_is_frozen_until_new_session() -> None:
    store = LocalMemoryStore()
    service = MemoryService(store, config=AgentMemoryConfig())
    await service.add(_CONTEXT, MemoryScope.USER_GLOBAL, "old")
    messages = _Messages()
    runner = session(messages, service)
    await runner._ensure_bootstrapped(_CONTEXT)
    saved = messages.items[system_message_id(_CONTEXT)]
    await service.update(_CONTEXT, MemoryScope.USER_GLOBAL, "old", "new")
    await runner._ensure_bootstrapped(_CONTEXT)
    assert messages.items[system_message_id(_CONTEXT)] == saved
    fresh = _CONTEXT.model_copy(update={"session_id": "fresh"})
    await runner._ensure_bootstrapped(fresh)
    content = messages.items[system_message_id(fresh)].content[0]
    assert isinstance(content, TextBlock) and "\nnew\n" in content.text
    assert "\nold\n" in cast(TextBlock, saved.content[0]).text


async def test_memory_failure_does_not_save_partial_system() -> None:
    class FailingService(MemoryService):
        async def context(self, ctx: SessionContext, scope: MemoryScope) -> str | None:
            raise RuntimeError("unavailable")

    service = FailingService(LocalMemoryStore(), config=AgentMemoryConfig())
    messages = _Messages()
    with pytest.raises(RuntimeError, match="unavailable"):
        await session(messages, service)._ensure_bootstrapped(_CONTEXT)
    assert messages.commits == 0
