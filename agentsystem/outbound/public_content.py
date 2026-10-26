"""统一 SSE 与历史消息的公开内容脱敏和截断策略。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from agentsystem.common.models import JsonValue
from agentsystem.config.models import PublicStreamConfig

_SENSITIVE_KEYS = frozenset(
    {"api_key", "token", "password", "secret", "authorization", "cookie"}
)


@dataclass(frozen=True, slots=True)
class PublicContentPolicy:
    """使公开流和权威消息快照使用同一内容口径的无状态策略。

    仅结构化工具参数按敏感键脱敏，工具结果原文只按开关和字节上限处理。

    Attributes:
        config: 是否公开工具内容及最大载荷字节数的配置引用。
    """

    config: PublicStreamConfig

    def tool_arguments(
        self,
        arguments: Mapping[str, JsonValue] | None,
    ) -> tuple[dict[str, JsonValue] | None, str | None, bool]:
        """按配置公开结构化参数及其紧凑 JSON。

        Args:
            arguments: 已解析工具参数；None 表示无有效结构化参数。

        Returns:
            脱敏参数、JSON 文本和截断标志；发生截断时不返回结构化参数。
            禁止公开或没有参数时返回两个 None 和 False。
        """
        if not self.config.include_tool_arguments or arguments is None:
            return None, None, False
        redacted = redact_mapping(arguments)
        serialized = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
        raw_arguments, truncated = truncate_utf8(
            serialized,
            self.config.max_tool_payload_bytes,
        )
        return None if truncated else redacted, raw_arguments, truncated

    def tool_result(self, content: str) -> tuple[str | None, bool]:
        """按配置公开工具结果原文，不解析或递归脱敏结果文本。

        Args:
            content: 工具返回的原始文本。

        Returns:
            允许公开的文本及截断标志；禁用时为 None 和 False。
        """
        if not self.config.include_tool_results:
            return None, False
        return truncate_utf8(content, self.config.max_tool_payload_bytes)


def redact_mapping(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """递归遮蔽常见敏感键，不修改输入映射。

    Args:
        value: 可包含嵌套字典和列表的 JSON 映射。

    Returns:
        对 api_key、token 等已知键名进行大小写不敏感遮蔽的新字典。
    """
    return {
        key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else _redact_value(item)
        for key, item in value.items()
    }


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    """按 UTF-8 字节上限截断文本，并丢弃不完整的尾部字符。

    Args:
        value: 原始文本。
        max_bytes: 调用方提供的非负字节上限。

    Returns:
        上限内的文本和是否发生截断。
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _redact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
