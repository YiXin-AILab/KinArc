"""每 Agent Plugin capability 原子装配测试。"""

from collections.abc import Mapping
from pathlib import Path

import pytest
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.context.providers import ContextProvider, ContextProviderRunner
from agentsystem.message.models import InjectedItem, Message
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePoint,
)
from agentsystem.plugin.assembler import assemble_agent_capabilities
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource


class _Provider:
    """测试用 ContextProvider。"""

    def __init__(self, provider_name: str, options: Mapping[str, JsonValue]) -> None:
        self.provider_name = provider_name
        self.options = dict(options)

    async def get(
        self,
        ctx: SessionContext,
        *,
        message: Message,
    ) -> InjectedItem | None:
        del ctx, message
        return None

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        del ctx, message


def _source(plugin_id: str, register: object) -> PluginSource:
    return PluginSource(
        plugin_id=plugin_id,
        origin=PluginOrigin.APPLICATION,
        path=Path(f"{plugin_id}.py"),
        register=register,  # type: ignore[arg-type]
    )


class _ExternalToolHandler:
    """测试用外部 Tool handler。"""

    async def invoke(self, **_: object) -> ToolOutput:
        """返回固定结果。"""
        return ToolOutput(content="ok")


def _external_tool(name: str) -> ToolDefinition:
    """构造最小外部 Tool。"""
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource.MCP,
        handler=_ExternalToolHandler(),
    )


def _capability_source(plugin_id: str, suffix: str = "") -> PluginSource:
    """贡献一组便于筛选的 Tool 与 Middleware。"""

    def register(context: PluginSetupContext) -> None:
        async def tool() -> ToolOutput:
            """Return a value."""
            return ToolOutput(content="ok")

        async def middleware(_: MiddlewareContext) -> MiddlewareDecision:
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

        context.register_tool(tool, name=f"search{suffix}")
        context.register_middleware(
            middleware,
            name=f"audit{suffix}",
            handler_id=f"audit{suffix}",
            point=MiddlewarePoint.BEFORE_RUN,
        )

    return _source(plugin_id, register)


def test_assembler_filters_tools_and_middlewares_with_full_match_globs() -> None:
    """筛选保持候选顺序，重叠模式去重，并覆盖外部 Tool。"""
    capabilities = assemble_agent_capabilities(
        agent_id="agent",
        sources=(
            _capability_source("application.agent.first", "_one"),
            _capability_source("application.agent.second", "_two"),
        ),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=("search_*", "*_one", "mcp_lookup"),
        enabled_middleware_patterns=("*", "*_one"),
        external_tools=(_external_tool("mcp_lookup"),),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )

    assert capabilities.tool_registry.names() == (
        "search_one",
        "search_two",
        "mcp_lookup",
    )
    assert tuple(
        item.name
        for item in capabilities.middleware_registry.resolve(
            app_id="app",
            point=MiddlewarePoint.BEFORE_RUN,
        )
    ) == ("audit_one", "audit_two")


def test_assembler_distinguishes_all_from_none_for_optional_allowlists() -> None:
    """None 选择全部候选，空列表明确关闭对应能力。"""
    source = _capability_source("application.agent.capabilities")
    all_capabilities = assemble_agent_capabilities(
        agent_id="all",
        sources=(source,),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=None,
        enabled_middleware_patterns=None,
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )
    no_capabilities = assemble_agent_capabilities(
        agent_id="none",
        sources=(source,),
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=(),
        enabled_middleware_patterns=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )

    assert all_capabilities.tool_registry.names() == ("search",)
    assert (
        len(
            all_capabilities.middleware_registry.resolve(
                app_id="app", point=MiddlewarePoint.BEFORE_RUN
            )
        )
        == 1
    )
    assert no_capabilities.tool_registry.names() == ()
    assert (
        no_capabilities.middleware_registry.resolve(
            app_id="app", point=MiddlewarePoint.BEFORE_RUN
        )
        == ()
    )


