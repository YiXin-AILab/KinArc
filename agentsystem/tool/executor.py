"""参数校验、授权、timeout 和错误归一化的单 Tool Executor。"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from pydantic import ValidationError

from agentsystem.common.errors import (
    ToolError,
    new_error_id,
)
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.llm.models import Timing
from agentsystem.message.models import ToolCallBlock, ToolResultBlock
from agentsystem.tool.authorization import (
    AllowAllToolAuthorization,
    ToolAuthorization,
    ToolAuthorizationDecisionType,
)
from agentsystem.tool.context import ToolInvocationContext, _bind_tool_context
from agentsystem.tool.models import ToolExecutionResult, ToolOutput, ToolSuspension
from agentsystem.tool.registry import ToolRegistry

LOGGER = logging.getLogger(__name__)


class ToolExecutor:
    """一次只执行一个 ToolCall 的异步执行器。

    依赖已冻结的 Registry 和授权策略；仅 handler 调用受本执行器的超时保护。
    不负责持久化结果或恢复挂起调用，取消异常继续向上传播。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        authorization: ToolAuthorization | None = None,
        timeout_seconds: float = 30.0,
        error_id_factory: Callable[[], str] = new_error_id,
    ) -> None:
        """要求冻结 Registry，并默认使用全放行授权。

        Args:
            registry: 已完成启动期注册的工具集合。
            authorization: 调用前授权策略，缺省时全部放行。
            timeout_seconds: 单个 handler 执行的秒数上限。
            error_id_factory: 未预期错误的关联标识工厂。

        Raises:
            ValueError: Registry 未冻结或 timeout 不为正数。
        """
        if not registry.frozen:
            raise ValueError("ToolRegistry 必须先 freeze")
        if timeout_seconds <= 0:
            raise ValueError("Tool timeout_seconds 必须为正数")
        self._registry = registry
        self._authorization = authorization or AllowAllToolAuthorization()
        self._timeout_seconds = timeout_seconds
        self._error_id_factory = error_id_factory

    async def execute(
        self,
        context: ToolInvocationContext,
        call: ToolCallBlock,
    ) -> ToolExecutionResult | ToolSuspension:
        """校验、授权并执行调用；handler 普通失败转换为 error ToolResult。

        Args:
            context: handler 执行期间绑定的调用上下文。
            call: 包含工具名、调用 ID 和已解析参数的调用块。

        Returns:
            带计时的成功或错误结果，或尚未执行 handler 的挂起信息。

        授权策略异常直接传播；handler 未预期异常在此记录一次完整堆栈。
        """
        ctx = context.session_context
        started_at = datetime.now(UTC)
        started = monotonic()
        definition = self._registry.get(call.tool_name)
        if definition is None:
            return _error_result(
                call,
                "unknown_tool",
                "unknown_tool",
                started_at,
                started,
            )
        arguments = _validated_arguments(call, definition.invocation_model)
        if isinstance(arguments, str):
            return _error_result(
                call,
                arguments,
                "invalid_tool_arguments",
                started_at,
                started,
            )
        decision = await self._authorization.authorize(ctx, call, definition)
        if decision.type is ToolAuthorizationDecisionType.DENY:
            return _error_result(
                call,
                decision.reason or "tool_denied",
                "tool_denied",
                started_at,
                started,
            )
        if decision.type is ToolAuthorizationDecisionType.SUSPEND:
            if not definition.can_suspend or decision.resume_token is None:
                return _error_result(
                    call,
                    "tool_cannot_suspend",
                    "tool_cannot_suspend",
                    started_at,
                    started,
                )
            return ToolSuspension(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                resume_token=decision.resume_token,
                suspended_at=datetime.now(UTC),
                expires_at=decision.expires_at,
            )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                with _bind_tool_context(context):
                    output = await definition.handler.invoke(
                        ctx=ctx,
                        arguments={**arguments, **definition.preset_args},
                    )
        except TimeoutError:
            return _error_result(
                call,
                "tool_timeout",
                "tool_timeout",
                started_at,
                started,
            )
        except ToolError as error:
            return _error_result(
                call,
                error.message,
                error.code,
                started_at,
                started,
            )
        except Exception as error:
            error_id = self._error_id_factory()
            log_unexpected_exception(
                LOGGER,
                "tool_execution_failed",
                error,
                operation="tool.execute",
                error_id=error_id,
                tool_name=call.tool_name,
            )
            return _error_result(
                call,
                f"工具执行失败，error_id={error_id}",
                "tool_error",
                started_at,
                started,
            )
        return _success_result(call, output, started_at, started)


def _validated_arguments(
    call: ToolCallBlock,
    invocation_model: type[Any] | None,
) -> dict[str, Any] | str:
    if call.arguments is None:
        return call.parse_error or "invalid_tool_arguments"
    if invocation_model is None:
        return dict(call.arguments)
    try:
        validated = invocation_model.model_validate(call.arguments)
    except ValidationError as error:
        return f"invalid_tool_arguments: {error.errors(include_url=False)}"
    return dict(validated.model_dump())


def _success_result(
    call: ToolCallBlock,
    output: ToolOutput,
    started_at: datetime,
    started: float,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        block=ToolResultBlock(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            content=output.content,
        ),
        timing=_timing(started_at, started),
        metadata=output.metadata,
    )


def _error_result(
    call: ToolCallBlock,
    message: str,
    error_code: str,
    started_at: datetime,
    started: float,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        block=ToolResultBlock(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            content=message,
            is_error=True,
            error_code=error_code,
        ),
        timing=_timing(started_at, started),
    )


def _timing(started_at: datetime, started: float) -> Timing:
    return Timing(
        started_at=started_at,
        ended_at=datetime.now(UTC),
        duration_ms=max(0.0, (monotonic() - started) * 1_000),
    )
