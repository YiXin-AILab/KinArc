"""把事件对应的执行快照投影到 StateManager。"""

from agentsystem.common.models import SessionContext
from agentsystem.runtime.events import AgentEventEnvelope
from agentsystem.runtime.state import AgentState
from agentsystem.state.manager import StateManager


class StateProjector:
    """把事件前后执行快照交给注入的 StateManager 做完整 CAS。

    保存前复制快照以隔离后续对象修改；不解释事件业务、不管理连接，
    存储异常直接传播给 Session。
    """

    def __init__(self, manager: StateManager) -> None:
        """注入执行控制状态权威端口。

        Args:
            manager: 注入的权威存储协议实现；其连接生命周期由应用管理。
        """
        self._manager = manager

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
        previous_state: AgentState,
        current_state: AgentState,
    ) -> None:
        """校验执行身份后执行一次 CAS 保存。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            event: 包含会话身份及业务载荷的事件封套。
            previous_state: 事件发生前的完整执行快照。
            current_state: 事件应用后的完整执行快照。

        Raises:
            ValueError: 事件或状态的会话身份与 ctx 不一致。
            AdapterError: 底层权威存储拒绝提交；异常向调用方传播。
        """
        if event.session_id != ctx.session_id:
            raise ValueError("Event 与 SessionContext 的 session_id 不一致")
        if (
            previous_state.session_context != ctx
            or current_state.session_context != ctx
        ):
            raise ValueError("投影 State 与 SessionContext 不一致")
        await self._manager.save(
            previous_state.model_copy(deep=True),
            current_state.model_copy(deep=True),
        )


__all__ = ["StateProjector"]