@pytest.mark.parametrize(
    ("field", "pattern"),
    (("tool", "Search*"), ("middleware", "Audit*")),
)
def test_assembler_rejects_every_unmatched_case_sensitive_pattern(
    field: str,
    pattern: str,
) -> None:
    """每个模式都必须至少命中一个大小写完全一致的候选名称。"""
    arguments: dict[str, object] = {
        "enabled_tool_patterns": None,
        "enabled_middleware_patterns": None,
    }
    arguments[f"enabled_{field}_patterns"] = (pattern,)

    with pytest.raises(ValueError, match=rf"agent.*{field}.*{pattern}"):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_capability_source("application.agent.capabilities"),),
            plugin_options={},
            context_provider_types=(),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
            **arguments,  # type: ignore[arg-type]
        )


def test_assembler_reports_all_unmatched_patterns_in_config_order() -> None:
    """零命中错误保留配置顺序并一次报告全部模式。"""
    with pytest.raises(ValueError) as captured:
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_capability_source("application.agent.capabilities"),),
            plugin_options={},
            context_provider_types=(),
            enabled_tool_patterns=("missing_two", "missing_one"),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )

    assert "patterns=missing_two,missing_one" in str(captured.value)


def test_assembler_checks_conflicts_only_after_filtering() -> None:
    """关闭的重名能力不阻止装配，选中重名能力仍启动失败。"""

    def duplicate(context: PluginSetupContext) -> None:
        async def tool() -> ToolOutput:
            """Return a value."""
            return ToolOutput(content="ok")

        async def middleware(_: MiddlewareContext) -> MiddlewareDecision:
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

        context.register_tool(tool, name="duplicate_tool")
        context.register_middleware(
            middleware,
            name="duplicate_middleware",
            handler_id="duplicate",
            point=MiddlewarePoint.BEFORE_RUN,
        )

    sources = (
        _source("application.agent.first", duplicate),
        _source("application.agent.second", duplicate),
    )
    capabilities = assemble_agent_capabilities(
        agent_id="disabled",
        sources=sources,
        plugin_options={},
        context_provider_types=(),
        enabled_tool_patterns=(),
        enabled_middleware_patterns=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )
    assert capabilities.tool_registry.names() == ()

    with pytest.raises(ValueError, match="Tool name 重复"):
        assemble_agent_capabilities(
            agent_id="enabled",
            sources=sources,
            plugin_options={},
            context_provider_types=(),
            enabled_tool_patterns=("duplicate_*",),
            enabled_middleware_patterns=(),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )

    with pytest.raises(
        ValueError,
        match=r"agent_id=enabled.*type=middleware.*name=duplicate_middleware",
    ):
        assemble_agent_capabilities(
            agent_id="enabled",
            sources=sources,
            plugin_options={},
            context_provider_types=(),
            enabled_tool_patterns=(),
            enabled_middleware_patterns=("duplicate_*",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )


def test_assembler_creates_only_selected_providers_in_config_order() -> None:
    """未选 Definition 不执行，Runner 顺序严格服从 Agent 配置。"""
    created: list[tuple[str, Mapping[str, JsonValue]]] = []

    def register(context: PluginSetupContext) -> None:
        options = context.options("shared-provider")

        def create(name: str) -> ContextProvider:
            created.append((name, options))
            return _Provider(name, options)

        context.register_context_provider("alpha", lambda: create("alpha"))
        context.register_context_provider("beta", lambda: create("beta"))
        context.register_context_provider(
            "unused",
            lambda: (_ for _ in ()).throw(AssertionError("不得创建")),
        )

    capabilities = assemble_agent_capabilities(
        agent_id="agent",
        sources=(_source("application.agent.provider", register),),
        plugin_options={"shared-provider": {"namespace": "customer"}},
        context_provider_types=("beta", "alpha"),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )

    assert capabilities.context_provider_runner.frozen is True
    assert [name for name, _ in created] == ["beta", "alpha"]
    assert [dict(options) for _, options in created] == [
        {"namespace": "customer"},
        {"namespace": "customer"},
    ]


def test_assembler_reports_both_provider_owners_before_factory_execution() -> None:
    """跨 Plugin Provider type 冲突必须带两个 owner 且不执行 factory。"""
    calls = 0

    def plugin(context: PluginSetupContext) -> None:
        def create() -> ContextProvider:
            nonlocal calls
            calls += 1
            return _Provider("shared", {})

        context.register_context_provider("shared", create)

    sources = (
        _source("application.agent.first", plugin),
        _source("application.agent.second", plugin),
    )

    with pytest.raises(
        ValueError,
        match=r"agent.*shared.*application\.agent\.first.*application\.agent\.second",
    ):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=sources,
            plugin_options={},
            context_provider_types=("shared",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )

    assert calls == 0


def test_assembler_factory_failure_does_not_freeze_captured_runner() -> None:
    """任一 factory 失败时不提交 Provider，也不发布看似可运行的 Runner。"""
    captured: list[ContextProviderRunner] = []

    def register(context: PluginSetupContext) -> None:
        captured.append(context.require(ContextProviderRunner))

        def fail() -> ContextProvider:
            raise RuntimeError("factory failed")

        context.register_context_provider("broken", fail)

    with pytest.raises(ValueError, match=r"agent.*broken.*factory"):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_source("application.agent.broken", register),),
            plugin_options={},
            context_provider_types=("broken",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )

    assert captured[0].frozen is False


