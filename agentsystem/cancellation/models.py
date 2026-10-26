"""分布式取消控制面的内部命令模型。"""

from datetime import datetime

from pydantic import Field

from agentsystem.common.models import FrozenModel


class CancellationCommand(FrozenModel):
    """供取消总线广播的不可变控制命令，不携带业务正文。

    Attributes:
        app_id: 目标应用标识。
        agent_id: 目标 Agent 标识。
        user_id: 目标用户标识。
        session_id: 目标会话标识。
        run_id: 要取消的内部 run，避免误取消同会话的后续执行。
        requested_at: 请求时间；本模型不执行过期判断。
    """

    app_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    requested_at: datetime
