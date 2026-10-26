"""Memory 额度和可信用户身份绑定。"""

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.config.models import AgentMemoryConfig, RuntimeMemoryConfig
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.service import MemoryService
from agentsystem.memory.store import MemoryScope
from pydantic import ValidationError


async def test_quota_and_user_agent_isolation() -> None:
    store = LocalMemoryStore()
    service = MemoryService(store, config=AgentMemoryConfig())
    ctx = SessionContext(app_id="a", user_id="u", agent_id="g", session_id="s")
    scope = MemoryScope.USER_GLOBAL
    with pytest.raises(ToolError) as failure:
        await service.add(ctx, scope, "文" * 1001)
    assert failure.value.code == "memory_capacity_exceeded"
    assert "1000" in failure.value.message and "本次未写入" in failure.value.message
    assert "精简后重试" in failure.value.message and "截断" not in failure.value.message
    assert (await service.read(ctx, scope)).content == ""
    result = await service.add(ctx, scope, "文" * 1000)
    assert result.length.remaining_chars == 0
    with pytest.raises(ToolError):
        await service.add(ctx, scope, "a")
    with pytest.raises(ToolError):
        await service.update(ctx, scope, "文", "文字", replace_all=True)
    view = await service.context(ctx, scope)
    assert view == "文" * 1000
    assert (
        len(
            (
                await service.read(ctx.model_copy(update={"agent_id": "g2"}), scope)
            ).content
        )
        == 1000
    )
    await service.add(ctx, MemoryScope.USER_AGENT, "private")
    assert (
        await service.read(
            ctx.model_copy(update={"agent_id": "g2"}), MemoryScope.USER_AGENT
        )
    ).content == ""
    assert (
        await service.read(ctx.model_copy(update={"user_id": "u2"}), scope)
    ).content == ""
    result = await service.update(ctx, MemoryScope.USER_AGENT, "private", "")
    assert result.changed and result.length.length_chars == 0


async def test_disabled_scope_and_matching_errors() -> None:
    service = MemoryService(LocalMemoryStore(), config=AgentMemoryConfig(scopes=()))
    ctx = SessionContext(app_id="a", user_id="u", agent_id="g", session_id="s")
    assert await service.context(ctx, MemoryScope.USER_GLOBAL) is None
    with pytest.raises(ToolError, match="scope"):
        await service.read(ctx, MemoryScope.USER_GLOBAL)


@pytest.mark.parametrize(
    "config",
    [
        {"scopes": ["user_global", "user_global"]},
        {"scopes": ["agent"]},
        {"max_chars": {"user_global": True}},
        {"max_chars": {"user_agent": 0}},
        {"max_chars": {"user_agent": 2.5}},
        {"max_chars": {"user_agent": 1_000_001}},
        {"unexpected": 1},
    ],
)
def test_invalid_config(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentMemoryConfig.model_validate(config)


def test_runtime_memory_defaults() -> None:
    assert not RuntimeMemoryConfig().enabled
    with pytest.raises(ValidationError):
        RuntimeMemoryConfig.model_validate({"store": {"type": "unknown"}})
