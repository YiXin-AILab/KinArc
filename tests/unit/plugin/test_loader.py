"""Plugin 同步 register 协议与 Contribution Loader 测试。"""

import inspect
from collections.abc import Coroutine
from pathlib import Path

import pytest
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.tool.models import ToolOutput


def _services() -> PluginSetupServices:
    services = PluginSetupServices()
    services.freeze()
    return services


def _source(
    plugin_id: str,
    register: object,
    *,
    origin: PluginOrigin = PluginOrigin.APPLICATION,
) -> PluginSource:
    return PluginSource(
        plugin_id=plugin_id,
        origin=origin,
        path=Path(f"{plugin_id.rsplit('.', 1)[-1]}.py"),
        register=register,  # type: ignore[arg-type]
    )


def test_loader_preserves_source_and_local_registration_order() -> None:
    """Loader 按显式 Source 顺序密封每个 Plugin 的完整贡献。"""

    def first(context: PluginSetupContext) -> None:
        async def beta() -> ToolOutput:
            """Beta tool."""
            return ToolOutput(content="beta")

        async def alpha() -> ToolOutput:
            """Alpha tool."""
            return ToolOutput(content="alpha")

        context.register_tool(beta)
        context.register_tool(alpha)

    def second(context: PluginSetupContext) -> None:
        assert context.options("shared") == {"enabled": True}

    contributions = load_plugin_contributions(
        (
            _source("builtin.first-plugin", first, origin=PluginOrigin.BUILTIN),
            _source("application.agent.second-plugin", second),
        ),
        agent_id="agent",
        options={"shared": {"enabled": True}},
        services=_services(),
    )

    assert [item.plugin_id for item in contributions] == [
        "builtin.first-plugin",
        "application.agent.second-plugin",
    ]
    assert [tool.name for tool in contributions[0].tools] == ["beta", "alpha"]


@pytest.mark.parametrize("invalid_result", (1, "invalid"))
def test_loader_rejects_non_none_register_result(invalid_result: object) -> None:
    """同步 register 必须严格返回 None。"""

    def register(context: PluginSetupContext) -> object:
        del context
        return invalid_result

    with pytest.raises(ValueError, match=r"agent.*invalid-plugin.*register"):
        load_plugin_contributions(
            (_source("application.agent.invalid-plugin", register),),
            agent_id="agent",
            options={},
            services=_services(),
        )


def test_loader_rejects_awaitable_result_and_closes_coroutine() -> None:
    """伪装成同步入口的 awaitable 返回值也必须协议失败。"""
    returned: list[Coroutine[object, object, None]] = []

    async def deferred() -> None:
        await _never()

    def register(context: PluginSetupContext) -> object:
        del context
        result = deferred()
        returned.append(result)
        return result

    with pytest.raises(ValueError, match="awaitable"):
        load_plugin_contributions(
            (_source("application.agent.awaitable-plugin", register),),
            agent_id="agent",
            options={},
            services=_services(),
        )

    assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED


async def _never() -> None:
    return None


def test_loader_seals_context_when_register_raises() -> None:
    """register 异常不能让 Plugin 保存的 Context 继续可用。"""
    captured: list[PluginSetupContext] = []

    def register(context: PluginSetupContext) -> None:
        captured.append(context)
        raise RuntimeError("boom")

    with pytest.raises(ValueError, match=r"agent.*broken-plugin.*register"):
        load_plugin_contributions(
            (_source("application.agent.broken-plugin", register),),
            agent_id="agent",
            options={},
            services=_services(),
        )

    with pytest.raises(RuntimeError, match="密封"):
        captured[0].options("anything")


def test_loader_rejects_duplicate_plugin_ids_before_execution() -> None:
    """重复 Plugin ID 不能执行任一入口或产生部分贡献。"""
    calls = 0

    def register(context: PluginSetupContext) -> None:
        nonlocal calls
        del context
        calls += 1

    sources = (
        _source("application.agent.duplicate", register),
        _source("application.agent.duplicate", register),
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_plugin_contributions(
            sources,
            agent_id="agent",
            options={},
            services=_services(),
        )

    assert calls == 0
