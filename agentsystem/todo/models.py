"""Todo 条目、边界校验与完整清单序列化。"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictStr, TypeAdapter, field_validator

from agentsystem.common.models import SessionContext, StrictModel, validate_identifier


class TodoItem(StrictModel):
    """没有独立身份的不可变进度条目，不驱动 Runtime 执行。

    Attributes:
        content: 保留原始空白的合法 UTF-8 工作描述，长度为 1–500 字符。
        status: pending、in_progress 或 completed；不表示会话运行状态。
    """

    model_config = ConfigDict(frozen=True)
    content: StrictStr = Field(min_length=1, max_length=500, description="工作内容")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="未开始、进行中或已完成"
    )

    @field_validator("content")
    @classmethod
    def nonblank_utf8(cls, value: str) -> str:
        """校验正文并保留原始空白。

        Args:
            value: 待校验或编码的原始文本。

        Returns:
            未经裁剪或规范化的输入原文。

        Raises:
            ValueError: 正文为空白或无法编码为 UTF-8。
        """
        value.encode("utf-8")
        if not value.strip():
            raise ValueError("Todo content must contain non-whitespace text")
        return value


type TodoList = Annotated[list[TodoItem], Field(max_length=50, strict=True)]
_ITEMS: TypeAdapter[list[TodoItem]] = TypeAdapter(TodoList)


def validate_identity(ctx: SessionContext) -> tuple[str, str, str, str]:
    """只校验四个定位字段，不查询会话。

    Args:
        ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。

    Returns:
        应用、用户、Agent、会话四个定位字段。

    Raises:
        ValueError: 身份为空、编码不合法或不满足定位规则。
    """
    identity = (ctx.app_id, ctx.user_id, ctx.agent_id, ctx.session_id)
    for value in identity:
        if not isinstance(value, str) or not 1 <= len(value) <= 191:
            raise ValueError("Todo identity must be 1–191 characters")
        validate_identifier(value)
        value.encode("utf-8")
    return identity


def validate_todos(todos: tuple[TodoItem, ...]) -> tuple[TodoItem, ...]:
    """在存储边界重新校验并创建不共享可变状态的条目。

    Args:
        todos: 按展示顺序排列的完整清单，空清单表示清空。

    Returns:
        校验后的完整条目序列；写入方法返回本次提交内容。
    """
    return tuple(_ITEMS.validate_python([item.model_dump() for item in todos]))


def encode_todos(todos: tuple[TodoItem, ...]) -> str:
    """序列化完整数组，不进行 HTML/XML 转义。

    Args:
        todos: 按展示顺序排列的完整清单，空清单表示清空。

    Returns:
        保留条目顺序的完整 JSON 数组文本。
    """
    return _ITEMS.dump_json(list(todos)).decode("utf-8")


def decode_todos(value: str | bytes) -> tuple[TodoItem, ...]:
    """解析并验证持久化清单，损坏数据由 adapter 报错。

    Args:
        value: 完整 JSON 文本或 UTF-8 字节。

    Returns:
        校验后的完整条目序列；写入方法返回本次提交内容。

    Raises:
        ValidationError: JSON、字段或清单长度不符合约束。
    """
    return tuple(_ITEMS.validate_json(value))
