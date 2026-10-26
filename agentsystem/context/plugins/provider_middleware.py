"""注册成功终态 ContextProvider 回写 Middleware。"""

from math import ceil

from agentsystem.context.providers import ContextProviderRunner
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
    RunResultMiddlewarePayload,
)
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.runtime.state import SessionStatus


def register(context: PluginSetupContext) -> None:
    """绑定当前 Agent Runner，并注册仅处理成功终态的 AFTER_RUN handler。

    Args:
        context: 必须提供 ContextProviderRunner 能力的插件注册上下文。

    注册的 handler 使用 fail-open 策略；非成功终态直接放行，成功时并发回写 Provider。
    """
    runner = context.require(ContextProviderRunner)

    async def after_run(invocation: MiddlewareContext) -> MiddlewareDecision:
        """校验终态 payload，仅将成功保存的最终消息交给 Runner 回写。"""
        payload = invocation.payload
        if not isinstance(payload, RunResultMiddlewarePayload):
            raise TypeError("ContextProvider put 只接受 AFTER_RUN payload")
        if payload.status is not SessionStatus.SUCCEEDED:
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)
        if payload.message is None:
            raise ValueError("SUCCEEDED AFTER_RUN 缺少最终 Message")
        await runner.put(invocation.ctx, message=payload.message)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    context.register_middleware(
        after_run,
        name="provider-writeback",
        handler_id="after-run",
        point=MiddlewarePoint.AFTER_RUN,
        timeout_ms=max(1, ceil((runner.timeout_seconds + 1.0) * 1_000)),
        failure_mode=MiddlewareFailureMode.FAIL_OPEN,
    )


__all__ = ["register"]
