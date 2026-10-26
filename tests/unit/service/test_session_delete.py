"""删除对话的活动检查、错误映射与默认语义。"""

from datetime import UTC, datetime

import pytest
from agentsystem.common.errors import AdapterError, ServiceError
from agentsystem.common.models import OutputMode, SessionContext
from agentsystem.inbox.models import InboundMessage
from agentsystem.message.models import TextBlock
from agentsystem.runtime.state import AgentState, SessionStatus
from tests.unit.service.test_session_catalog import CatalogMessages
from tests.unit.test_application import (
    _CONTEXT,
    ApplicationService,
    _Agents,
    _Queue,
    _States,
)


class DeleteMessages(CatalogMessages):
    """记录删除范围并支持失败注入。"""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.calls: list[bool] = []
        self.fail = fail

    async def delete_session(
        self, ctx: SessionContext, *, hard_delete: bool = False
    ) -> None:
        self.calls.append(hard_delete)
        if self.fail:
            raise AdapterError(
                operation="session.delete",
                code="session_storage_failed",
                message="存储不可用",
            )


async def test_default_soft_delete_and_explicit_hard_delete() -> None:
    messages = DeleteMessages()
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=messages,
    )
    await app.delete_session(_CONTEXT)
    await app.delete_session(_CONTEXT, hard_delete=True)
    assert messages.calls == [False, True]


@pytest.mark.parametrize("status", [SessionStatus.RUNNING, SessionStatus.PENDING, None])
async def test_busy_session_is_not_deleted(status: SessionStatus | None) -> None:
    messages, queue = DeleteMessages(), _Queue()
    now = datetime.now(UTC)
    state = None
    if status is None:
        queue.messages.append(
            InboundMessage(
                message_id="pending",
                session_id=_CONTEXT.session_id,
                origin="user",
                content=(TextBlock(text="queued"),),
                created_at=now,
            )
        )
    else:
        state = AgentState(
            session_context=_CONTEXT,
            status=status,
            run_id="r",
            output_mode=OutputMode.COMPLETE,
            started_at=now,
            active_started_at=now if status is SessionStatus.RUNNING else None,
            pending_tool_call_id="t" if status is SessionStatus.PENDING else None,
            resume_token="token" if status is SessionStatus.PENDING else None,
        )
    app = ApplicationService(
        agents=_Agents(),
        inbox=queue,
        states=_States(state),
        messages=messages,
    )
    with pytest.raises(ServiceError) as error:
        await app.delete_session(_CONTEXT, hard_delete=True)
    assert error.value.code == "session_busy"
    assert messages.calls == []


async def test_delete_storage_failure_is_not_reported_as_success() -> None:
    app = ApplicationService(
        agents=_Agents(),
        inbox=_Queue(),
        states=_States(None),
        messages=DeleteMessages(fail=True),
    )
    with pytest.raises(ServiceError) as error:
        await app.delete_session(_CONTEXT)
    assert error.value.code == "session_storage_failed"
