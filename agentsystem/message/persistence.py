"""Message 存储实现共享的规范编码与上下文组装。"""

import json

from pydantic import TypeAdapter, ValidationError

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import JsonValue
from agentsystem.message.models import (
    CompactionBlock,
    ContextMessage,
    InjectedBlock,
    Message,
    MessageVisibility,
    Role,
    is_model_visible,
)

_SCHEMA_VERSION = 2
_ENVELOPE_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class MessageCodec:
    """以规范 JSON bytes 编解码带版本的 Message。

    无状态且不执行 I/O，供 Redis 与 SQL 实现共用；稳定编码用于幂等比较。
    """

    def encode(self, message: Message) -> bytes:
        """按现有 Redis envelope 规则编码一条已校验 Message。

        Args:
            message: 已经通过领域校验的消息。

        Returns:
            使用 UTF-8、固定分隔符和排序键编码的版本化 JSON bytes。

        Raises:
            AdapterError: 内容无法编码为规范 JSON。
        """
        payload = _JSON_ADAPTER.validate_python(message.model_dump(mode="json"))
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
                operation="message.encode",
                code="message_payload_invalid",
                message="Message 不能编码为规范 JSON",
            ) from error
        return encoded.encode("utf-8")

    def decode(self, encoded: bytes | str) -> Message:
        """校验 envelope、版本和领域模型后返回 Message。

        Args:
            encoded: 带 schema_version 和 payload 的 JSON bytes 或字符串。

        Returns:
            通过 envelope、版本及领域校验的 Message。

        Raises:
            AdapterError: JSON、封套、schema 版本或 payload 校验失败。
        """
        try:
            envelope = _ENVELOPE_ADAPTER.validate_json(encoded)
            version = envelope.get("schema_version")
            if version != _SCHEMA_VERSION:
                raise AdapterError(
                    operation="message.decode",
                    code="unsupported_schema_version",
                    message=f"不支持 Message schema_version={version}",
                )
            if "payload" not in envelope:
                raise AdapterError(
                    operation="message.decode",
                    code="message_payload_invalid",
                    message="Message envelope 缺少 payload",
                )
            return Message.model_validate(envelope["payload"])
        except AdapterError:
            raise
        except ValidationError as error:
            raise AdapterError(
                operation="message.decode",
                code="message_payload_invalid",
                message="Message payload 不是合法 JSON 或领域模型",
            ) from error


def assemble_context_message(messages: tuple[Message, ...]) -> ContextMessage:
    """从稳定存储顺序构造一次模型上下文权威快照。

    Args:
        messages: 同一会话的一次一致性快照，按稳定存储顺序排列。

    Returns:
        基础系统消息、最新压缩快照、保留消息及其注入消息。

    Raises:
        AdapterError: 基础 SYSTEM 消息不是恰好一条，或压缩结构非法。
    """
    base_systems = tuple(
        message
        for message in messages
        if message.role is Role.SYSTEM
        and message.run_id is None
        and message.visibility is MessageVisibility.MODEL
    )
    if len(base_systems) != 1:
        raise AdapterError(
            operation="message.read_context",
            code="message_context_invalid",
            message="session 必须且只能包含一个基础 SYSTEM Message",
        )
    latest_compaction = _latest_internal(messages, CompactionBlock)
    compacted_ids = _compacted_ids(latest_compaction)
    retained = tuple(
        message
        for message in messages
        if is_model_visible(message)
        and not _is_base_system(message)
        and not _is_internal_block_message(message)
        and message.message_id not in compacted_ids
    )
    retained_user_ids = {
        message.message_id for message in retained if message.role is Role.USER
    }
    injections = tuple(
        message
        for message in messages
        if _injection_target(message) in retained_user_ids
    )
    return ContextMessage(
        system_message=base_systems[0],
        latest_compaction_message=latest_compaction,
        retained_messages=retained,
        injection_messages=injections,
    )


def _latest_internal[BlockT: (CompactionBlock, InjectedBlock)](
    messages: tuple[Message, ...], block_type: type[BlockT]
) -> Message | None:
    for message in reversed(messages):
        if len(message.content) == 1 and isinstance(message.content[0], block_type):
            return message
    return None


def _is_internal_block_message(message: Message) -> bool:
    """判断 Message 是否属于 ContextMessage 的内部 Block 分区。"""
    return len(message.content) == 1 and isinstance(
        message.content[0], (CompactionBlock, InjectedBlock)
    )


def _is_base_system(message: Message) -> bool:
    """判断 Message 是否为 ContextMessage 独立保存的基础 SYSTEM。"""
    return message.role is Role.SYSTEM and message.run_id is None


def _compacted_ids(compaction: Message | None) -> frozenset[str]:
    """提取摘要覆盖的消息集合；剪枝 ID 留给后续上下文构建处理。"""
    if compaction is None:
        return frozenset()
    block = compaction.content[0]
    if not isinstance(block, CompactionBlock):
        raise AdapterError(
            operation="message.read_context",
            code="message_context_invalid",
            message="最新压缩消息不包含 CompactionBlock",
        )
    return frozenset(block.compacted_message_ids)


def _injection_target(message: Message) -> str | None:
    if len(message.content) != 1 or not isinstance(message.content[0], InjectedBlock):
        return None
    return message.content[0].target_user_message_id


__all__ = ["MessageCodec", "assemble_context_message"]
