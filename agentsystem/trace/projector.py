"""把 AgentEvent 映射为标准 OpenTelemetry 与 GenAI Span。"""

import json
from collections.abc import Mapping

from opentelemetry.trace import SpanKind
from opentelemetry.util.types import AttributeValue

from agentsystem.common.models import SessionContext
from agentsystem.llm.models import LLMResponse, Usage
from agentsystem.message.models import (
    AssetSource,
    AudioBlock,
    Base64Source,
    DocumentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UrlSource,
    VideoBlock,
)
from agentsystem.runtime.events import (
    AgentEventEnvelope,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    LLMCallCompleted,
    LLMCallStarted,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    MessagesCompleted,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunResumed,
    RunStarted,
    RunSuspended,
    ToolExecutionCancelled,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    ToolExecutionSuspended,
    TurnCompleted,
    TurnStarted,
)
from agentsystem.trace.otel import OTelTraceRecorder

_OPERATION_NAME = "gen_ai.operation.name"
_AGENT_ID = "gen_ai.agent.id"
_CONVERSATION_ID = "gen_ai.conversation.id"
_PROVIDER_NAME = "gen_ai.provider.name"
_REQUEST_ID = "llm.request_id"
_REQUEST_MODEL = "gen_ai.request.model"
_RESPONSE_ID = "gen_ai.response.id"
_RESPONSE_MODEL = "gen_ai.response.model"
_FINISH_REASONS = "gen_ai.response.finish_reasons"
_TOOL_CALL_ID = "gen_ai.tool.call.id"
_TOOL_NAME = "gen_ai.tool.name"
_INPUT_MESSAGES = "gen_ai.input.messages"
_OUTPUT_MESSAGES = "gen_ai.output.messages"
_TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
_TOOL_RESULT = "gen_ai.tool.call.result"
_COMPACTION_SELECTED = "agentsystem.compaction.selected"
_ERROR_ID = "agentsystem.error.id"


