"""与存储技术无关的异步 StateManager 领域端口。"""

from enum import StrEnum
from typing import Protocol

from agentsystem.common.models import SessionContext
from agentsystem.runtime.state import AgentState


class StateWriteResult(StrEnum):
    """区分状态写入是否实际改变存储。

    Attributes:
        APPLIED: 本次首次创建或成功替换了状态。
        IDEMPOTENT: 存储中已经存在同一规范目标快照。
    """

    APPLIED = "applied"
    IDEMPOTENT = "idempotent"


class StateManager(Protocol):
    """定义每个会话唯一最新 AgentState 的权威存储协议。

    保存通过完整旧快照 CAS，允许相同目标快照幂等重试；不存储消息正文。
    具体实现使用应用注入的存储连接，调用方负责执行状态转换。
    """

    async def get(self, ctx: SessionContext) -> AgentState | None:
        """读取 session 最新状态。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            已校验的最新状态；尚未创建时为 None。

        Raises:
            AdapterError: 存储状态无法解码或与请求身份不匹配。
        """
        ...

    async def create(self, initial_state: AgentState) -> StateWriteResult:
        """首次创建状态；相同初始内容可幂等重试。

        Args:
            initial_state: 包含完整会话身份的初始执行快照。

        Returns:
            首次生效返回 APPLIED；已存相同目标快照返回 IDEMPOTENT。

        Raises:
            AdapterError: 已存在不同内容的状态，或存储内容非法。
        """
        ...

    async def save(
        self,
        previous: AgentState,
        current: AgentState,
    ) -> StateWriteResult:
        """通过完整 previous 快照 CAS 保存 current。

        Args:
            previous: CAS 预期的完整旧执行快照。
            current: 拟保存的完整新执行快照，必须与旧快照属于同一会话。

        Returns:
            首次生效返回 APPLIED；已存相同目标快照返回 IDEMPOTENT。

        Raises:
            ValueError: previous 与 current 的会话身份不同。
            AdapterError: 状态不存在、旧快照不匹配或存储内容非法。
        """
        ...
