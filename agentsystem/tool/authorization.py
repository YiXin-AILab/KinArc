"""可替换的 Tool 授权与挂起决策协议。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

from agentsystem.common.models import SessionContext
from agentsystem.message.models import ToolCallBlock
from agentsystem.tool.models import ToolDefinition


class ToolAuthorizationDecisionType(StrEnum):
    """工具执行前授权决策。"""

    ALLOW = "allow"
    DENY = "deny"
    SUSPEND = "suspend"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolAuthorizationDecision:
    """授权策略返回的不可变决策，不自行执行工具或保存挂起状态。

    Attributes:
        type: 放行、拒绝或挂起。
        reason: 拒绝时提供给工具结果的可选原因。
        resume_token: 挂起时用于后续恢复的凭据。
        expires_at: 可选的恢复有效期截止时间。
    """

    type: ToolAuthorizationDecisionType
    reason: str | None = None
    resume_token: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def allow(cls) -> Self:
        """构造放行决策。"""
        return cls(type=ToolAuthorizationDecisionType.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> Self:
        """构造拒绝决策。

        Args:
            reason: 可对模型公开的拒绝原因。

        Returns:
            不携带恢复凭据的 DENY 决策。
        """
        return cls(type=ToolAuthorizationDecisionType.DENY, reason=reason)

    @classmethod
    def suspend(cls, resume_token: str) -> Self:
        """构造挂起决策。

        Args:
            resume_token: 后续恢复需要校验的单次凭据。

        Returns:
            未指定过期时间的 SUSPEND 决策。
        """
        return cls(
            type=ToolAuthorizationDecisionType.SUSPEND,
            resume_token=resume_token,
        )


class ToolAuthorization(Protocol):
    """ToolExecutor 调用前的异步授权端口。

    实现负责自己的外部 I/O 超时；Executor 在参数校验后调用本协议。
    """

    async def authorize(
        self,
        ctx: SessionContext,
        call: ToolCallBlock,
        definition: ToolDefinition,
    ) -> ToolAuthorizationDecision:
        """决定放行、拒绝或挂起。

        Args:
            ctx: 当前会话的业务身份。
            call: 原始工具调用块。
            definition: 已注册定义及其挂起能力。

        Returns:
            供 Executor 应用的授权决策。
        """
        ...


class AllowAllToolAuthorization:
    """V1 无状态默认策略：所有已注册且参数合法的工具调用均放行。

    不读取配置、不进行外部 I/O，也不持有需要清理的资源。
    """

    async def authorize(
        self,
        ctx: SessionContext,
        call: ToolCallBlock,
        definition: ToolDefinition,
    ) -> ToolAuthorizationDecision:
        """忽略业务内容并返回 ALLOW。

        Args:
            ctx: 为满足授权协议接收的会话身份。
            call: 为满足授权协议接收的调用块。
            definition: 为满足授权协议接收的工具定义。

        Returns:
            无附加原因或恢复凭据的放行决策。
        """
        del ctx, call, definition
        return ToolAuthorizationDecision.allow()
