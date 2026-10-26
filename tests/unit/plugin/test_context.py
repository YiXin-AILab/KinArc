"""单 Plugin 一次性 SetupContext 测试。"""

from collections.abc import Mapping

import pytest
from agentsystem.common.models import JsonValue
from agentsystem.context.providers import ContextProvider
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
    MiddlewareScope,
)
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.tool.models import ToolOutput, ToolSource


class _Capability:
    """测试用 Agent capability。"""


def _services() -> tuple[PluginSetupServices, _Capability]:
    value = _Capability()
    services = PluginSetupServices()
    services.register(_Capability, value)
    services.freeze()
    return services, value


def _context(
    *,
    options: Mapping[str, Mapping[str, JsonValue]] | None = None,
    origin: PluginOrigin = PluginOrigin.APPLICATION,
) -> PluginSetupContext:
    services, _ = _services()
    return PluginSetupContext(
        plugin_id="application.agent.audit-plugin",
        agent_id="agent",
        origin=origin,
        options=options or {},
        services=services,
    )


def test_setup_context_options_are_exact_isolated_and_top_level_read_only() -> None:
    """自定义 key 精确匹配，且读取结果不与 AgentConfig 共享容器。"""
    raw: dict[str, dict[str, JsonValue]] = {
        "Audit": {"nested": {"limit": 3}},
        "unused": {"enabled": True},
    }
    context = _context(options=raw)

    selected = context.options("Audit")
    raw["Audit"]["nested"] = {"limit": 99}

    assert selected == {"nested": {"limit": 3}}
    assert context.options("audit") == {}
    assert context.options("missing") == {}
    with pytest.raises(TypeError):
        selected["new"] = True  # type: ignore[index]


def test_setup_context_resolves_current_agent_capability() -> None:
    """Context get/require 返回当前 Agent 服务表中的同一实例。"""
    services, value = _services()
    context = PluginSetupContext(
        plugin_id="builtin.context.provider-middleware",
        agent_id="agent",
        origin=PluginOrigin.BUILTIN,
        options={},
        services=services,
    )

    assert context.get(_Capability) is value
    assert context.require(_Capability) is value
    with pytest.raises(KeyError, match=r"audit-plugin|str"):
        _context().require(str)


def test_setup_context_collects_provider_definition_without_constructing() -> None:
    """Provider 注册只记录延迟 Definition，不执行构造闭包。"""
    created = False

    def create() -> ContextProvider:
        nonlocal created
        created = True
        raise AssertionError("本测试不应构造 Provider")

    context = _context()
    context.register_context_provider("audit-context", create)

    contribution = context._seal_and_extract()

    assert created is False
    assert contribution.plugin_id == "application.agent.audit-plugin"
    assert len(contribution.context_providers) == 1
    assert contribution.context_providers[0].provider_type == "audit-context"
    assert contribution.context_providers[0].create is create


async def test_setup_context_builds_tool_and_middleware_contributions() -> None:
    """ToolSource 与 Middleware ID 只能由当前 Plugin 身份生成。"""

    async def query(value: str) -> ToolOutput:
        """Query audit records."""
        return ToolOutput(content=value)

    async def after_run(invocation: MiddlewareContext) -> MiddlewareDecision:
        del invocation
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    context = _context()
    context.register_tool(query, rules="仅查询当前 Agent。")
    context.register_middleware(
        after_run,
        name="audit-writeback",
        handler_id="after-run",
        point=MiddlewarePoint.AFTER_RUN,
        scope=MiddlewareScope.GLOBAL,
        priority=7,
        timeout_ms=250,
        failure_mode=MiddlewareFailureMode.FAIL_OPEN,
    )

    contribution = context._seal_and_extract()

    tool = contribution.tools[0]
    assert tool.name == "query"
    assert tool.source is ToolSource.CUSTOMIZED
    assert tool.rules == "仅查询当前 Agent。"
    assert await tool.handler.invoke(ctx=object(), arguments={"value": "ok"}) == (
        ToolOutput(content="ok")
    )
    middleware = contribution.middlewares[0]
    assert middleware.name == "audit-writeback"
    assert middleware.middleware_id == "application.agent.audit-plugin.after-run"
    assert middleware.priority == 7
    assert middleware.timeout_ms == 250


@pytest.mark.parametrize("name", [" ", " audit", "audit "])
def test_setup_context_rejects_invalid_middleware_name(name: str) -> None:
    """Middleware 配置名称必须非空且不含首尾空白。"""

    async def middleware(invocation: MiddlewareContext) -> MiddlewareDecision:
        del invocation
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    with pytest.raises(ValueError, match="name"):
        _context().register_middleware(
            middleware,
            name=name,
            handler_id="handler",
            point=MiddlewarePoint.BEFORE_RUN,
        )


def test_setup_context_rejects_local_duplicate_contribution_ids() -> None:
    """单 Plugin 内三类局部身份重复必须立即失败。"""

    def create() -> ContextProvider:
        raise AssertionError

    async def tool() -> ToolOutput:
        """Return a value."""
        return ToolOutput(content="ok")

    async def middleware(invocation: MiddlewareContext) -> MiddlewareDecision:
        del invocation
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    context = _context()
    context.register_context_provider("provider", create)
    context.register_tool(tool)
    context.register_middleware(
        middleware,
        name="handler",
        handler_id="handler",
        point=MiddlewarePoint.BEFORE_RUN,
    )

    with pytest.raises(ValueError, match="provider"):
        context.register_context_provider("provider", create)
    with pytest.raises(ValueError, match="tool"):
        context.register_tool(tool)
    with pytest.raises(ValueError, match="handler"):
        context.register_middleware(
            middleware,
            name="handler",
            handler_id="handler",
            point=MiddlewarePoint.BEFORE_RUN,
        )


def test_setup_context_rejects_all_operations_after_seal() -> None:
    """Plugin 返回后不能继续读取依赖、参数或追加贡献。"""

    async def tool() -> ToolOutput:
        """Return a value."""
        return ToolOutput(content="ok")

    context = _context()
    context._seal_and_extract()

    operations = (
        lambda: context.options("audit"),
        lambda: context.get(_Capability),
        lambda: context.require(_Capability),
        lambda: context.register_tool(tool),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="密封"):
            operation()
