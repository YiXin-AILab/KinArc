"""AgentRuntimeBundle 与不可变 AgentRegistry 测试。"""

from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from agentsystem.common.errors import ServiceError
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
)
from agentsystem.runtime.session import AgentSession
from agentsystem.service.agents import AgentRegistry, AgentRuntimeBundle


class _Session:
    """Registry 测试不执行的最小 Session 占位。"""


def _bundle(
    agent_id: str,
    *,
    close_callback: Callable[[], Awaitable[None]] | None = None,
) -> AgentRuntimeBundle:
    return AgentRuntimeBundle(
        agent_config=AgentConfig(
            agent_id=agent_id,
            agent_name=f"{agent_id} name",
            prompt="prompt.md",
            outbound=AgentOutboundConfig(
                type="log",
            ),
            llm_model=ModelParamsConfig(model="openai/test"),
        ),
        session=cast(AgentSession, _Session()),
        close_callback=close_callback,
    )


def test_registry_resolves_bundles_in_sorted_order() -> None:
    """Registry 对外提供稳定顺序且未知 ID 不回退。"""
    first = _bundle("b")
    second = _bundle("a")
    registry = AgentRegistry((first, second))

    assert registry.agent_ids() == ("a", "b")
    assert registry.resolve("a") is second
    assert registry.resolve("missing") is None
    with pytest.raises(ServiceError) as captured:
        registry.require("missing")
    assert captured.value.code == "agent_not_found"


def test_registry_rejects_duplicate_ids() -> None:
    """Registry 自身也防御重复 ID，不能覆盖已有 Bundle。"""
    with pytest.raises(ValueError, match="duplicate"):
        AgentRegistry((_bundle("duplicate"), _bundle("duplicate")))


async def test_registry_closes_all_agent_resources() -> None:
    """关闭时释放每个 Agent 的专属资源。"""
    closed: list[str] = []

    def callback(agent_id: str) -> Callable[[], Awaitable[None]]:
        async def close() -> None:
            closed.append(agent_id)

        return close

    registry = AgentRegistry(
        (
            _bundle("a", close_callback=callback("a")),
            _bundle("b", close_callback=callback("b")),
        )
    )

    await registry.close()

    assert closed == ["a", "b"]
