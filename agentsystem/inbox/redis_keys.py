"""仅属于 Inbox/Ready 模块的 Redis key。"""

import re

from agentsystem.common.models import SessionContext
from agentsystem.redis.scope import encode_redis_component, encode_session_scope

_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]+")


class InboxRedisKeys:
    """生成 Inbox 的 Stream、顺序、正文及领取 key，不执行 I/O。

    Ready Stream 共享应用级 hash tag；同会话输入 key 使用完整作用域的
    hash tag，以满足各自 Lua 和多 Stream 读取的同槽要求。
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

    def ready(self, agent_id: str) -> str:
        """返回 Agent 跨会话的 Ready Stream key。

        Args:
            agent_id: 已校验的 Agent 标识，将编码后拼入 key。

        Returns:
            使用应用 Ready hash tag 的 Stream key。
        """
        return (
            f"{self._prefix}:{{{self._prefix}:ready}}:ready:"
            f"{encode_redis_component(agent_id)}"
        )

    def order(self, ctx: SessionContext) -> str:
        """返回会话输入顺序 list key。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            与该会话输入及锁同槽的 Redis key。
        """
        return self._session_key(ctx, "order")

    def data(self, ctx: SessionContext) -> str:
        """返回会话输入正文 hash key。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            与该会话输入及锁同槽的 Redis key。
        """
        return self._session_key(ctx, "data")

    def claims(self, ctx: SessionContext) -> str:
        """返回会话领取租约 hash key。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            与该会话输入及锁同槽的 Redis key。
        """
        return self._session_key(ctx, "claims")

    def _session_key(self, ctx: SessionContext, suffix: str) -> str:
        scope = encode_session_scope(ctx)
        return f"{self._prefix}:{{{scope}}}:inbox:{suffix}"