class TraceProjector:
    """集中执行 AgentEvent 到 OTel/GenAI Span 的映射。

    依赖具体 OTelTraceRecorder 维护 run、turn、模型和工具的父子生命周期，
    自身不持有 exporter，也不关闭 recorder。内容采集由显式开关控制，
    Base64 媒体只记录长度元数据；开启采集并不等同于通用文本脱敏。
    """

    def __init__(
        self,
        recorder: OTelTraceRecorder,
        *,
        capture_content: bool = False,
    ) -> None:
        """注入具体 recorder 与显式内容采集策略。

        Args:
            recorder: Application 管理的 Span 生命周期记录器。
            capture_content: 是否记录消息、工具参数和结果内容，默认关闭。
        """
        self._recorder = recorder
        self._capture_content = capture_content

    async def project(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        """按事件类型同步更新 Span；Message 流事件不产生 Span。

        Args:
            ctx: 当前 Agent 和 session 的隔离上下文。
            event: 按执行顺序交付的内部事件。

        Raises:
            ValueError: session 不一致，或 recorder 所需的父 Span 不存在。
            RuntimeError: recorder 已关闭，或 RunFailed 缺少完整错误诊断。
        """
        if event.session_id != ctx.session_id:
            raise ValueError("Event 与 SessionContext 的 session_id 不一致")
        payload = event.payload
        if isinstance(
            payload,
            (
                MessagesCompleted,
                MessageBlockStarted,
                MessageBlockDelta,
                MessageBlockCompleted,
            ),
        ):
            return
        if isinstance(payload, ContextCompactionStarted):
            self._start_compaction(event)
            return
        if isinstance(payload, ContextCompactionCompleted):
            self._complete_compaction(event, payload)
            return
        if isinstance(
            payload,
            (RunStarted, RunResumed, TurnStarted, LLMCallStarted, ToolExecutionStarted),
        ):
            self._project_start(ctx, event)
            return
        self._project_end(event)

    def _project_start(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        payload = event.payload
        if isinstance(payload, (RunStarted, RunResumed)):
            self._start_agent(ctx, event)
            if isinstance(payload, RunResumed) and event.turn_index is not None:
                self._start_turn(event, event.turn_index)
        elif isinstance(payload, TurnStarted):
            self._start_turn(event, payload.turn_index)
        elif isinstance(payload, LLMCallStarted):
            self._start_llm(event, payload)
        elif isinstance(payload, ToolExecutionStarted):
            self._start_tool(event, payload)

    def _project_end(self, event: AgentEventEnvelope) -> None:
        payload = event.payload
        if isinstance(payload, LLMCallCompleted):
            self._end_llm(event, payload)
        elif isinstance(
            payload,
            (
                ToolExecutionCompleted,
                ToolExecutionSuspended,
                ToolExecutionCancelled,
            ),
        ):
            self._end_tool(event, payload)
        elif isinstance(payload, TurnCompleted):
            self._recorder.end_span(
                key=_turn_key(event),
                ended_at=event.created_at,
            )
        elif isinstance(payload, (RunCompleted, RunSuspended, RunCanceled, RunFailed)):
            self._end_run(event, payload)

    def _start_agent(
        self,
        ctx: SessionContext,
        event: AgentEventEnvelope,
    ) -> None:
        attributes: dict[str, AttributeValue] = {
            _OPERATION_NAME: "invoke_agent",
            _AGENT_ID: ctx.agent_id,
            _CONVERSATION_ID: ctx.session_id,
        }
        payload = event.payload
        if self._capture_content and isinstance(payload, RunStarted):
            content = self._serialize_content(
                [self._message_content(message) for message in payload.initial_messages]
            )
            if content is not None:
                attributes[_INPUT_MESSAGES] = content
        self._recorder.start_span(
            key=_agent_key(event),
            name="invoke_agent",
            started_at=event.created_at,
            attributes=attributes,
        )

    def _start_turn(self, event: AgentEventEnvelope, turn_index: int) -> None:
        del turn_index
        self._recorder.start_span(
            key=_turn_key(event),
            parent_key=_agent_key(event),
            name="agent turn",
            started_at=event.created_at,
        )

    def _start_compaction(self, event: AgentEventEnvelope) -> None:
        """为整个压缩过程建立 span，摘要尝试作为其子 span。"""
        self._recorder.start_span(
            key=_compaction_key(event),
            parent_key=_turn_key(event),
            name="context compaction",
            started_at=event.created_at,
        )

    def _start_llm(
        self,
        event: AgentEventEnvelope,
        payload: LLMCallStarted,
    ) -> None:
        attributes: dict[str, AttributeValue] = {
            _OPERATION_NAME: "chat",
            _PROVIDER_NAME: payload.provider_name,
            _REQUEST_ID: payload.request_id,
            _REQUEST_MODEL: payload.model,
        }
        if self._capture_content and payload.request is not None:
            content = self._serialize_content(
                [self._message_content(message) for message in payload.request.messages]
            )
            if content is not None:
                attributes[_INPUT_MESSAGES] = content
        self._recorder.start_span(
            key=_llm_key(event, payload.request_id),
            parent_key=_turn_key(event),
            name=f"chat {payload.model}",
            kind=SpanKind.CLIENT,
            started_at=event.created_at,
            attributes=attributes,
        )

    def _end_llm(
        self,
        event: AgentEventEnvelope,
        payload: LLMCallCompleted,
    ) -> None:
        response = payload.response
        attributes: dict[str, AttributeValue] = {
            _FINISH_REASONS: (response.finish_reason.value,),
            _RESPONSE_MODEL: response.model,
            **_usage_attributes(response.usage),
        }
        if response.response_id is not None:
            attributes[_RESPONSE_ID] = response.response_id
        if self._capture_content:
            parts = [
                part
                for block in response.blocks
                if (part := _content_part(block)) is not None
            ]
            content = self._serialize_content(
                [
                    {
                        "role": "assistant",
                        "parts": parts,
                        "finish_reason": response.finish_reason.value,
                    }
                ]
            )
            if content is not None:
                attributes[_OUTPUT_MESSAGES] = content
        self._recorder.end_span(
            key=_llm_key(event, payload.request_id),
            ended_at=response.timing.ended_at,
            attributes=attributes,
            error_type=response.error.code if response.error else None,
        )

    def _start_tool(
        self,
        event: AgentEventEnvelope,
        payload: ToolExecutionStarted,
    ) -> None:
        attributes: dict[str, AttributeValue] = {
            _OPERATION_NAME: "execute_tool",
            _TOOL_CALL_ID: payload.tool_call_id,
            _TOOL_NAME: payload.tool_name,
        }
        if self._capture_content:
            arguments: object = (
                payload.arguments
                if payload.arguments is not None
                else payload.raw_arguments
            )
            content = self._serialize_content(arguments)
            if content is not None:
                attributes[_TOOL_ARGUMENTS] = content
        self._recorder.start_span(
            key=_tool_key(event, payload.tool_call_id),
            parent_key=_turn_key(event),
            name=f"execute_tool {payload.tool_name}",
            started_at=event.created_at,
            attributes=attributes,
        )

    def _end_tool(
        self,
        event: AgentEventEnvelope,
        payload: (
            ToolExecutionCompleted | ToolExecutionSuspended | ToolExecutionCancelled
        ),
    ) -> None:
        attributes: dict[str, AttributeValue] = {}
        error_type: str | None = None
        ended_at = event.created_at
        result: object | None = None
        if isinstance(payload, ToolExecutionCompleted):
            ended_at = payload.timing.ended_at
            result = {
                "content": payload.result.content,
                "is_error": payload.result.is_error,
            }
            if payload.result.is_error:
                error_type = payload.result.error_code or "tool_error"
                result["error_code"] = error_type
        elif isinstance(payload, ToolExecutionSuspended):
            result = {"status": "suspended"}
        elif isinstance(payload, ToolExecutionCancelled):
            result = {"status": "canceled", "reason_code": payload.reason_code}
        if self._capture_content and result is not None:
            content = self._serialize_content(result)
            if content is not None:
                attributes[_TOOL_RESULT] = content
        self._recorder.end_span(
            key=_tool_key(event, payload.tool_call_id),
            ended_at=ended_at,
            attributes=attributes,
            error_type=error_type,
        )

    def _complete_compaction(
        self,
        event: AgentEventEnvelope,
        payload: ContextCompactionCompleted,
    ) -> None:
        """用响应原始时间补记摘要尝试子 Span，再闭合整个压缩 Span。"""
        for index, (request, response) in enumerate(payload.summary_calls):
            key = _compaction_attempt_key(event, index)
            start_attributes: dict[str, AttributeValue] = {
                _OPERATION_NAME: "chat",
                _PROVIDER_NAME: payload.summary_provider_name or "",
                _REQUEST_MODEL: request.config.model,
            }
            if self._capture_content:
                content = self._serialize_content(
                    [self._message_content(message) for message in request.messages]
                )
                if content is not None:
                    start_attributes[_INPUT_MESSAGES] = content
            self._recorder.start_span(
                key=key,
                parent_key=_compaction_key(event),
                name=f"compact {request.config.model} attempt {index + 1}",
                kind=SpanKind.CLIENT,
                started_at=response.timing.started_at,
                attributes=start_attributes,
            )
            end_attributes = self._compaction_response_attributes(
                response,
                selected=index == payload.selected_summary_index,
            )
            self._recorder.end_span(
                key=key,
                ended_at=response.timing.ended_at,
                attributes=end_attributes,
                error_type=response.error.code if response.error else None,
            )
        self._recorder.end_span(
            key=_compaction_key(event),
            ended_at=event.created_at,
            attributes={
                "agentsystem.compaction.action": payload.action,
                "agentsystem.compaction.before_tokens": payload.before_tokens,
                "agentsystem.compaction.after_tokens": payload.after_tokens,
            },
        )

    def _compaction_response_attributes(
        self,
        response: LLMResponse,
        *,
        selected: bool,
    ) -> dict[str, AttributeValue]:
        """构造单次摘要响应的结束属性。"""
        attributes: dict[str, AttributeValue] = {
            _FINISH_REASONS: (response.finish_reason.value,),
            _RESPONSE_MODEL: response.model,
            _COMPACTION_SELECTED: selected,
            **_usage_attributes(response.usage),
        }
        if response.response_id is not None:
            attributes[_RESPONSE_ID] = response.response_id
        if self._capture_content:
            parts = [
                part
                for block in response.blocks
                if (part := _content_part(block)) is not None
            ]
            content = self._serialize_content([{"role": "assistant", "parts": parts}])
            if content is not None:
                attributes[_OUTPUT_MESSAGES] = content
        return attributes

    def _end_run(
        self,
        event: AgentEventEnvelope,
        payload: RunCompleted | RunSuspended | RunCanceled | RunFailed,
    ) -> None:
        """闭合 run 及活动 turn，失败时要求并附加完整诊断以保留故障关联。"""
        attributes: dict[str, AttributeValue] = {}
        error_type: str | None = None
        error_description: str | None = None
        exception_type: str | None = None
        exception_stacktrace: str | None = None
        if isinstance(payload, RunFailed):
            error_id = payload.error.error_id
            exception_type = payload.error.exception_type
            exception_stacktrace = payload.error.stacktrace
            if (
                error_id is None
                or exception_type is None
                or exception_stacktrace is None
            ):
                raise RuntimeError("RunFailed 缺少完整错误诊断")
            attributes[_ERROR_ID] = error_id
            error_type = payload.error.code
            error_description = payload.error.message
            self._recorder.end_span(
                key=_compaction_key(event),
                ended_at=event.created_at,
                error_type=payload.error.code,
            )
        self._recorder.end_span(key=_turn_key(event), ended_at=event.created_at)
        if self._capture_content and isinstance(payload, RunCompleted):
            content = self._serialize_content(
                [self._message_content(payload.final_message)]
            )
            if content is not None:
                attributes[_OUTPUT_MESSAGES] = content
        self._recorder.end_span(
            key=_agent_key(event),
            ended_at=event.created_at,
            attributes=attributes,
            error_type=error_type,
            error_description=error_description,
            exception_type=exception_type,
            exception_message=error_description,
            exception_stacktrace=exception_stacktrace,
        )

    def _message_content(self, message: Message) -> dict[str, object]:
        parts = [
            part
            for block in message.content
            if (part := _content_part(block)) is not None
        ]
        return {
            "role": message.role.value,
            "parts": parts,
        }

    def _serialize_content(self, value: object) -> str | None:
        try:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return None
        return rendered


def _content_part(block: object) -> dict[str, object] | None:
    """映射可采集内容；Base64 只记录长度，内部上下文块不公开到 Trace 内容。"""
    if isinstance(block, TextBlock):
        return {"type": "text", "content": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "reasoning", "content": block.thinking}
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_call",
            "id": block.tool_call_id,
            "name": block.tool_name,
            "arguments": (
                block.arguments if block.arguments is not None else block.raw_arguments
            ),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_call_response",
            "id": block.tool_call_id,
            "result": block.content,
        }
    if isinstance(block, (ImageBlock, AudioBlock, VideoBlock, DocumentBlock)):
        source = block.source
        if isinstance(source, Base64Source):
            return {
                "type": "blob",
                "modality": block.type,
                "mime_type": source.media_type,
                "base64_chars": len(source.data),
                "content_omitted": True,
            }
        uri = (
            source.url
            if isinstance(source, UrlSource)
            else f"asset://{source.asset_id}"
        )
        part: dict[str, object] = {
            "type": "uri",
            "modality": block.type,
            "mime_type": source.media_type,
            "uri": uri,
        }
        if isinstance(source, AssetSource) and source.filename is not None:
            part["filename"] = source.filename
        return part
    return None


def _usage_attributes(usage: Usage) -> dict[str, AttributeValue]:
    attributes: dict[str, AttributeValue] = {
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
    }
    optional: Mapping[str, int | None] = {
        "gen_ai.usage.cache_read.input_tokens": usage.cache_read_tokens,
        "gen_ai.usage.cache_creation.input_tokens": usage.cache_write_tokens,
        "gen_ai.usage.reasoning.output_tokens": usage.reasoning_tokens,
    }
    attributes.update(
        {key: value for key, value in optional.items() if value is not None}
    )
    return attributes


def _agent_key(event: AgentEventEnvelope) -> str:
    return f"agent:{event.trace_id}"


def _turn_key(event: AgentEventEnvelope) -> str:
    return f"turn:{event.trace_id}:{event.turn_index}"


def _llm_key(event: AgentEventEnvelope, request_id: str) -> str:
    return f"llm:{event.trace_id}:{request_id}"


def _tool_key(event: AgentEventEnvelope, tool_call_id: str) -> str:
    return f"tool:{event.trace_id}:{tool_call_id}"


def _compaction_key(event: AgentEventEnvelope) -> str:
    return f"compaction:{event.trace_id}:{event.turn_index}"


def _compaction_attempt_key(
    event: AgentEventEnvelope,
    attempt_index: int,
) -> str:
    return f"{_compaction_key(event)}:{attempt_index}"
