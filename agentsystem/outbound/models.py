"""经过公开映射和脱敏的非流式 run 终态。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from agentsystem.common.errors import RunError
from agentsystem.common.models import StrictModel
from agentsystem.message.models import Message
from agentsystem.runtime.state import SessionStatus


class PublicRunResult(StrictModel):
    """PublicProjector 可交付的非流式挂起或终态结果。

    本记录不替代 StateManager 的状态权威；字段组合按 status 校验。

    Attributes:
        event_id: 内部事件标识，用于交付关联。
        created_at: 事件生成时间。
        session_id: 所属会话标识。
        run_id: 内部执行标识。
        trace_id: Runtime trace 关联标识。
        status: 挂起、成功、失败或取消状态。
        message: 成功时可公开的最终文本消息，可为空。
        error: 失败或取消时的结构化错误。
        resume_token: 仅 PENDING 携带的单次恢复 token。
        suspend_expires_at: 可选挂起过期时刻。
    """

    event_id: str = Field(min_length=1)
    created_at: datetime
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    status: Literal[
        SessionStatus.PENDING,
        SessionStatus.SUCCEEDED,
        SessionStatus.FAILED,
        SessionStatus.CANCELED,
    ]
    message: Message | None = None
    error: RunError | None = None
    resume_token: str | None = Field(default=None, min_length=1)
    suspend_expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        """确保 PENDING、成功和失败终态不混合字段。

        Returns:
            校验通过的当前结果。

        Raises:
            ValueError: 当前状态缺少必需字段或携带互斥字段。
        """
        if self.status is SessionStatus.PENDING:
            if self.resume_token is None or self.message is not None or self.error:
                raise ValueError("PENDING 公开结果字段不完整")
            return self
        if self.resume_token is not None or self.suspend_expires_at is not None:
            raise ValueError("非 PENDING 结果不得携带恢复字段")
        if self.status is SessionStatus.SUCCEEDED:
            if self.error is not None:
                raise ValueError("SUCCEEDED 结果不得携带 error")
            return self
        if self.message is not None or self.error is None:
            raise ValueError("FAILED/CANCELED 结果必须只携带 error")
        return self
