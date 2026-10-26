"""进程内普通 Tool 的只读 Runtime 调用上下文。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from agentsystem.common.models import SessionContext


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvocationContext:
    """一次普通 Tool 调用可读取的只读 Runtime 关联上下文。

    Executor 在 handler 执行期间绑定该对象，退出时恢复原上下文。

    Attributes:
        session_context: 当前会话的业务身份。
        run_id: 已由 Session 创建的内部 run 标识。
        runtime_trace_id: Runtime trace 标识，不是 OpenTelemetry TraceId。
        tool_call_id: 本次调用标识。
        tool_name: 被调用的工具名。
        turn_index: 本次调用所属的 turn 序号。
    """

    session_context: SessionContext
    run_id: str
    runtime_trace_id: str
    tool_call_id: str
    tool_name: str
    turn_index: int


_CURRENT_TOOL_CONTEXT: ContextVar[ToolInvocationContext | None] = ContextVar(
    "agentsystem_tool_context",
    default=None,
)


def get_tool_context() -> ToolInvocationContext:
    """返回当前普通 Tool 的调用上下文。

    Returns:
        当前执行链绑定的只读上下文。

    Raises:
        RuntimeError: 当前执行链不在普通 Tool 调用上下文中。
    """
    context = _CURRENT_TOOL_CONTEXT.get()
    if context is None:
        raise RuntimeError("get_tool_context() 只能在普通 Tool 调用期间使用")
    return context


@contextmanager
def _bind_tool_context(context: ToolInvocationContext) -> Iterator[None]:
    """在当前执行链绑定上下文，并在退出时恢复进入前的值。"""
    token = _CURRENT_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_TOOL_CONTEXT.reset(token)
