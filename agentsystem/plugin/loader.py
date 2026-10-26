"""执行同步 Plugin register 并密封临时贡献。"""

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from agentsystem.common.models import JsonValue
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginContribution, PluginSource
from agentsystem.plugin.services import PluginSetupServices


def load_plugin_contributions(
    sources: Sequence[PluginSource],
    *,
    agent_id: str,
    options: Mapping[str, Mapping[str, JsonValue]],
    services: PluginSetupServices,
) -> tuple[PluginContribution, ...]:
    """按来源顺序同步执行全部插件，成功后返回密封贡献。

    失败不返回部分结果，但不能撤销已经执行的插件代码的外部副作用。

    Args:
        sources: 本次 Agent 初始化需要加载的插件来源。
        agent_id: 当前 Agent 身份，用于隔离注册上下文和错误定位。
        options: 每个上下文独立复制的 JSON 配置。
        services: 已冻结、由装配层管理的能力表。

    Returns:
        与 sources 顺序一致的密封贡献元组。

    Raises:
        ValueError: 插件身份重复、注册失败或注册返回值不符合协议。
    """
    identifiers = tuple(source.plugin_id for source in sources)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"Plugin ID 重复：{', '.join(identifiers)}")
    return tuple(
        _load_one(
            source,
            agent_id=agent_id,
            options=options,
            services=services,
        )
        for source in sources
    )


def _load_one(
    source: PluginSource,
    *,
    agent_id: str,
    options: Mapping[str, Mapping[str, JsonValue]],
    services: PluginSetupServices,
) -> PluginContribution:
    context = PluginSetupContext(
        plugin_id=source.plugin_id,
        agent_id=agent_id,
        origin=source.origin,
        options=options,
        services=services,
    )
    contribution: PluginContribution
    try:
        if not callable(source.register) or inspect.iscoroutinefunction(
            source.register
        ):
            raise TypeError("register 必须是同步 callable")
        register = cast(Callable[[PluginSetupContext], object], source.register)
        result = register(context)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("register 不得返回 awaitable")
        if result is not None:
            raise TypeError("register 必须返回 None")
    except Exception as error:
        raise ValueError(
            f"Agent Plugin 执行失败 agent_id={agent_id} "
            f"plugin_id={source.plugin_id} path={source.path.name} "
            f"stage=register: {type(error).__name__}"
        ) from error
    finally:
        # 失败路径也密封，防止插件保留上下文并在注册结束后继续写入。
        contribution = context._seal_and_extract()
    return contribution
