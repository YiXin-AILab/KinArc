"""Memory 独占的无歧义 Redis key 编码。"""

import base64
import json

from agentsystem.memory.store import MemoryScope, validate_identity


def memory_key(
    prefix: str, *, app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
) -> str:
    """编码真实身份字段，不向领域层暴露物理 key。

    Args:
        prefix: Application 分配的 Redis 命名空间。
        app_id: 应用命名空间。
        scope: 用户记忆范围，Service 会校验是否启用。
        user_id: 当前用户身份。
        agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。

    Returns:
        该逻辑身份对应的模块私有 Redis key。
    """
    validate_identity(app_id, scope, user_id, agent_id)
    identity = json.dumps(
        [app_id, scope, user_id, agent_id], ensure_ascii=False
    ).encode()
    encoded = base64.urlsafe_b64encode(identity).decode("ascii")
    return f"{prefix}:memory:{encoded}"
