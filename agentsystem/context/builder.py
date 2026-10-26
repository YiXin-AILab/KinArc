"""把持久化上下文源投影为不可变 Provider 发送副本。"""

import hashlib
import html
import json
from collections.abc import Mapping
from typing import cast

from agentsystem.common.models import JsonValue
from agentsystem.config.models import CompactionConfig
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    CompactionBlock,
    ContextMessage,
    DocumentBlock,
    ImageBlock,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    StoredBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    VideoBlock,
)
from agentsystem.runtime.state import AgentState


class ContextBuilder:
    """仅依赖配置、无 I/O 的模型上下文构建器。

    从权威消息和控制状态生成发送副本；修复和剪枝不回写历史记录，实例不保存会话状态。
    """

    def __init__(
        self,
        config: CompactionConfig | None = None,
        *,
        include_asset_id_in_context: bool = False,
    ) -> None:
        """保存工具剪枝的当前配置。

        Args:
            config: 剪枝长度配置，缺省时采用 CompactionConfig 默认值。
            include_asset_id_in_context: 是否在资产媒体块前插入可见引用文本。
        """
        self._config = config or CompactionConfig()
        self._include_asset_id_in_context = include_asset_id_in_context

    def build(
        self,
        source: ContextMessage,
        state: AgentState,
    ) -> tuple[Message, ...]:
        """投影 SYSTEM、摘要、保留历史和当前输入消息的 Injection。

        仅合入 state.current_message_ids 对应 USER Message 的动态注入。

        Args:
            source: MessageManager 提供的权威上下文快照。
            state: 当前消息 ID、STEER 排除项和挂起调用等执行控制状态。

        Returns:
            已修复工具配对、按调用顺序排列且完成注入的模型发送副本。

        Raises:
            ValueError: 压缩边界、消息块、当前消息覆盖或最终 ID 唯一性无效。
        """
        _validate_compaction_boundary(source)
        excluded = set(state.steer_message_ids)
        retained = tuple(
            message
            for message in source.retained_messages
            if message.message_id not in excluded and message.error is None
        )
        repaired = _repair_history(retained, state)
        ordered = _reorder_tool_results(repaired)
        compaction = _compaction_block(source)
        if compaction is not None:
            ordered = self._prune_tools(
                ordered,
                frozenset(compaction.pruned_message_ids),
            )
        injections = _index_injections(
            source,
            retained,
            frozenset(state.current_message_ids),
        )
        projected = tuple(_inject(message, injections) for message in ordered)
        prefix: tuple[Message, ...] = (source.system_message,)
        if compaction is not None and compaction.summary is not None:
            latest = source.latest_compaction_message
            if latest is None:
                raise ValueError("Compaction Message 缺失")
            prefix += (_project_compaction(latest),)
        result = tuple(
            project_asset_references(
                message,
                enabled=self._include_asset_id_in_context,
            )
            for message in prefix + projected
        )
        _validate_current_ids(result, state)
        _validate_unique_ids(result)
        return result

    def _prune_tools(
        self,
        messages: tuple[Message, ...],
        pruned_message_ids: frozenset[str],
    ) -> tuple[Message, ...]:
        """按快照 Message ID 重放当前配置下的工具剪枝。"""
        return tuple(
            _prune_tool_message(message, self._config)
            if message.message_id in pruned_message_ids
            else message.model_copy(deep=True)
            for message in messages
        )


_MEDIA_BLOCK_TYPES = (ImageBlock, AudioBlock, VideoBlock, DocumentBlock)


def project_asset_references(message: Message, *, enabled: bool) -> Message:
    """返回按配置投影 Asset 引用的 Message 副本。

    Args:
        message: 待发送的原始消息，不会被修改。
        enabled: 是否在 Asset 媒体块前添加转义后的引用文本。

    Returns:
        深复制消息；关闭投影时同样返回副本。
    """
    if not enabled:
        return message.model_copy(deep=True)
    content: list[StoredBlock] = []
    for block in message.content:
        reference = _asset_reference(block)
        if reference is not None and (not content or content[-1] != reference):
            content.append(reference)
        content.append(block)
    return message.model_copy(update={"content": tuple(content)}, deep=True)


def _asset_reference(block: StoredBlock) -> TextBlock | None:
    """为单个 Asset 媒体生成相邻引用。"""
    if not isinstance(block, _MEDIA_BLOCK_TYPES) or not isinstance(
        block.source, AssetSource
    ):
        return None
    source = block.source
    asset_id = html.escape(source.asset_id, quote=True)
    media_type = html.escape(source.media_type, quote=True)
    return TextBlock(
        text=(
            f'<asset_reference asset_id="{asset_id}" type="{block.type}" '
            f'media_type="{media_type}" />'
        )
    )