def test_assembler_rejects_provider_with_synchronous_protocol_methods() -> None:
    """同名对象的同步 get/put 不能伪装成 ContextProvider。"""

    class SyncProvider:
        provider_name = "sync"

        def get(self, ctx: object, *, message: object) -> None:
            del ctx, message

        def put(self, ctx: object, *, message: object) -> None:
            del ctx, message

    def register(context: PluginSetupContext) -> None:
        context.register_context_provider(
            "sync",
            SyncProvider,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match=r"sync.*factory|factory.*sync"):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_source("application.agent.sync", register),),
            plugin_options={},
            context_provider_types=("sync",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )


def test_assembler_rejects_provider_without_put() -> None:
    """ContextProvider 必须显式实现 async put。"""

    class GetOnlyProvider:
        provider_name = "get-only"

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            del ctx, message
            return None

    def register(context: PluginSetupContext) -> None:
        context.register_context_provider(
            "get-only",
            GetOnlyProvider,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match=r"get-only.*factory|factory.*get-only"):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_source("application.agent.get-only", register),),
            plugin_options={},
            context_provider_types=("get-only",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )


def test_assembler_rejects_explicit_non_callable_put() -> None:
    """显式声明的 put 必须可调用，不能被当作缺失 hook。"""

    class InvalidPutProvider:
        provider_name = "invalid-put"
        put = None

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            del ctx, message
            return None

    def register(context: PluginSetupContext) -> None:
        context.register_context_provider(
            "invalid-put",
            InvalidPutProvider,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match=r"invalid-put.*factory|factory.*invalid-put"):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_source("application.agent.invalid-put", register),),
            plugin_options={},
            context_provider_types=("invalid-put",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )


@pytest.mark.parametrize("missing_method", ("get", "put"))
def test_assembler_rejects_inherited_protocol_placeholders(
    missing_method: str,
) -> None:
    """继承 Protocol 占位方法不等于显式实现 Provider 协议。"""

    class MissingGetProvider(ContextProvider):
        provider_name = "missing-get"

        async def put(self, ctx: SessionContext, *, message: Message) -> None:
            del ctx, message

    class MissingPutProvider(ContextProvider):
        provider_name = "missing-put"

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            del ctx, message
            return None

    provider_type = f"missing-{missing_method}"
    provider_class = (
        MissingGetProvider if missing_method == "get" else MissingPutProvider
    )

    def register(context: PluginSetupContext) -> None:
        context.register_context_provider(
            provider_type,
            provider_class,
        )

    with pytest.raises(
        ValueError, match=rf"{provider_type}.*factory|factory.*{provider_type}"
    ):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_source(f"application.agent.{provider_type}", register),),
            plugin_options={},
            context_provider_types=(provider_type,),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )


