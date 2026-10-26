"""全领域共享的严格模型、协议枚举与 JSON 值类型。"""

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def validate_identifier(value: str) -> str:
    """拒绝尾部 ASCII 空格，避免存储比较规则合并不同身份。

    Args:
        value: 原始身份，不做 strip 或 Unicode 规范化。

    Returns:
        校验通过的原值。

    Raises:
        ValueError: 身份以 ASCII 空格结尾。
    """
    if value.endswith(" "):
        raise ValueError("Identifier must not end with a trailing space")
    return value


type IdentityText = Annotated[
    str, Field(min_length=1), AfterValidator(validate_identifier)
]


class FinishReason(StrEnum):
    """归一化后的 LLM 响应结束原因。

    区分正常停止、长度上限、工具调用、内容过滤、错误及中止，避免 SDK 枚举外泄。
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    ABORTED = "aborted"


class OutputMode(StrEnum):
    """公开输出的交付方式，由 Agent 配置确定并保存到会话状态。

    STREAM 表示流式交付，COMPLETE 表示完整结果交付；不决定模型内部调用方式。
    """

    STREAM = "stream"
    COMPLETE = "complete"


class StrictModel(BaseModel):
    """拒绝未知字段的共享 Pydantic 边界模型。

    此基类只设置 extra=forbid；字段是否允许类型转换由各字段声明决定。
    """

    model_config = ConfigDict(extra="forbid")


class FrozenModel(StrictModel):
    """禁止字段重新赋值的运行事实模型。

    继承未知字段校验；冻结不递归覆盖字段内部的可变容器。
    """

    model_config = ConfigDict(frozen=True)


class SessionContext(FrozenModel):
    """在领域模块之间传递会话身份，不持有连接或运行状态。

    Attributes:
        app_id: 应用隔离标识。
        user_id: 应用内用户标识。
        agent_id: 执行该会话的 Agent 标识。
        session_id: 会话标识，与其余三个字段共同构成完整作用域。
    """

    app_id: IdentityText
    user_id: IdentityText
    agent_id: IdentityText
    session_id: IdentityText


type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]


__all__ = [
    "FinishReason",
    "FrozenModel",
    "IdentityText",
    "JsonPrimitive",
    "JsonValue",
    "OutputMode",
    "SessionContext",
    "StrictModel",
]
