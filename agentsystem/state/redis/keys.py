"""仅属于 StateManager 模块的 Redis key。"""

import re

from agentsystem.common.models import SessionContext
from agentsystem.redis.scope import encode_session_scope

_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]+")


class StateRedisKeys:
    """构造最新 AgentState 的 Redis key，不访问 Redis。

    实例保存经校验的 namespace 前缀，完整会话身份决定 hash tag。
    """

    def __init__(self, prefix: str) -> None:
        """保存合法 Redis prefix。

        Args:
            prefix: Redis namespace 前缀，仅允许字母、数字、冒号、下划线和连字符。

        Raises:
            ValueError: Redis prefix 为空或包含非法字符。
        """
        if _PREFIX_PATTERN.fullmatch(prefix) is None:
            raise ValueError("Redis prefix 包含非法字符")
        self._prefix = prefix

    def state(self, ctx: SessionContext) -> str:
        """返回与 session 数据共享 hash tag 的 state key。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        scope = encode_session_scope(ctx)
        return f"{self._prefix}:{{{scope}}}:state"
