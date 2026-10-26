"""公开事件投影协议与内置日志实现。"""

import logging
from collections.abc import Mapping
from typing import Protocol

from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.outbound.mapper import to_public_run_result
from agentsystem.runtime.events import AgentEventEnvelope
from agentsystem.runtime.state import SessionStatus

_EVENT_NAME_BY_STATUS = {
    SessionStatus.PENDING: "RunSuspended",
    SessionStatus.SUCCEEDED: "RunCompleted",
    SessionStatus.FAILED: "RunFailed",
    SessionStatus.CANCELED: "RunCanceled",
}


class PublicProjector(Protocol):
    """AgentSession 固定投影序列中最后一个公开输出端口。

    实现自行选择要公开的事件和交付方式；异常必须传播，由 Session 执行 fail-close。
    本端口不是消息或执行状态的查询权威，也不定义通用业务回调协议。
    """

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        """处理一个完整内部事件。

        Args:
            ctx: 当前租户、Agent 和 session 的隔离上下文。
            event: 前序 Message、State、Trace 投影已成功处理的事件。
        """
        ...


class LoggingPublicProjector:
    """记录挂起和 run 终态的安全元数据。

    使用注入或模块级 logger，不创建 Handler；不写入消息正文、错误详情或恢复 token。
    实例不缓存事件，也不负责 logger 生命周期。
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
    ) -> None:
        """保存可注入 logger。

        Args:
            logger: 日志入口；缺省使用当前模块 logger。
        """
        self._logger = logger or logging.getLogger(__name__)

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        """映射公开结果并记录元数据，过程事件跳过。

        Args:
            ctx: 当前 session 上下文。
            event: 待映射的内部事件。

        Raises:
            ValueError: 事件与上下文的 session_id 不一致。
        """
        if event.session_id != ctx.session_id:
            raise ValueError("Event 与 SessionContext 的 session_id 不一致")
        result = to_public_run_result(event)
        if result is None:
            return
        event_name = _EVENT_NAME_BY_STATUS[result.status]
        self._logger.info(
            "public_projector %s → %s",
            event_name,
            result.status.name,
            extra={
                "event_name": event_name,
                "status": result.status.name,
                "session_id": result.session_id,
                "run_id": result.run_id,
                "event_id": result.event_id,
            },
        )


class LoggingProjectorFactory:
    """无状态的内置日志输出器工厂，每次调用创建独立输出器实例。"""

    def __call__(
        self,
        options: Mapping[str, JsonValue],
    ) -> LoggingPublicProjector:
        """创建日志输出器。

        Args:
            options: 必须为空的实现配置。

        Returns:
            使用模块 logger 的日志输出器。

        Raises:
            ValueError: 传入非空 options。
        """
        if options:
            raise ValueError("日志 PublicProjector 不支持 options")
        return LoggingPublicProjector()
