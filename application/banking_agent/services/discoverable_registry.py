"""Banking 隐藏工具注册与参数调用。"""

import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import UnionType
from typing import (
    Literal,
    ParamSpec,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import TypeAdapter, ValidationError

_DISCOVERABLE_KIND_ATTR = "__banking_discoverable_kind__"
_DISCOVERABLE_NAME_ATTR = "__banking_discoverable_name__"

_PARAMS = ParamSpec("_PARAMS")


class DiscoverableToolKind(StrEnum):
    """隐藏工具对业务状态的访问类型。"""

    READ = "read"
    WRITE = "write"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class DiscoverableToolDefinition:
    """一个可由通用入口调用的隐藏工具定义。"""

    name: str
    method_name: str
    kind: DiscoverableToolKind
    description: str
    signature: inspect.Signature
    parameter_types: Mapping[str, object]


def discoverable_tool(
    kind: DiscoverableToolKind,
    *,
    name: str | None = None,
) -> Callable[[Callable[_PARAMS, str]], Callable[_PARAMS, str]]:
    """标记一个只通过通用入口暴露的 Banking 工具。"""

    def decorate(method: Callable[_PARAMS, str]) -> Callable[_PARAMS, str]:
        setattr(method, _DISCOVERABLE_KIND_ATTR, kind)
        setattr(method, _DISCOVERABLE_NAME_ATTR, name or method.__name__)
        return method

    return decorate


def build_discoverable_registry(
    owner_type: type[object],
) -> dict[str, DiscoverableToolDefinition]:
    """从类方法构建并验证不可重复的隐藏工具注册表。"""
    registry: dict[str, DiscoverableToolDefinition] = {}
    for method_name, method in inspect.getmembers(owner_type, inspect.isfunction):
        kind = getattr(method, _DISCOVERABLE_KIND_ATTR, None)
        if not isinstance(kind, DiscoverableToolKind):
            continue
        tool_name = getattr(method, _DISCOVERABLE_NAME_ATTR, method_name)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("隐藏工具名称不能为空")
        if tool_name in registry:
            raise ValueError(f"隐藏工具名称重复: {tool_name}")
        registry[tool_name] = _build_definition(
            tool_name=tool_name,
            method_name=method_name,
            kind=kind,
            method=method,
        )
    return registry


def parse_discoverable_arguments(raw_arguments: str) -> dict[str, object]:
    """把通用入口的 JSON 参数解析为对象。"""
    try:
        parsed = json.loads(raw_arguments, parse_int=float) if raw_arguments else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"无效 JSON 参数: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("隐藏工具参数必须是 JSON object")
    if not all(isinstance(key, str) for key in parsed):
        raise ValueError("隐藏工具参数名称必须是字符串")
    return parsed


def invoke_discoverable_tool(
    definition: DiscoverableToolDefinition,
    owner: object,
    arguments: Mapping[str, object],
    *,
    strict_types: bool = True,
) -> str:
    """校验参数并调用一个真实隐藏工具方法。"""
    method = getattr(owner, definition.method_name)
    try:
        bound = definition.signature.bind(**arguments)
    except TypeError as error:
        raise ValueError(f"隐藏工具参数不匹配: {error}") from error
    bound.apply_defaults()

    validated = dict(bound.arguments)
    if strict_types:
        for parameter_name, value in bound.arguments.items():
            annotation = definition.parameter_types[parameter_name]
            try:
                validated[parameter_name] = TypeAdapter(annotation).validate_python(
                    value,
                    strict=True,
                )
            except ValidationError as error:
                raise ValueError(f"隐藏工具参数类型错误: {parameter_name}") from error

    result = method(**validated)
    if not isinstance(result, str):
        raise TypeError(f"隐藏工具 {definition.name} 必须返回 str")
    return result


def _build_definition(
    *,
    tool_name: str,
    method_name: str,
    kind: DiscoverableToolKind,
    method: Callable[..., str],
) -> DiscoverableToolDefinition:
    """验证单个方法并生成注册表定义。"""
    signature = inspect.signature(method)
    type_hints = get_type_hints(method)
    return_type = type_hints.get("return")
    if return_type is not str:
        raise TypeError(f"隐藏工具 {tool_name} 必须声明 str 返回类型")

    parameters = list(signature.parameters.values())
    if not parameters or parameters[0].name != "self":
        raise TypeError(f"隐藏工具 {tool_name} 必须是实例方法")

    callable_parameters: list[inspect.Parameter] = []
    parameter_types: dict[str, object] = {}
    for parameter in parameters[1:]:
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            raise TypeError(f"隐藏工具 {tool_name} 使用了不支持的参数形式")
        annotation = type_hints.get(parameter.name)
        if not _is_supported_annotation(annotation):
            raise TypeError(
                f"隐藏工具 {tool_name} 参数 {parameter.name} 使用了不支持的类型"
            )
        callable_parameters.append(parameter)
        parameter_types[parameter.name] = annotation

    callable_signature = signature.replace(parameters=callable_parameters)
    return DiscoverableToolDefinition(
        name=tool_name,
        method_name=method_name,
        kind=kind,
        description=inspect.getdoc(method) or "",
        signature=callable_signature,
        parameter_types=parameter_types,
    )


def _is_supported_annotation(annotation: object) -> bool:
    """判断类型是否可由 JSON 值严格校验。"""
    if annotation in {str, int, float, bool, type(None)}:
        return True
    origin = get_origin(annotation)
    if origin is Literal:
        return bool(get_args(annotation)) and all(
            isinstance(value, (str, int, float, bool)) for value in get_args(annotation)
        )
    if origin in {Union, UnionType}:
        return all(_is_supported_annotation(item) for item in get_args(annotation))
    return False
