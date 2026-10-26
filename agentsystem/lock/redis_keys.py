"""仅属于 SessionLock 模块的 Redis key。"""

import re

from agentsystem.common.models import SessionContext
from agentsystem.redis.scope import encode_session_scope

_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]+")


class LockRedisKeys:
    """构造会话锁 key 的无 I/O 值对象。

    使用完整会话作用域生成 hash tag，使锁与该会话的 Inbox key 同槽。
    """

    def __init__(self, prefix: str) -> None:
        """保存 key 命名空间，不访问 Redis。

        Args:
            prefix: 仅含字母数字、冒号、下划线和横线的非空前缀。

        Raises:
            ValueError: 前缀为空或含非法字符。
        """
        if _PREFIX_PATTERN.fullmatch(prefix) is None:
            raise ValueError("Redis prefix 包含非法字符")
        self._prefix = prefix

    def lock(self, ctx: SessionContext) -> str:
        """返回与 session 数据共享 hash tag 的锁 key。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            带已编码作用域的 Redis 锁 key。
        """
        scope = encode_session_scope(ctx)
        return f"{self._prefix}:{{{scope}}}:lock"
