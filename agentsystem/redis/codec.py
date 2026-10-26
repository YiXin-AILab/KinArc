"""Redis payload 的带版本规范 JSON codec。"""

import json

from pydantic import TypeAdapter, ValidationError

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue

_SCHEMA_VERSION = 1
_ENVELOPE_ADAPTER = TypeAdapter(dict[str, JsonValue])


class RedisJsonCodec:
    """以规范 JSON bytes 编解码带版本的 Runtime payload。

    无状态且不持有 Redis client；仅处理传输封套，不校验业务领域模型。
    """

    def encode(self, payload: JsonValue) -> bytes:
        """用当前 schema 版本和规范键顺序编码 payload。

        Args:
            payload: 可 JSON 序列化的 Runtime 内容，不允许非有限浮点数。

        Returns:
            使用 UTF-8、固定分隔符和排序键编码的版本化 JSON bytes。

        Raises:
            AdapterError: 内容无法编码为规范 JSON。
        """
        envelope = {
            "payload": payload,
            "schema_version": _SCHEMA_VERSION,
        }
        try:
            encoded = json.dumps(
                envelope,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise AdapterError(
                operation="redis.encode",
                code="redis_payload_invalid",
                message="payload 不能编码为规范 JSON",
            ) from error
        return encoded.encode("utf-8")

    def decode(self, encoded: bytes | str) -> JsonValue:
        """校验 envelope 和 schema 版本后返回原始 payload。

        Args:
            encoded: 带 schema_version 和 payload 的 JSON bytes 或字符串。

        Returns:
            版本化 envelope 中的原始 JSON payload。

        Raises:
            AdapterError: JSON、封套、schema 版本或 payload 校验失败。
        """
        try:
            envelope = _ENVELOPE_ADAPTER.validate_json(encoded)
        except ValidationError as error:
            raise AdapterError(
                operation="redis.decode",
                code="redis_payload_invalid",
                message="Redis payload 不是合法 JSON envelope",
            ) from error
        version = envelope.get("schema_version")
        if version != _SCHEMA_VERSION:
            raise AdapterError(
                operation="redis.decode",
                code="unsupported_schema_version",
                message=f"不支持 schema_version={version}",
            )
        if "payload" not in envelope:
            raise AdapterError(
                operation="redis.decode",
                code="redis_payload_invalid",
                message="Redis payload 缺少 payload 字段",
            )
        return envelope["payload"]
