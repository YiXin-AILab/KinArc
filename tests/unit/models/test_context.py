"""SessionContext 行为测试。"""

import pytest
from agentsystem.common.models import SessionContext
from pydantic import ValidationError


def test_session_context_rejects_empty_identity() -> None:
    """任何空身份字段都必须在边界处被拒绝。"""
    with pytest.raises(ValidationError):
        SessionContext(
            app_id="",
            user_id="user",
            agent_id="agent",
            session_id="session",
        )


def test_session_context_is_immutable() -> None:
    """创建后的会话定位信息不得被原地修改。"""
    context = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="session",
    )

    field_name = "session_id"
    with pytest.raises(ValidationError):
        setattr(context, field_name, "changed")
