"""SQL 身份列的验证及空 Agent 映射。"""

from typing import TypedDict

from agentsystem.memory.store import MemoryScope, validate_identity


class MemoryIdentity(TypedDict):
    """经过验证、仅在 SQL adapter 内使用的身份参数。

    Attributes:
        app_id: 应用命名空间，最多 191 个字符。
        scope: MemoryScope 对应的持久化字符串。
        user_id: 用户身份，最多 191 个字符。
        agent_id: Agent 身份；全局记忆用空字符串替代 None。
    """

    app_id: str
    scope: str
    user_id: str
    agent_id: str


def memory_identity(
    *, app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
) -> MemoryIdentity:
    """身份超出索引列长度时拒绝，不截断。

    Args:
        app_id: 应用命名空间。
        scope: 用户记忆范围，Service 会校验是否启用。
        user_id: 当前用户身份。
        agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。

    Returns:
        SQL 身份参数，全局范围的 agent_id 为空字符串。

    Raises:
        ValueError: 身份无效或任一索引身份字段超过 191 字符。
    """
    validate_identity(app_id, scope, user_id, agent_id)
    if any(len(value) > 191 for value in (app_id, user_id, agent_id or "")):
        raise ValueError("Memory SQL identity exceeds 191 characters")
    return MemoryIdentity(
        app_id=app_id, scope=scope.value, user_id=user_id, agent_id=agent_id or ""
    )
