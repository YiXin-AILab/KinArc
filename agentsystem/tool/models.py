"""Provider 和来源无关的 Tool 领域模型。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.llm.models import Timing
from agentsystem.message.models import ToolResultBlock


class ToolSource(StrEnum):
    """ToolDefinition 的加载来源。"""

    BUILTIN = "builtin"
    CUSTOMIZED = "customized"
    MCP = "mcp"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOutput:
    """Tool handler 的成功输出；预期失败必须抛出 ToolError。

    Attributes:
        content: 提供给模型的文本结果。
        metadata: 内部附加信息，不合并进模型可见正文。
    """

    content: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


class ToolHandler(Protocol):
    """Builtin、Customized 和 MCP handler 的统一异步端口。

    Handler 由注册方创建并交给 Executor 调用；资源生命周期由具体实现负责。
    """

    async def invoke(
        self,
        *,
        ctx: SessionContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutput:
        """执行一次已经校验的工具调用。

        Args:
            ctx: 当前会话的业务身份。
            arguments: 校验后并合并预设值的关键字参数。

        Returns:
            模型可见正文和内部元数据。

        Raises:
            ToolError: 工具遇到可向调用方解释的预期失败。
        """
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolDefinition:
    """初始化阶段注册并在运行期冻结使用的工具定义。

    定义关联 schema 与 handler，不负责执行、授权或管理 handler 资源。

    Attributes:
        name: Registry 内唯一、提供给模型的工具名。
        description: 提供给模型的用途说明。
        parameters: 模型可见参数的 JSON Schema。
        source: 工具加载来源。
        is_concurrency_safe: 是否允许与其他安全工具并发执行。
        can_suspend: 是否允许授权阶段挂起此工具。
        rules: 可选的系统提示词使用规则。
        handler: 实现 invoke 的执行对象。
        invocation_model: 参数运行期校验模型；None 表示直接传递参数。
        preset_args: 校验后注入的参数，可覆盖同名模型参数。
    """

    name: str
    description: str
    parameters: Mapping[str, JsonValue]
    source: ToolSource
    is_concurrency_safe: bool = True
    can_suspend: bool = False
    rules: str | None = None
    handler: Any = field(repr=False)
    invocation_model: type[BaseModel] | None = field(default=None, repr=False)
    preset_args: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """拒绝无法注册或展示给模型的空定义。

        Raises:
            ValueError: name 或 description 为空。
        """
        if not self.name or not self.description:
            raise ValueError("Tool name 和 description 不能为空")

    @property
    def llm_schema(self) -> dict[str, JsonValue]:
        """返回 OpenAI-compatible function tool schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionResult:
    """Executor 返回的模型可见 Block 和内部计时信息。

    Attributes:
        block: 成功或失败的工具结果，供 Loop 生成事件。
        timing: 本次执行的起止时间与耗时。
        metadata: handler 返回的内部附加信息。
    """

    block: ToolResultBlock
    timing: Timing
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSuspension:
    """授权策略请求挂起当前 ToolCall 的不可变结果。

    本对象只携带恢复信息；持久化和单次 token 消费由 Runtime 负责。

    Attributes:
        tool_call_id: 被挂起的调用标识。
        tool_name: 被挂起的工具名。
        resume_token: 恢复时需要校验的单次凭据。
        suspended_at: 挂起发生时间。
        expires_at: 可选的恢复有效期截止时间。
    """

    tool_call_id: str
    tool_name: str
    resume_token: str
    suspended_at: datetime
    expires_at: datetime | None = None
