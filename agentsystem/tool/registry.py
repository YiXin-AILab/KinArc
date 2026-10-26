"""初始化时可写、运行时冻结的 ToolRegistry。"""

import re
from collections.abc import Iterable, Mapping

from agentsystem.common.models import JsonValue
from agentsystem.tool.models import ToolDefinition, ToolSource

_TOOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}")
_RESERVED_NAMES = frozenset({"skill_view"})


class ToolRegistry:
    """Tool schema、规则与 handler 的单一初始化权威。

    启动阶段收集定义，freeze 后仅供读取；不创建或关闭 handler 的外部资源。
    """

    def __init__(self) -> None:
        """创建空且尚未冻结的 Registry。"""
        self._definitions: dict[str, ToolDefinition] = {}
        self._frozen = False

    def register(self, definition: ToolDefinition) -> None:
        """注册唯一工具；freeze 后禁止调用。

        Args:
            definition: 包含有效名称、schema 和 handler 的定义。

        Raises:
            RuntimeError: Registry 已冻结。
            ValueError: 定义无效、名称重复或使用了不允许的保留名称。
        """
        if self._frozen:
            raise RuntimeError("ToolRegistry 已冻结")
        _validate_definition(definition)
        if definition.name in self._definitions:
            raise ValueError(f"Tool 重复注册：{definition.name}")
        self._definitions[definition.name] = definition

    def register_many(self, definitions: Iterable[ToolDefinition]) -> None:
        """在修改 Registry 前一次性校验整批定义。

        Args:
            definitions: 按注册顺序遍历的工具定义。

        Raises:
            RuntimeError: Registry 已冻结。
            ValueError: 任一定义无效或存在重复名称；整批均不写入。
        """
        if self._frozen:
            raise RuntimeError("ToolRegistry 已冻结")
        items = tuple(definitions)
        names = tuple(item.name for item in items)
        if len(names) != len(set(names)) or set(names).intersection(self._definitions):
            raise ValueError(f"Tool 重复注册：{', '.join(names)}")
        for item in items:
            _validate_definition(item)
        self._definitions.update((item.name, item) for item in items)

    def freeze(self) -> None:
        """永久关闭当前实例的写入入口。"""
        self._frozen = True

    def get(self, name: str) -> ToolDefinition | None:
        """按名称读取工具定义。

        Args:
            name: 注册时的工具名称。

        Returns:
            注册定义本身；名称不存在时返回 None。
        """
        return self._definitions.get(name)

    def names(self) -> tuple[str, ...]:
        """按注册顺序返回工具名。"""
        return tuple(self._definitions)

    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        """按注册顺序返回 LLM function schemas。"""
        return tuple(item.llm_schema for item in self._definitions.values())

    def rules(self) -> tuple[tuple[str, str], ...]:
        """按注册顺序返回非空模型使用规则。"""
        return tuple(
            (item.name, item.rules)
            for item in self._definitions.values()
            if item.rules is not None and item.rules.strip()
        )

    @property
    def frozen(self) -> bool:
        """返回 Registry 是否已冻结。"""
        return self._frozen


def _validate_definition(definition: ToolDefinition) -> None:
    if _TOOL_NAME_RE.fullmatch(definition.name) is None:
        raise ValueError(f"Tool 名称无效：{definition.name!r}")
    if (
        definition.name in _RESERVED_NAMES
        and definition.source is not ToolSource.BUILTIN
    ):
        raise ValueError(f"Tool 名称已保留：{definition.name}")
    if not callable(getattr(definition.handler, "invoke", None)):
        raise ValueError("Tool handler 必须实现 invoke")
    parameters: Mapping[str, JsonValue] = definition.parameters
    if parameters.get("type") != "object":
        raise ValueError("Tool parameters.type 必须为 object")
    properties = parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("Tool properties 必须为 mapping")
    required = parameters.get("required", [])
    if not isinstance(required, list) or not set(required).issubset(properties):
        raise ValueError("Tool required 必须是 properties 子集")
