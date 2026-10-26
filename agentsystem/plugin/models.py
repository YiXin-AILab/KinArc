"""Agent Plugin 的启动期来源与临时贡献模型。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentsystem.context.providers import ContextProvider
from agentsystem.middleware.models import MiddlewareRegistration
from agentsystem.tool.models import ToolDefinition

if TYPE_CHECKING:
    from agentsystem.plugin.context import PluginSetupContext


class PluginOrigin(StrEnum):
    """标记插件来自内置包或当前 Application，决定注册工具的来源标签。"""

    BUILTIN = "builtin"
    APPLICATION = "application"


class PluginRegister(Protocol):
    """Agent 初始化时执行的同步注册协议。

    实现只能通过一次性 SetupContext 声明贡献；不得返回 awaitable 或结果值。
    注册异常由加载边界处理，运行期不再次调用此入口。
    """

    def __call__(self, context: PluginSetupContext) -> None:
        """向一次性 SetupContext 声明当前 Plugin 的贡献。

        Args:
            context: 当前 Agent 和 Plugin 专用的注册上下文，仅本次调用有效。
        """
        ...


@dataclass(frozen=True, slots=True)
class PluginSource:
    """发现阶段得到的插件来源，不执行注册或拥有运行资源。

    Attributes:
        plugin_id: 根据来源路径生成的稳定插件身份。
        origin: 内置或 Application 来源分类。
        path: 用于诊断及受控发现的源文件路径。
        register: 已检查为同步 callable 的注册入口。
    """

    plugin_id: str
    origin: PluginOrigin
    path: Path
    register: PluginRegister


@dataclass(frozen=True, slots=True)
class ContextProviderDefinition:
    """插件声明的 Provider 工厂，仅被 Agent 选择后才在装配期调用。

    Attributes:
        provider_type: 配置选择使用的类型名称。
        create: 不接收参数的同步工厂；返回值在装配边界进行协议检查。
    """

    provider_type: str
    create: Callable[[], ContextProvider]


@dataclass(frozen=True, slots=True)
class PluginContribution:
    """从一次性注册上下文提取的插件贡献，不负责运行组件或释放资源。

    注册上下文即使失败也会密封；加载入口仅返回成功注册的贡献。

    Attributes:
        plugin_id: 贡献所属插件身份。
        context_providers: 延迟 Provider 工厂声明。
        tools: 已构造的工具定义。
        middlewares: 已构造的中间件注册项。
    """

    plugin_id: str
    context_providers: tuple[ContextProviderDefinition, ...]
    tools: tuple[ToolDefinition, ...]
    middlewares: tuple[MiddlewareRegistration, ...]
