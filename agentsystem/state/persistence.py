"""State 存储实现共享的规范 AgentState 编解码。"""

import json

from pydantic import TypeAdapter, ValidationError

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue
from agentsystem.runtime.state import AgentState

_SCHEMA_VERSION = 1
_ENVELOPE_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class StateCodec:
    """以规范 JSON bytes 编解码带版本的 AgentState。

    无状态且不执行 I/O；Redis 与 SQL 使用相同编码进行完整快照 CAS。
    """

    def encode(self, state: AgentState) -> bytes:
        """按现有 Redis envelope 规则编码已校验状态。

        Args:
            state: 已经通过领域校验的执行快照。

        Returns:
            使用 UTF-8、固定分隔符和排序键编码的版本化 JSON bytes。

        Raises:
            AdapterError: 内容无法编码为规范 JSON。
        """
        payload = _JSON_ADAPTER.validate_python(state.model_dump(mode="json"))
        envelope = {"payload": payload, "schema_version": _SCHEMA_VERSION}
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
                operation="state.encode",
                code="state_payload_invalid",
                message="AgentState 不能编码为规范 JSON",
            ) from error
        return encoded.encode("utf-8")

    def decode(self, encoded: bytes | str) -> AgentState:
        """校验 envelope、版本和领域模型后返回 AgentState。

        Args:
            encoded: 带 schema_version 和 payload 的 JSON bytes 或字符串。

        Returns:
            通过 envelope、版本及领域校验的 AgentState。

        Raises:
            AdapterError: JSON、封套、schema 版本或 payload 校验失败。
        """
        try:
            envelope = _ENVELOPE_ADAPTER.validate_json(encoded)
            version = envelope.get("schema_version")
            if version != _SCHEMA_VERSION:
                raise AdapterError(
                    operation="state.decode",
                    code="unsupported_schema_version",
                    message=f"不支持 AgentState schema_version={version}",
                )
            if "payload" not in envelope:
                raise AdapterError(
                    operation="state.decode",
                    code="state_payload_invalid",
                    message="AgentState envelope 缺少 payload",
                )
            return AgentState.model_validate(envelope["payload"])
        except AdapterError:
            raise
        except ValidationError as error:
            raise AdapterError(
                operation="state.decode",
                code="state_payload_invalid",
                message="AgentState payload 不是合法 JSON 或领域模型",
            ) from error


__all__ = ["StateCodec"]
