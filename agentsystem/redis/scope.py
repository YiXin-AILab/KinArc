"""不包含模块业务语义的 Redis session scope 编码。"""

import base64

from agentsystem.common.models import SessionContext


def encode_session_scope(context: SessionContext) -> str:
    """把四个独立定位字段编码为可放入 Redis hash tag 的稳定 scope。

    Args:
        context: 应用、用户、Agent 和会话的完整身份。

    Returns:
        四段 URL-safe Base64 组件以点分隔组成的稳定 hash tag 内容。
    """
    return ".".join(
        encode_redis_component(component)
        for component in (
            context.app_id,
            context.user_id,
            context.agent_id,
            context.session_id,
        )
    )


def encode_redis_component(component: str) -> str:
    """把单个外部定位值编码为 Redis key 安全组件。

    Args:
        component: 待编码的原始 UTF-8 定位值。

    Returns:
        移除末尾填充符的 URL-safe Base64 字符串。
    """
    encoded = base64.urlsafe_b64encode(component.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")