def test_assembler_registry_failure_leaves_captured_runner_unmodified() -> None:
    """后续 Middleware 校验失败也不能向 Plugin 捕获的 Runner 部分提交。"""
    captured: list[ContextProviderRunner] = []

    def register(context: PluginSetupContext) -> None:
        captured.append(context.require(ContextProviderRunner))
        context.register_context_provider(
            "memory",
            lambda: _Provider("memory", {}),
        )

        async def invalid_middleware(invocation: object) -> object:
            del invocation
            raise AssertionError("装配测试不执行 Middleware")

        context.register_middleware(
            invalid_middleware,  # type: ignore[arg-type]
            name="invalid",
            handler_id="invalid",
            point=MiddlewarePoint.BEFORE_RUN,
            timeout_ms=0,
        )

    with pytest.raises(ValueError, match="timeout_ms"):
        assemble_agent_capabilities(
            agent_id="agent",
            sources=(_source("application.agent.invalid", register),),
            plugin_options={},
            context_provider_types=("memory",),
            skill_index=SkillIndex({}),
            timeout_seconds=1.0,
            max_content_chars=1_000,
        )

    runner = captured[0]
    runner.register_many((_Provider("memory", {}),))
    assert runner.frozen is False


def test_assembler_freezes_tools_and_middleware_per_agent() -> None:
    """同一 Source 每次执行都绑定独立 Agent capability 与 options。"""
    captured: list[tuple[str, ContextProviderRunner, Mapping[str, JsonValue]]] = []

    def register(context: PluginSetupContext) -> None:
        captured.append(
            (
                context.agent_id,
                context.require(ContextProviderRunner),
                context.options("audit"),
            )
        )

        async def inspect_agent() -> ToolOutput:
            """Inspect current agent."""
            return ToolOutput(content=context.agent_id)

        async def after_run(invocation: object) -> object:
            del invocation
            raise AssertionError("装配测试不执行 Middleware")

        context.register_tool(inspect_agent)
        context.register_middleware(
            after_run,  # type: ignore[arg-type]
            name="after-run",
            handler_id="after-run",
            point=MiddlewarePoint.AFTER_RUN,
        )

    source = _source("application.shared.audit", register)
    first = assemble_agent_capabilities(
        agent_id="first",
        sources=(source,),
        plugin_options={"audit": {"tenant": "one"}},
        context_provider_types=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )
    second = assemble_agent_capabilities(
        agent_id="second",
        sources=(source,),
        plugin_options={"audit": {"tenant": "two"}},
        context_provider_types=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )

    assert first.tool_registry.frozen is True
    assert first.tool_registry.names() == ("inspect_agent",)
    assert first.middleware_registry.frozen is True
    assert first.context_provider_runner is not second.context_provider_runner
    assert [item[0] for item in captured] == ["first", "second"]
    assert [dict(item[2]) for item in captured] == [
        {"tenant": "one"},
        {"tenant": "two"},
    ]


class _MiddlewareCapability:
    """测试 Composition Root 注入的 Plugin capability。"""

    async def before_run(self, _: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)


def test_assembler_exposes_composition_root_capability_to_plugin() -> None:
    """Composition Root 注入的长寿命对象必须由 SetupContext.require 获取。"""
    service = _MiddlewareCapability()

    def register(context: PluginSetupContext) -> None:
        capability = context.require(_MiddlewareCapability)
        context.register_middleware(
            capability.before_run,
            name="before-run",
            handler_id="before-run",
            point=MiddlewarePoint.BEFORE_RUN,
        )

    capabilities = assemble_agent_capabilities(
        agent_id="agent",
        sources=(_source("application.agent.capability", register),),
        plugin_options={},
        context_provider_types=(),
        skill_index=SkillIndex({}),
        timeout_seconds=1.0,
        max_content_chars=1_000,
        plugin_capabilities=((_MiddlewareCapability, service),),
    )

    registrations = capabilities.middleware_registry.resolve(
        app_id="app",
        point=MiddlewarePoint.BEFORE_RUN,
    )
    assert len(registrations) == 1
    assert registrations[0].middleware_id == ("application.agent.capability.before-run")
