"""把普通函数构建为经过 schema 校验的 ToolDefinition。"""

import asyncio
import inspect
from collections.abc import Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, create_model

from agentsystem.common.models import JsonValue
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource


class _FunctionToolHandler:
    """把普通已绑定函数适配为 ToolHandler，不改变函数自身的资源生命周期。

    同步函数在当前线程直接执行；可等待返回值会被 await。
    """

    def __init__(self, function: Callable[..., object]) -> None:
        """保存待执行函数，不在构造时调用它。

        Args:
            function: 接收关键字参数并返回 ToolOutput 或其 awaitable 的函数。
        """
        self._function = function
        self._is_coroutine_function = inspect.iscoroutinefunction(function)

    async def invoke(
        self,
        *,
        ctx: object,
        arguments: Mapping[str, Any],
    ) -> ToolOutput:
        """使用已校验参数调用函数并强制标准返回类型。

        同步函数统一经 asyncio.to_thread 隔离到工作线程，避免同步文件 I/O
        阻塞事件循环并使外围 timeout 失效；取消只放弃等待，不会终止已经
        在线程中运行的函数，因此同步 handler 的写入必须自行保证原子性。
        异步函数继续在当前事件循环中直接 await。

        Args:
            ctx: 统一接口传入的上下文，此 adapter 不向函数参数注入它。
            arguments: 直接展开给函数的关键字参数。

        Returns:
            函数返回的 ToolOutput。

        Raises:
            TypeError: 函数返回值不是 ToolOutput。

        函数自身抛出的异常直接传播给 Executor。
        """
        del ctx
        if self._is_coroutine_function:
            result = self._function(**arguments)
        else:
            result = await asyncio.to_thread(self._function, **arguments)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolOutput):
            raise TypeError("Tool handler 必须返回 ToolOutput")
        return result


def build_function_tool(
    handler: Callable[..., object],
    *,
    source: ToolSource,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, JsonValue] | None = None,
    rules: str | None = None,
    is_concurrency_safe: bool = True,
    can_suspend: bool = False,
) -> ToolDefinition:
    """校验函数签名和 schema，并返回可注册的 Tool 定义。

    Args:
        handler: 接收关键字参数并返回 ToolOutput 的同步或异步函数。
        source: 工具的加载来源。
        name: 显式工具名；缺省时使用函数名。
        description: 用途说明；缺省时读取函数 docstring 首行。
        parameters: 显式 JSON Schema；缺省时从签名生成。
        rules: 注入 SYSTEM 的可选使用规则。
        is_concurrency_safe: 工具是否允许并发调度。
        can_suspend: 工具是否允许授权挂起。

    Returns:
        包含参数校验模型与函数 adapter、尚未注册的工具定义。

    Raises:
        ValueError: 缺少说明、签名不受支持或 schema 参数名不匹配。
    """
    effective_name = name or handler.__name__
    effective_description = description or _description(handler)
    model = _invocation_model(handler, effective_name)
    generated = cast(Mapping[str, JsonValue], model.model_json_schema())
    effective_parameters = parameters or generated
    _validate_explicit_parameters(effective_parameters, model)
    return ToolDefinition(
        name=effective_name,
        description=effective_description,
        parameters=effective_parameters,
        source=source,
        is_concurrency_safe=is_concurrency_safe,
        can_suspend=can_suspend,
        rules=rules,
        handler=_FunctionToolHandler(handler),
        invocation_model=model,
    )


def _description(function: Callable[..., object]) -> str:
    docstring = inspect.getdoc(function) or ""
    first_line = docstring.splitlines()[0].strip() if docstring else ""
    if not first_line:
        raise ValueError("Tool 必须提供 description 或 docstring")
    return first_line


def _invocation_model(
    function: Callable[..., object],
    name: str,
) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for parameter in inspect.signature(function).parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        }:
            raise ValueError("Tool 不支持 *args/**kwargs 或位置专用参数")
        if parameter.annotation is inspect.Parameter.empty:
            raise ValueError(f"Tool 参数 {parameter.name!r} 缺少类型注解")
        default = (
            ... if parameter.default is inspect.Parameter.empty else parameter.default
        )
        fields[parameter.name] = (parameter.annotation, default)
    model_name = "".join(character for character in name.title() if character.isalnum())
    return cast(
        type[BaseModel],
        create_model(
            f"{model_name}Arguments",
            __config__=ConfigDict(extra="forbid", frozen=True),
            **fields,
        ),
    )


def _validate_explicit_parameters(
    parameters: Mapping[str, JsonValue],
    model: type[BaseModel],
) -> None:
    if parameters.get("type") != "object":
        raise ValueError("Tool parameters.type 必须为 object")
    properties = parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("Tool parameters.properties 必须为 mapping")
    if set(properties) != set(model.model_fields):
        raise ValueError("Tool Schema 与 handler 可见参数不一致")
