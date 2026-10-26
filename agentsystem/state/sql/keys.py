"""SQL StateManager 使用的固定长度摘要键。"""

import hashlib

from agentsystem.common.models import SessionContext


def state_scope_key(ctx: SessionContext) -> bytes:
    """以无歧义的身份分段编码计算 session scope 摘要。

    Args:
        ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

    Returns:
        完整会话身份的 32 字节 SHA-256 摘要。
    """
    digest = hashlib.sha256()
    for value in (ctx.app_id, ctx.user_id, ctx.agent_id, ctx.session_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big", signed=False))
        digest.update(encoded)
    return digest.digest()


def payload_hash(payload: bytes) -> bytes:
    """计算规范 AgentState payload 的内容摘要。

    Args:
        payload: 用于持久化的规范 payload bytes。

    Returns:
        内容的 32 字节 SHA-256 摘要，不修改原始内容。
    """
    return hashlib.sha256(payload).digest()


__all__ = ["payload_hash", "state_scope_key"]
