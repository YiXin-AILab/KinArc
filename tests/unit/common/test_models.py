"""共享基础模型的严格校验与不可变行为测试。"""

import pytest
from agentsystem.common.models import FrozenModel, SessionContext, StrictModel
from pydantic import ValidationError


class _StrictExample(StrictModel):
    """用于验证额外字段拒绝行为。"""

    value: str


class _FrozenExample(FrozenModel):
    """用于验证不可变行为。"""

    value: str


def test_common_models_reject_extra_fields_and_frozen_mutation() -> None:
    """严格模型拒绝未知输入，不可变模型拒绝原地修改。"""
    with pytest.raises(ValidationError):
        _StrictExample.model_validate({"value": "ok", "extra": "rejected"})

    frozen = _FrozenExample(value="before")
    with pytest.raises(ValidationError):
        frozen.__setattr__("value", "after")


@pytest.mark.parametrize("field", ["app_id", "user_id", "agent_id", "session_id"])
def test_session_identity_rejects_trailing_spaces(field: str) -> None:
    """身份不能因 MySQL PAD SPACE 比较而与另一个值合并。"""
    values = dict(app_id="app", user_id="user", agent_id="agent", session_id="session")
    values[field] += " "
    with pytest.raises(ValidationError, match="trailing space"):
        SessionContext.model_validate(values)


def test_session_identity_preserves_case_unicode_and_leading_space() -> None:
    """合法身份保留原值，不静默规范化。"""
    ctx = SessionContext(
        app_id=" 应用", user_id="User", agent_id="agent", session_id="会话"
    )
    assert (ctx.app_id, ctx.user_id, ctx.session_id) == (" 应用", "User", "会话")
