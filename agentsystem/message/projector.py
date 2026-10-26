"""把消息相关 AgentEvent 投影到 MessageManager。"""

from agentsystem.common.models import SessionContext
from agentsystem.message.manager import MessageManager
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    ContextCompactionCompleted,
    MessagesCompleted,
)


class MessageProjector:
    """把定稿消息事件投影到注入的 MessageManager，形成保存屏障。

    不持有连接生命周期，不处理流式增量；存储异常直接传播给 Session，
    由其停止后续投影，已经提交的消息不在此回滚。
    """

    def __init__(self, manager: MessageManager) -> None:
        """注入 Message 内容权威端口。

        Args:
            manager: 注入的权威存储协议实现；其连接生命周期由应用管理。
        """
        self._manager = manager

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        """消息事件原子提交；其他事件安全跳过。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            event: 包含会话身份及业务载荷的事件封套。

        Raises:
            ValueError: 事件或状态的会话身份与 ctx 不一致。
            AdapterError: 底层权威存储拒绝提交；异常向调用方传播。
        """
        if event.session_id != ctx.session_id:
            raise ValueError("Event 与 SessionContext 的 session_id 不一致")
        if isinstance(event.payload, MessagesCompleted):
            await self._manager.commit(ctx, event.payload.messages)
        elif (
            isinstance(event.payload, ContextCompactionCompleted)
            and event.payload.compaction_message is not None
        ):
            await self._manager.commit_compaction(
                ctx,
                event.payload.compaction_message,
                expected_latest_compaction_id=(
                    event.payload.expected_latest_compaction_id
                ),
            )


__all__ = ["MessageProjector"]
