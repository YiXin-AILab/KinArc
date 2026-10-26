"""单 Plugin、单 Agent、单次注册的一次性 SetupContext。"""

from collections.abc import Callable, Mapping
from copy import deepcopy
from types import MappingProxyType

from agentsystem.common.models import JsonValue
from agentsystem.context.providers import ContextProvider
from agentsystem.middleware.models import (
    MiddlewareFailureMode,
    MiddlewareHandler,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
)
from agentsystem.plugin.models import (
    ContextProviderDefinition,
    PluginContribution,
    PluginOrigin,
)
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.models import ToolDefinition, ToolSource

_EMPTY_OPTIONS: Mapping[str, JsonValue] = MappingProxyType({})


class PluginSetupContext:
    """单 Plugin、单 Agent 的一次性启动注册上下文。

    配置在构造时深复制，服务由装配层管理；注册只累积临时贡献，不启动
    Provider 或执行工具。加载器在 register 退出时密封上下文，此后配置、
    服务查询和注册均失败；身份属性仍可读取。
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        agent_id: str,
        origin: PluginOrigin,
        options: Mapping[str, Mapping[str, JsonValue]],
        services: PluginSetupServices,
    ) -> None:
        """深复制配置并创建当前插件的空贡献缓冲区。

        Args:
            plugin_id: 当前插件的稳定身份。
            agent_id: 正在初始化的 Agent 身份。
            origin: 插件来源，用于确定工具来源标签。
            options: 按插件自定义键组织的 JSON 配置。
            services: 装配层提供的已冻结服务表，不由此上下文关闭。

        Raises:
            ValueError: plugin_id 或 agent_id 为空白。
        """
        if not plugin_id.strip() or not agent_id.strip():
            raise ValueError("plugin_id 和 agent_id 不能为空")
        self._plugin_id = plugin_id
        self._agent_id = agent_id
        self._origin = origin
        self._options = {
            key: MappingProxyType(deepcopy(dict(value)))
            for key, value in options.items()
        }
        self._services = services
        self._context_providers: dict[str, ContextProviderDefinition] = {}
        self._tools: dict[str, ToolDefinition] = {}
        self._middlewares: dict[str, MiddlewareRegistration] = {}
        self._sealed = False

    @property
    def plugin_id(self) -> str:
        """读取当前 Plugin 的稳定内部身份，密封后仍可访问。

        Returns:
            构造时传入的 plugin_id。
        """
        return self._plugin_id

    @property
    def agent_id(self) -> str:
        """读取当前装配中的 Agent 身份，密封后仍可访问。

        Returns:
            构造时传入的 agent_id。
        """
        return self._agent_id

    def options(self, key: str) -> Mapping[str, JsonValue]:
        """按插件自定义键读取与调用方隔离的配置。

        Args:
            key: 配置键，使用精确匹配。

        Returns:
            顶层不可修改的映射；未知键返回空映射。嵌套容器未递归冻结。

        Raises:
            RuntimeError: 上下文已经密封。
        """
        self._ensure_open()
        return self._options.get(key, _EMPTY_OPTIONS)

    def get[ServiceT](self, service_type: type[ServiceT]) -> ServiceT | None:
        """按精确类型键查询当前 Agent 的可选能力。

        Args:
            service_type: 装配层注册时使用的类型键。

        Returns:
            注册实例，未提供该能力时为 None。

        Raises:
            RuntimeError: 上下文已密封或服务表尚未冻结。
        """
        self._ensure_open()
        return self._services.get(service_type)

    def require[ServiceT](self, service_type: type[ServiceT]) -> ServiceT:
        """查询当前插件必需的能力，缺失时补充插件身份信息。

        Args:
            service_type: 装配层注册时使用的精确类型键。

        Returns:
            非 None 的注册实例。

        Raises:
            RuntimeError: 上下文已密封或服务表尚未冻结。
            KeyError: 当前 Agent 未提供所需能力。
        """
        self._ensure_open()
        try:
            return self._services.require(service_type)
        except KeyError as error:
            raise KeyError(
                f"Plugin {self._plugin_id} 缺少 capability：{service_type.__name__}"
            ) from error

    def register_context_provider(
        self,
        provider_type: str,
        create: Callable[[], ContextProvider],
    ) -> None:
        """记录延迟 Provider 声明，此时不执行工厂。

        Args:
            provider_type: 配置选择使用的非空类型名称。
            create: 装配选中该类型时调用的无参同步工厂。

        Raises:
            RuntimeError: 上下文已经密封。
            ValueError: 名称为空白或在本插件内重复。
            TypeError: create 不是 callable。
        """
        self._ensure_open()
        if not provider_type.strip():
            raise ValueError("ContextProvider type 不能为空")
        if not callable(create):
            raise TypeError("ContextProvider create 必须可调用")
        if provider_type in self._context_providers:
            raise ValueError(f"ContextProvider type 重复：{provider_type}")
        self._context_providers[provider_type] = ContextProviderDefinition(
            provider_type=provider_type,
            create=create,
        )

    def register_tool(
        self,
        handler: Callable[..., object],
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: Mapping[str, JsonValue] | None = None,
        rules: str | None = None,
        is_concurrency_safe: bool = True,
        can_suspend: bool = False,
    ) -> None:
        """根据函数构建工具定义并加入当前插件贡献，不执行 handler。

        Args:
            handler: 工具执行函数，供 builder 读取签名与文档。
            name: 显式工具名；省略时由 builder 推导。
            description: 显式说明；省略时由函数文档推导。
            parameters: 显式参数 schema；省略时由签名推导。
            rules: 可选工具使用规则。
            is_concurrency_safe: 是否允许与其他工具并发执行。
            can_suspend: 是否声明支持挂起。

        Raises:
            RuntimeError: 上下文已经密封。
            ValueError: 工具名称重复或工具定义不满足 builder 校验。
        """
        self._ensure_open()
        definition = build_function_tool(
            handler,
            source=_tool_source(self._origin),
            name=name,
            description=description,
            parameters=parameters,
            rules=rules,
            is_concurrency_safe=is_concurrency_safe,
            can_suspend=can_suspend,
        )
        if definition.name in self._tools:
            raise ValueError(f"Tool name 重复：{definition.name}")
        self._tools[definition.name] = definition

    def register_middleware(
        self,
        handler: MiddlewareHandler,
        *,
        name: str,
        handler_id: str,
        point: MiddlewarePoint,
        scope: MiddlewareScope = MiddlewareScope.GLOBAL,
        priority: int = 0,
        app_id: str | None = None,
        timeout_ms: int = 1_000,
        failure_mode: MiddlewareFailureMode = MiddlewareFailureMode.FAIL_OPEN,
    ) -> None:
        """构造中间件注册项，以 Plugin ID 为 handler_id 添加命名空间。

        Args:
            handler: 对应扩展点的中间件函数。
            name: Agent 筛选使用的非空短名称，不允许首尾空白。
            handler_id: 本插件内部唯一的处理器标识。
            point: 中间件触发点。
            scope: 注册项生效范围。
            priority: 注册项排序优先级。
            app_id: 应用范围所需的可选应用标识。
            timeout_ms: 单次调用期限，单位毫秒。
            failure_mode: 失败时继续或阻断的策略。

        Raises:
            RuntimeError: 上下文已经密封。
            ValueError: 名称或身份非法、handler_id 重复，或注册项组合无效。
        """
        self._ensure_open()
        if not name.strip() or name != name.strip():
            raise ValueError("Middleware name 不能为空或包含首尾空白")
        if not handler_id.strip():
            raise ValueError("Middleware handler_id 不能为空")
        if handler_id in self._middlewares:
            raise ValueError(f"Middleware handler_id 重复：{handler_id}")
        self._middlewares[handler_id] = MiddlewareRegistration(
            name=name,
            middleware_id=f"{self._plugin_id}.{handler_id}",
            point=point,
            scope=scope,
            handler=handler,
            priority=priority,
            app_id=app_id,
            timeout_ms=timeout_ms,
            failure_mode=failure_mode,
        )

    def _seal_and_extract(self) -> PluginContribution:
        """永久关闭本上下文并提取有序贡献；仅供加载边界调用一次。"""
        self._ensure_open()
        self._sealed = True
        return PluginContribution(
            plugin_id=self._plugin_id,
            context_providers=tuple(self._context_providers.values()),
            tools=tuple(self._tools.values()),
            middlewares=tuple(self._middlewares.values()),
        )

    def _ensure_open(self) -> None:
        if self._sealed:
            raise RuntimeError(f"PluginSetupContext 已密封：{self._plugin_id}")


def _tool_source(origin: PluginOrigin) -> ToolSource:
    if origin is PluginOrigin.BUILTIN:
        return ToolSource.BUILTIN
    return ToolSource.CUSTOMIZED