def _index_injections(
    source: ContextMessage,
    retained: tuple[Message, ...],
    current_message_ids: frozenset[str],
) -> dict[str, InjectedBlock]:
    users = {
        message.message_id
        for message in retained
        if message.role is Role.USER and message.message_id in current_message_ids
    }
    indexed: dict[str, InjectedBlock] = {}
    for message in source.injection_messages:
        block = message.content[0]
        if not isinstance(block, InjectedBlock):
            raise ValueError("injection_messages 包含非法 Block")
        target = block.target_user_message_id
        if target not in users:
            continue
        if target in indexed:
            raise ValueError("当前 run 的 Injection 目标重复")
        indexed[target] = block
    return indexed


def _inject(message: Message, injections: dict[str, InjectedBlock]) -> Message:
    block = injections.get(message.message_id)
    if block is None or not block.items:
        return message.model_copy(deep=True)
    rendered = ["<context_injection>"]
    for item in block.items:
        provider = html.escape(item.provider_name, quote=True)
        content = html.escape(item.content)
        rendered.extend((f'<context provider="{provider}">', content, "</context>"))
    rendered.append("</context_injection>")
    injection_text = TextBlock(text="\n".join(rendered))
    return message.model_copy(update={"content": (injection_text, *message.content)})


def _project_compaction(message: Message) -> Message:
    block = message.content[0]
    if (
        not isinstance(block, CompactionBlock)
        or block.summary is None
        or message.run_id is None
    ):
        raise ValueError("Compaction Message 无效")
    return Message(
        message_id=message.message_id,
        session_id=message.session_id,
        role=Role.USER,
        content=(
            TextBlock(
                text=(
                    "以下 <summary> 是前期对话的总结，请基于该总结继续处理后续消息。\n"
                    f"{block.summary}"
                )
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.MODEL,
        run_id=message.run_id,
        created_at=message.created_at,
    )


def _validate_compaction_boundary(source: ContextMessage) -> None:
    message = source.latest_compaction_message
    if message is None:
        return
    block = message.content[0]
    if not isinstance(block, CompactionBlock):
        raise ValueError("Compaction Message 无效")
    if block.summary is None:
        if block.compacted_message_ids or block.next_retained_message_id is not None:
            raise ValueError("纯剪枝 Block 包含摘要边界")
        return
    retained_ids = tuple(item.message_id for item in source.retained_messages)
    expected_next = retained_ids[0] if retained_ids else None
    if block.next_retained_message_id != expected_next:
        raise ValueError("Compaction 与 retained tail 边界不一致")
    if set(block.compacted_message_ids).intersection(retained_ids):
        raise ValueError("Compaction 覆盖 ID 仍存在于 retained tail")


def _reorder_tool_results(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    result: list[Message] = []
    index = 0
    while index < len(messages):
        current = messages[index]
        result.append(current.model_copy(deep=True))
        call_ids = _tool_call_ids(current)
        if not call_ids:
            index += 1
            continue
        following, next_index = _collect_tool_results(messages, index + 1)
        ordered = _order_results(call_ids, following)
        result.extend(item.model_copy(deep=True) for item in ordered)
        index = next_index
    return tuple(result)


def _repair_history(
    messages: tuple[Message, ...],
    state: AgentState,
) -> tuple[Message, ...]:
    """修复工具调用批次，仅保留挂起调用的未闭合状态。"""
    repaired: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if _historical_thinking_only(message, state):
            index += 1
            continue
        if message.role is Role.TOOL:
            index += 1
            continue
        calls = tuple(
            block for block in message.content if isinstance(block, ToolCallBlock)
        )
        if not calls:
            repaired.append(message.model_copy(deep=True))
            index += 1
            continue
        following, next_index = _collect_tool_results(messages, index + 1)
        repaired.extend(_repair_tool_batch(message, calls, following, state))
        index = next_index
    return tuple(repaired)


def _historical_thinking_only(message: Message, state: AgentState) -> bool:
    return (
        message.role is Role.ASSISTANT
        and message.run_id != state.run_id
        and all(isinstance(block, ThinkingBlock) for block in message.content)
    )


def _repair_tool_batch(
    assistant: Message,
    calls: tuple[ToolCallBlock, ...],
    results: tuple[Message, ...],
    state: AgentState,
) -> tuple[Message, ...]:
    current = assistant.run_id == state.run_id
    expected = {call.tool_call_id for call in calls}
    indexed: dict[str, Message] = {}
    for result in results:
        block = result.content[0]
        if not isinstance(block, ToolResultBlock):
            raise ValueError("TOOL Message 缺少 ToolResultBlock")
        if block.tool_call_id not in expected or block.tool_call_id in indexed:
            continue
        indexed[block.tool_call_id] = result.model_copy(deep=True)
    repaired_assistant = _repair_tool_arguments(assistant)
    output = [repaired_assistant]
    for call in calls:
        matched = indexed.get(call.tool_call_id)
        if matched is not None:
            output.append(matched)
            continue
        if current and state.pending_tool_call_id == call.tool_call_id:
            continue
        output.append(_missing_tool_result(assistant, call))
    return tuple(output)


def _repair_tool_arguments(message: Message) -> Message:
    content: list[StoredBlock] = []
    for block in message.content:
        if not isinstance(block, ToolCallBlock) or (
            block.arguments is not None and block.parse_error is None
        ):
            content.append(block.model_copy(deep=True))
            continue
        content.append(
            block.model_copy(
                update={
                    "arguments": {},
                    "raw_arguments": "{}",
                    "parse_error": None,
                },
                deep=True,
            )
        )
    return message.model_copy(update={"content": tuple(content)}, deep=True)


def _missing_tool_result(assistant: Message, call: ToolCallBlock) -> Message:
    if assistant.run_id is None:
        raise ValueError("历史 Assistant 缺少 run_id")
    invalid_arguments = call.arguments is None or call.parse_error is not None
    error_code = (
        "invalid_tool_arguments" if invalid_arguments else "tool_result_missing"
    )
    message = (
        call.parse_error or "工具参数无效"
        if invalid_arguments
        else "工具执行结果缺失，可按需重试"
    )
    content = json.dumps(
        {"code": error_code, "message": message, "status": "error"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return Message(
        message_id=f"repair:{assistant.message_id}:{call.tool_call_id}",
        session_id=assistant.session_id,
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                content=content,
                is_error=True,
                error_code=error_code,
            ),
        ),
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id=assistant.run_id,
        created_at=assistant.created_at,
    )


def _tool_call_ids(message: Message) -> tuple[str, ...]:
    if message.role is not Role.ASSISTANT:
        return ()
    return tuple(
        block.tool_call_id
        for block in message.content
        if isinstance(block, ToolCallBlock)
    )


def _collect_tool_results(
    messages: tuple[Message, ...],
    start: int,
) -> tuple[tuple[Message, ...], int]:
    end = start
    while end < len(messages) and messages[end].role is Role.TOOL:
        end += 1
    return messages[start:end], end


def _order_results(
    call_ids: tuple[str, ...],
    results: tuple[Message, ...],
) -> tuple[Message, ...]:
    by_id: dict[str, Message] = {}
    for message in results:
        block = message.content[0]
        if not isinstance(block, ToolResultBlock):
            raise ValueError("TOOL Message 缺少 ToolResultBlock")
        if block.tool_call_id not in call_ids or block.tool_call_id in by_id:
            raise ValueError("ToolResult 未知或重复")
        by_id[block.tool_call_id] = message
    return tuple(by_id[call_id] for call_id in call_ids if call_id in by_id)


def _validate_current_ids(messages: tuple[Message, ...], state: AgentState) -> None:
    ids = {message.message_id for message in messages}
    missing = set(state.current_message_ids).difference(ids)
    if missing:
        raise ValueError(
            f"current_message_ids 不在最终上下文：{', '.join(sorted(missing))}"
        )


def _validate_unique_ids(messages: tuple[Message, ...]) -> None:
    ids = [message.message_id for message in messages]
    if len(set(ids)) != len(ids):
        raise ValueError("最终上下文包含重复 Message ID")


def _compaction_block(source: ContextMessage) -> CompactionBlock | None:
    message = source.latest_compaction_message
    if message is None:
        return None
    block = message.content[0]
    if not isinstance(block, CompactionBlock):
        raise ValueError("Compaction Message 无效")
    return block


def _prune_tool_message(message: Message, config: CompactionConfig) -> Message:
    """从原始 Message 生成确定性工具发送副本。"""
    content = tuple(_prune_tool_block(block, config) for block in message.content)
    return message.model_copy(update={"content": content}, deep=True)


def _prune_tool_block(
    block: StoredBlock,
    config: CompactionConfig,
) -> StoredBlock:
    if isinstance(block, ToolCallBlock):
        if block.arguments is None:
            return block.model_copy(deep=True)
        arguments = {
            key: _prune_json_value(
                value, config.tool_argument_max_chars, block.tool_call_id
            )
            for key, value in block.arguments.items()
        }
        if arguments == block.arguments:
            return block.model_copy(deep=True)
        raw = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return block.model_copy(
            update={"arguments": arguments, "raw_arguments": raw},
            deep=True,
        )
    if isinstance(block, ToolResultBlock) and (
        len(block.content) > config.tool_result_max_chars
    ):
        return block.model_copy(
            update={
                "content": _pruned_text(
                    block.content,
                    config.tool_result_max_chars,
                    block.tool_call_id,
                    "result",
                )
            },
            deep=True,
        )
    return block.model_copy(deep=True)


def _prune_json_value(value: JsonValue, max_chars: int, tool_call_id: str) -> JsonValue:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return _pruned_text(value, max_chars, tool_call_id, "arguments")
    if isinstance(value, list):
        return [_prune_json_value(item, max_chars, tool_call_id) for item in value]
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, JsonValue], value)
        return {
            key: _prune_json_value(item, max_chars, tool_call_id)
            for key, item in mapping.items()
        }
    return value


def _pruned_text(value: str, max_chars: int, tool_call_id: str, part: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return (
        f'<pruned-tool-content tool_call_id="{html.escape(tool_call_id, quote=True)}" '
        f'part="{part}" original_chars="{len(value)}" sha256="{digest}">\n'
        f"{html.escape(value[:max_chars], quote=False)}\n</pruned-tool-content>"
    )
