"""将单 Agent Plugin 贡献原子装配为冻结的运行容器。"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import cast

from agentsystem.common.models import JsonValue
from agentsystem.context.providers import ContextProvider, ContextProviderRunner
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.models import (
    ContextProviderDefinition,
    PluginContribution,
    PluginSource,
)
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.models import ToolDefinition
from agentsystem.tool.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class AgentPluginCapabilities:
    """单 Agent 装配完成后交给运行时的能力容器。

    dataclass 字段不可重赋值，内部注册表已由装配函数冻结；此对象不负责
    驱动运行或关闭能力所依赖的外部资源。

    Attributes:
        context_provider_runner: 按配置顺序注册 Provider 的执行器。
        tool_registry: 包含最终选中工具的冻结注册表。
        middleware_registry: 包含最终选中中间件的冻结注册表。
    """

    context_provider_runner: ContextProviderRunner
    tool_registry: ToolRegistry
    middleware_registry: MiddlewareRegistry


def assemble_agent_capabilities(
    *,
    agent_id: str,
    sources: Sequence[PluginSource],
    plugin_options: Mapping[str, Mapping[str, JsonValue]],
    context_provider_types: Sequence[str],
    skill_index: SkillIndex,
    timeout_seconds: float,
    max_content_chars: int,
    external_tools: Sequence[ToolDefinition] = (),
    plugin_capabilities: Sequence[tuple[type[object], object]] = (),
    enabled_tool_patterns: Sequence[str] | None = None,
    enabled_middleware_patterns: Sequence[str] | None = None,
) -> AgentPluginCapabilities:
    """执行单 Agent 插件注册，完整校验后返回三个冻结容器。

    失败时不返回部分容器，但不回滚插件注册函数或工厂的外部副作用。

    Args:
        agent_id: 正在初始化的 Agent 身份。
        sources: 按执行顺序排列的插件来源。
        plugin_options: 插件自定义键对应的 JSON 配置。
        context_provider_types: 需要构建的 Provider 类型及顺序。
        skill_index: 当前 Agent 的只读 Skill 索引能力。
        timeout_seconds: Provider 执行器使用的调用期限。
        max_content_chars: Provider 执行器使用的内容字符限制。
        external_tools: 同插件工具合并的外部工具定义。
        plugin_capabilities: 附加的精确类型键与服务实例。
        enabled_tool_patterns: 工具筛选模式；None 全选，空序列不选。
        enabled_middleware_patterns: 中间件筛选模式，空值语义同工具。

    Returns:
        完整构建并冻结的 Agent 能力集合。

    Raises:
        ValueError: 注册、工厂或名称校验失败，或筛选模式没有匹配项。
        KeyError: 选中的 Provider 类型没有声明。
    """
    runner = ContextProviderRunner(
        timeout_seconds=timeout_seconds,
        max_content_chars=max_content_chars,
    )
    services = PluginSetupServices()
    services.register(ContextProviderRunner, runner)
    services.register(SkillIndex, skill_index)
    for service_type, value in plugin_capabilities:
        services.register(service_type, value)
    services.freeze()
    contributions = load_plugin_contributions(
        sources,
        agent_id=agent_id,
        options=plugin_options,
        services=services,
    )
    providers = _create_selected_providers(
        agent_id,
        context_provider_types,
        contributions,
    )
    tools = _merge_tool_definitions(
        agent_id,
        contributions,
        external_tools,
        enabled_tool_patterns,
    )
    middlewares = _select_matching_items(
        agent_id,
        "middleware",
        enabled_middleware_patterns,
        tuple(
            (middleware.name, middleware)
            for contribution in contributions
            for middleware in contribution.middlewares
        ),
    )

    tool_registry = ToolRegistry()
    middleware_registry = MiddlewareRegistry()
    tool_registry.register_many(tools)
    try:
        middleware_registry.register_many(middlewares)
    except ValueError as error:
        raise ValueError(
            f"Middleware 装配失败 agent_id={agent_id} type=middleware error={error}"
        ) from error
    runner.register_many(providers)
    runner.freeze()
    tool_registry.freeze()
    middleware_registry.freeze()
    return AgentPluginCapabilities(
        context_provider_runner=runner,
        tool_registry=tool_registry,
        middleware_registry=middleware_registry,
    )


def _create_selected_providers(
    agent_id: str,
    selected_types: Sequence[str],
    contributions: Sequence[PluginContribution],
) -> tuple[ContextProvider, ...]:
    """先检查全部声明的名称冲突，再仅实例化配置选中的 Provider。"""
    definitions: dict[str, tuple[ContextProviderDefinition, str]] = {}
    for contribution in contributions:
        for definition in contribution.context_providers:
            existing = definitions.get(definition.provider_type)
            if existing is not None:
                raise ValueError(
                    "ContextProvider type 重复 "
                    f"agent_id={agent_id} type={definition.provider_type} "
                    f"owners={existing[1]},{contribution.plugin_id}"
                )
            definitions[definition.provider_type] = (
                definition,
                contribution.plugin_id,
            )

    providers: list[ContextProvider] = []
    for provider_type in selected_types:
        item = definitions.get(provider_type)
        if item is None:
            raise KeyError(
                f"ContextProvider Definition 缺失 agent_id={agent_id} "
                f"type={provider_type}"
            )
        definition, owner = item
        providers.append(
            _create_provider(
                agent_id=agent_id,
                provider_type=provider_type,
                owner=owner,
                definition=definition,
            )
        )
    return tuple(providers)


def _create_provider(
    *,
    agent_id: str,
    provider_type: str,
    owner: str,
    definition: ContextProviderDefinition,
) -> ContextProvider:
    """同步调用工厂并检查 Provider 名称和异步方法，将错误归属到插件。"""
    try:
        candidate = definition.create()
        if inspect.isawaitable(candidate):
            if inspect.iscoroutine(candidate):
                candidate.close()
            raise TypeError("ContextProvider factory 不得返回 awaitable")
        provider = cast(object, candidate)
        name = getattr(provider, "provider_name", None)
        if name != provider_type:
            raise TypeError("ContextProvider provider_name 与 type 不匹配")
        get = getattr(provider, "get", None)
        put = getattr(provider, "put", None)
        if (
            not callable(get)
            or not callable(put)
            or not inspect.iscoroutinefunction(get)
            or not inspect.iscoroutinefunction(put)
        ):
            raise TypeError("ContextProvider 必须实现 async get/put")
        return cast(ContextProvider, provider)
    except Exception as error:
        raise ValueError(
            f"ContextProvider factory 失败 agent_id={agent_id} "
            f"type={provider_type} owner={owner} stage=factory "
            f"error={type(error).__name__}"
        ) from error


def _merge_tool_definitions(
    agent_id: str,
    contributions: Sequence[PluginContribution],
    external_tools: Sequence[ToolDefinition],
    enabled_patterns: Sequence[str] | None,
) -> tuple[ToolDefinition, ...]:
    candidates = tuple(
        (definition, contribution.plugin_id)
        for contribution in contributions
        for definition in contribution.tools
    ) + tuple((definition, "external") for definition in external_tools)
    selected = _select_matching_items(
        agent_id,
        "tool",
        enabled_patterns,
        tuple(
            (definition.name, (definition, owner)) for definition, owner in candidates
        ),
    )
    owners: dict[str, str] = {}
    items: list[ToolDefinition] = []
    for definition, owner in selected:
        _claim_tool_name(agent_id, definition.name, owner, owners)
        items.append(definition)
    return tuple(items)


def _select_matching_items[ItemT](
    agent_id: str,
    capability: str,
    patterns: Sequence[str] | None,
    candidates: Sequence[tuple[str, ItemT]],
) -> tuple[ItemT, ...]:
    """按候选顺序返回 full-match glob 命中的能力。"""
    if patterns is None:
        return tuple(item for _, item in candidates)
    missing = tuple(
        pattern
        for pattern in patterns
        if not any(fnmatchcase(name, pattern) for name, _ in candidates)
    )
    if missing:
        raise ValueError(
            f"Agent capability pattern 未匹配 agent_id={agent_id} "
            f"type={capability} patterns={','.join(missing)}"
        )
    return tuple(
        item
        for name, item in candidates
        if any(fnmatchcase(name, pattern) for pattern in patterns)
    )


def _claim_tool_name(
    agent_id: str,
    tool_name: str,
    owner: str,
    owners: dict[str, str],
) -> None:
    existing = owners.get(tool_name)
    if existing is not None:
        raise ValueError(
            f"Tool name 重复 agent_id={agent_id} name={tool_name} "
            f"owners={existing},{owner}"
        )
    owners[tool_name] = owner
