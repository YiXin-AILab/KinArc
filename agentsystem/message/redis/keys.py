"""仅属于 MessageManager 模块的 Redis key。"""

import re

from agentsystem.common.models import SessionContext
from agentsystem.message.sessions import identity_digest
from agentsystem.redis.scope import encode_session_scope

_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]+")


class MessageRedisKeys:
    """构造消息存储及用户对话目录的 Redis key，不访问 Redis。

    会话消息共享完整身份 hash tag；用户目录与其索引使用用户级 hash tag。
    实例仅保存经校验的应用 namespace 前缀。
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

    def order(self, ctx: SessionContext) -> str:
        """返回 Message 稳定顺序 list key。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        return self._session_key(ctx, "order")

    def sessions(self, app_id: str, user_id: str) -> str:
        """用户目录记录和索引共用用户 hash slot。

        Args:
            app_id: 应用隔离标识。
            user_id: 用户隔离标识。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        scope = identity_digest(app_id, user_id)
        return f"{self._prefix}:{{catalog:{scope}}}:messages:sessions"

    def session_index(
        self, app_id: str, user_id: str, agent_id: str | None = None
    ) -> str:
        """返回全部或单 Agent 的同槽目录索引。

        Args:
            app_id: 应用隔离标识。
            user_id: 用户隔离标识。
            agent_id: 可选 Agent 筛选标识；None 使用用户全部会话索引。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        suffix = "all" if agent_id is None else identity_digest(agent_id)
        return f"{self.sessions(app_id, user_id)}:index:{suffix}"

    def data(self, ctx: SessionContext) -> str:
        """返回 Message payload hash key。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        return self._session_key(ctx, "data")

    def latest_compaction(self, ctx: SessionContext) -> str:
        """返回最新 Compaction Message ID 的 CAS 指针 key。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        return self._session_key(ctx, "latest_compaction")

    def deleted(self, ctx: SessionContext) -> str:
        """与消息同槽的永久写屏障。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            包含 namespace 和对应隔离范围 hash tag 的 Redis key。
        """
        return self._session_key(ctx, "deleted")

    def _session_key(self, ctx: SessionContext, suffix: str) -> str:
        scope = encode_session_scope(ctx)
        return f"{self._prefix}:{{{scope}}}:messages:{suffix}"
