"""仅属于公开 Session EventBus adapter 的 Redis key。"""

import re

from agentsystem.common.models import SessionContext
from agentsystem.redis.scope import encode_session_scope

_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]+")


class PublicEventRedisKeys:
    """生成同一 Redis Cluster hash slot 内的 Stream 与 metadata key。

    只保存应用前缀并编码 SessionContext，不执行 I/O 或管理连接。
    """

    def __init__(self, prefix: str) -> None:
        """保存经过约束的应用级 Redis prefix。

        Args:
            prefix: 仅含字母、数字、冒号、下划线和连字符的非空前缀。

        Raises:
            ValueError: 前缀包含非法字符或为空。
        """
        if _PREFIX_PATTERN.fullmatch(prefix) is None:
            raise ValueError("Redis prefix 包含非法字符")
        self._prefix = prefix

    def stream(self, ctx: SessionContext) -> str:
        """返回 session 公开事件 Stream key。

        Args:
            ctx: 用于编码隔离范围的 session 上下文。

        Returns:
            带作用域 hash tag 的 Stream key。
        """
        return self._session_key(ctx, "public-events")

    def metadata(self, ctx: SessionContext) -> str:
        """返回 epoch 与 lifecycle metadata hash key。

        Args:
            ctx: 用于编码隔离范围的 session 上下文。

        Returns:
            与 Stream key 同 slot 的 metadata key。
        """
        return self._session_key(ctx, "public-events:meta")

    def _session_key(self, ctx: SessionContext, suffix: str) -> str:
        scope = encode_session_scope(ctx)
        return f"{self._prefix}:{{{scope}}}:{suffix}"
