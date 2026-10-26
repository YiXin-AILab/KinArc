"""逐项 timeout、决策校验和 payload 传递的 MiddlewareManager。"""

import asyncio
import logging
from dataclasses import replace
from uuid import UUID, uuid5

from agentsystem.common.logging import log_unexpected_exception
from agentsystem.llm.models import LLMRequest, LLMResponse
from agentsystem.message.models import Message, MessageVisibility, Role
from agentsystem.middleware.models import (
    CompactionMiddlewarePayload,
    ErrorMiddlewarePayload,
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePayload,
    MiddlewarePoint,
    MiddlewareRegistration,
    RunMiddlewarePayload,
    RunResultMiddlewarePayload,
    SystemPromptBuildPayload,
    ToolCallMiddlewarePayload,
    ToolResultMiddlewarePayload,
    TurnMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry

_INVOCATION_NAMESPACE = UUID("f08cf981-a543-4bf2-8b13-c7d7583bf19e")
_LOGGER = logging.getLogger(__name__)
_OBSERVATION_POINTS = {
    MiddlewarePoint.AFTER_RUN,
    MiddlewarePoint.AFTER_TURN,
}
_ALLOWED_DECISIONS = {
    MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.UPDATE,
    },
    MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.UPDATE,
    },
    MiddlewarePoint.BEFORE_RUN: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.MODIFY,
        MiddlewareDecisionType.BLOCK,
    },
    MiddlewarePoint.BEFORE_TURN: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.MESSAGES,
        MiddlewareDecisionType.BLOCK,
    },
    MiddlewarePoint.AFTER_TURN: {MiddlewareDecisionType.CONTINUE},
    MiddlewarePoint.BEFORE_LLM: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.REPLACE,
        MiddlewareDecisionType.BLOCK,
    },
    MiddlewarePoint.AFTER_LLM: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.MODIFY,
        MiddlewareDecisionType.RETRY,
        MiddlewareDecisionType.BLOCK,
    },
    MiddlewarePoint.BEFORE_TOOL_CALL: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.MODIFY,
        MiddlewareDecisionType.REPLACE,
        MiddlewareDecisionType.BLOCK,
    },
    MiddlewarePoint.AFTER_TOOL_CALL: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.MODIFY,
    },
    MiddlewarePoint.BEFORE_COMPACTION: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.BLOCK,
    },
    MiddlewarePoint.AFTER_RUN: {MiddlewareDecisionType.CONTINUE},
    MiddlewarePoint.ON_ERROR: {
        MiddlewareDecisionType.CONTINUE,
        MiddlewareDecisionType.RETRY,
        MiddlewareDecisionType.HANDLED,
    },
}
_INPUT_TYPES: dict[MiddlewarePoint, type[object]] = {
    MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD: SystemPromptBuildPayload,
    MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD: SystemPromptBuildPayload,
    MiddlewarePoint.BEFORE_RUN: RunMiddlewarePayload,
    MiddlewarePoint.BEFORE_TURN: TurnMiddlewarePayload,
    MiddlewarePoint.AFTER_TURN: TurnMiddlewarePayload,
    MiddlewarePoint.BEFORE_LLM: LLMRequest,
    MiddlewarePoint.AFTER_LLM: LLMResponse,
    MiddlewarePoint.BEFORE_TOOL_CALL: ToolCallMiddlewarePayload,
    MiddlewarePoint.AFTER_TOOL_CALL: ToolResultMiddlewarePayload,
    MiddlewarePoint.BEFORE_COMPACTION: CompactionMiddlewarePayload,
    MiddlewarePoint.AFTER_RUN: RunResultMiddlewarePayload,
    MiddlewarePoint.ON_ERROR: ErrorMiddlewarePayload,
}


class MiddlewareManager:
    """使用注入的冻结 Registry 依次执行适用 Middleware。

    管理单项超时、失败策略与决策形状，将修改传给后续 handler；不直接写入消息或状态。
    取消异常继续传播，初始化挂点的 fail-closed 错误交给外层记录。
    """

    def __init__(self, registry: MiddlewareRegistry) -> None:
        """保存只读 Registry；未冻结时拒绝启动。

        Args:
            registry: 已完成启动期注册并冻结的 Middleware 集合。

        Raises:
            ValueError: Registry 尚未冻结。
        """
        if not registry.frozen:
            raise ValueError("MiddlewareRegistry 必须先冻结")
        self._registry = registry

    async def dispatch(self, context: MiddlewareContext) -> MiddlewareDecision:
        """依次执行 handler，并返回组合后的最终决策。

        Args:
            context: 含会话身份、挂点及该挂点输入的调用快照。

        Returns:
            首个终止链的决策，或合并追加消息、累计修改后的决策；无修改时为 CONTINUE。

        Raises:
            ValueError: 输入类型或非观察挂点的返回决策不合法。

        普通 handler 失败按注册策略处理；初始化 fail-closed 异常直接传播。
        """
        _validate_input(context.point, context.payload)
        registrations = self._registry.resolve(
            app_id=context.ctx.app_id,
            point=context.point,
        )
        current = context.payload
        modified = False
        messages_to_append: list[Message] = []
        for registration in registrations:
            decision = await self._invoke(registration, context, current)
            if decision is None:
                continue
            try:
                _validate_decision(context.point, decision, current)
            except Exception as error:
                if context.point not in _OBSERVATION_POINTS:
                    raise
                log_unexpected_exception(
                    _LOGGER,
                    "middleware_failure",
                    error,
                    operation="middleware.invoke",
                    middleware_id=registration.middleware_id,
                    middleware_point=registration.point.value,
                )
                continue
            handled, current, was_modified = _apply_nonterminal_decision(
                context,
                decision,
                current,
                messages_to_append,
            )
            if handled:
                modified = modified or was_modified
                continue
            return decision
        if messages_to_append:
            return MiddlewareDecision(
                type=MiddlewareDecisionType.MESSAGES,
                payload=MessagesMiddlewarePayload(messages=tuple(messages_to_append)),
            )
        if modified:
            return MiddlewareDecision(
                type=MiddlewareDecisionType.MODIFY,
                payload=current,
            )
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    async def _invoke(
        self,
        registration: MiddlewareRegistration,
        base: MiddlewareContext,
        payload: MiddlewarePayload,
    ) -> MiddlewareDecision | None:
        """为单个 handler 派生稳定身份和状态副本，并应用其超时及失败策略。"""
        invocation = replace(
            base,
            invocation_id=_invocation_id(base, registration.middleware_id),
            payload=payload,
            state_snapshot=(
                base.state_snapshot.model_copy(deep=True)
                if base.state_snapshot is not None
                else None
            ),
        )
        try:
            decision = await asyncio.wait_for(
                registration.handler(invocation),
                timeout=registration.timeout_ms / 1_000,
            )
            _LOGGER.info(
                "middleware_decision",
                extra={
                    "middleware_id": registration.middleware_id,
                    "middleware_point": registration.point.value,
                    "decision_type": getattr(decision.type, "value", "invalid"),
                },
            )
            return decision
        except TimeoutError:
            if isinstance(payload, SystemPromptBuildPayload) and (
                registration.failure_mode is MiddlewareFailureMode.FAIL_CLOSED
            ):
                raise
            _LOGGER.warning(
                "middleware_timeout",
                extra={
                    "middleware_id": registration.middleware_id,
                    "middleware_point": registration.point.value,
                },
            )
            return _failure_decision(registration)
        except Exception as error:
            if isinstance(payload, SystemPromptBuildPayload) and (
                registration.failure_mode is MiddlewareFailureMode.FAIL_CLOSED
            ):
                # 初始化没有 run 终态边界，由 Dispatcher 负责唯一一次异常记录。
                raise
            log_unexpected_exception(
                _LOGGER,
                "middleware_failure",
                error,
                operation="middleware.invoke",
                middleware_id=registration.middleware_id,
                middleware_point=registration.point.value,
            )
            return _failure_decision(registration)


def _failure_decision(
    registration: MiddlewareRegistration,
) -> MiddlewareDecision | None:
    if registration.failure_mode is MiddlewareFailureMode.FAIL_OPEN:
        return None
    return MiddlewareDecision(
        type=MiddlewareDecisionType.BLOCK,
        reason_code="middleware_execution_failed",
    )


def _apply_nonterminal_decision(
    context: MiddlewareContext,
    decision: MiddlewareDecision,
    current: MiddlewarePayload,
    messages_to_append: list[Message],
) -> tuple[bool, MiddlewarePayload, bool]:
    """应用继续执行 Middleware 链的 Decision。"""
    if decision.type is MiddlewareDecisionType.CONTINUE:
        return True, current, False
    if decision.type is MiddlewareDecisionType.MODIFY:
        if decision.payload is None:
            raise ValueError("MODIFY 缺少 payload")
        return True, decision.payload, True
    if decision.type is MiddlewareDecisionType.MESSAGES:
        if not isinstance(decision.payload, MessagesMiddlewarePayload):
            raise ValueError("MESSAGES payload 类型错误")
        _validate_messages_to_append(
            context,
            current,
            tuple(messages_to_append),
            decision.payload,
        )
        messages_to_append.extend(decision.payload.messages)
        return True, current, False
    return False, current, False


def _invocation_id(context: MiddlewareContext, middleware_id: str) -> str:
    identity = (
        f"{context.ctx.app_id}/{context.ctx.agent_id}/{context.ctx.session_id}/"
        f"{context.trace_id}/{context.run_id}/{context.point.value}/"
        f"{context.turn_index}/{context.attempt}/{middleware_id}"
    )
    return str(uuid5(_INVOCATION_NAMESPACE, identity))


def _validate_input(point: MiddlewarePoint, payload: MiddlewarePayload) -> None:
    if not isinstance(payload, _INPUT_TYPES[point]):
        raise ValueError(f"{point.value} 输入 payload 类型错误")


def _validate_decision(
    point: MiddlewarePoint,
    decision: MiddlewareDecision,
    current: MiddlewarePayload,
) -> None:
    if not isinstance(decision, MiddlewareDecision):
        raise ValueError("Middleware Handler 返回类型错误")
    if decision.type not in _ALLOWED_DECISIONS[point]:
        raise ValueError(f"{point.value} 不允许 {decision.type.value} 决策")
    requires_payload = decision.type in {
        MiddlewareDecisionType.UPDATE,
        MiddlewareDecisionType.MODIFY,
        MiddlewareDecisionType.REPLACE,
        MiddlewareDecisionType.HANDLED,
        MiddlewareDecisionType.MESSAGES,
    }
    if requires_payload != (decision.payload is not None):
        raise ValueError(f"{decision.type.value} payload 约束错误")
    if decision.payload is None:
        _validate_retry(point, decision, current)
        return
    _validate_output_payload(point, decision, current)


def _validate_retry(
    point: MiddlewarePoint,
    decision: MiddlewareDecision,
    current: MiddlewarePayload,
) -> None:
    if decision.type is not MiddlewareDecisionType.RETRY:
        return
    if point is MiddlewarePoint.ON_ERROR and (
        not isinstance(current, ErrorMiddlewarePayload) or current.operation != "llm"
    ):
        raise ValueError("ON_ERROR 只有 LLM 错误允许 RETRY")


def _validate_output_payload(
    point: MiddlewarePoint,
    decision: MiddlewareDecision,
    current: MiddlewarePayload,
) -> None:
    payload = decision.payload
    if payload is None:
        raise ValueError("返回 payload 不能为空")
    expected = _expected_output_type(point, decision.type, current)
    if not isinstance(payload, expected):
        raise ValueError(f"{point.value} 返回 payload 类型错误")
    if isinstance(payload, SystemPromptBuildPayload) and (
        not isinstance(payload.prompt, str) or not payload.prompt.strip()
    ):
        raise ValueError("system prompt UPDATE requires nonempty text")
    if point is MiddlewarePoint.BEFORE_TOOL_CALL:
        _validate_tool_identity(current, payload)
    if point is MiddlewarePoint.AFTER_TOOL_CALL:
        _validate_tool_result_identity(current, payload)
    if point is MiddlewarePoint.ON_ERROR:
        _validate_error_result_identity(current, payload)


def _expected_output_type(
    point: MiddlewarePoint,
    decision_type: MiddlewareDecisionType,
    current: MiddlewarePayload,
) -> type[object]:
    if decision_type is MiddlewareDecisionType.MESSAGES:
        return MessagesMiddlewarePayload
    if point is MiddlewarePoint.BEFORE_LLM:
        return LLMResponse
    if point is MiddlewarePoint.BEFORE_TOOL_CALL:
        if decision_type is MiddlewareDecisionType.REPLACE:
            return ToolResultMiddlewarePayload
        return ToolCallMiddlewarePayload
    if point is MiddlewarePoint.ON_ERROR:
        if not isinstance(current, ErrorMiddlewarePayload):
            raise ValueError("ON_ERROR 当前 payload 类型错误")
        if current.operation == "llm":
            return LLMResponse
        if current.operation == "tool":
            return ToolResultMiddlewarePayload
        raise ValueError(f"{current.operation} 错误不允许 HANDLED")
    return _INPUT_TYPES[point]


def _validate_messages_to_append(
    context: MiddlewareContext,
    current: MiddlewarePayload,
    accumulated: tuple[Message, ...],
    payload: MessagesMiddlewarePayload,
) -> None:
    """校验准备由 Runtime 持久化的完整提醒 Message。"""
    if not isinstance(current, TurnMiddlewarePayload):
        raise ValueError("MESSAGES 只接受 TurnMiddlewarePayload")
    known_ids = {item.message_id for item in current.messages}
    known_ids.update(item.message_id for item in accumulated)
    for message in payload.messages:
        if not isinstance(message, Message):
            raise ValueError("MESSAGES 必须包含完整 Message")
        Message.model_validate(message.model_dump(mode="python"))
        if message.message_id in known_ids:
            raise ValueError("MESSAGES message_id 重复")
        if message.session_id != context.ctx.session_id:
            raise ValueError("MESSAGES session_id 不匹配")
        if message.run_id != context.run_id:
            raise ValueError("MESSAGES run_id 不匹配")
        if message.role is not Role.USER:
            raise ValueError("MESSAGES 只允许 USER Message")
        if message.visibility is not MessageVisibility.MODEL:
            raise ValueError("MESSAGES 只允许 MODEL visibility")
        if any(
            value is not None
            for value in (message.model, message.stop_reason, message.error)
        ):
            raise ValueError("MESSAGES 不允许 Assistant 专属字段")
        known_ids.add(message.message_id)


def _validate_tool_identity(
    current: MiddlewarePayload,
    replacement: MiddlewarePayload,
) -> None:
    if not isinstance(current, ToolCallMiddlewarePayload):
        raise ValueError("Tool 当前 payload 类型错误")
    call = (
        replacement.call
        if isinstance(
            replacement, (ToolCallMiddlewarePayload, ToolResultMiddlewarePayload)
        )
        else None
    )
    if call is None or (
        call.tool_call_id != current.call.tool_call_id
        or call.tool_name != current.call.tool_name
    ):
        raise ValueError("Tool Middleware 不能改变调用身份")
    if isinstance(replacement, ToolResultMiddlewarePayload):
        result = replacement.result
        if (
            result.tool_call_id != current.call.tool_call_id
            or result.tool_name != current.call.tool_name
        ):
            raise ValueError("ToolResult 身份必须匹配原调用")


def _validate_tool_result_identity(
    current: MiddlewarePayload,
    replacement: MiddlewarePayload,
) -> None:
    if not isinstance(current, ToolResultMiddlewarePayload) or not isinstance(
        replacement, ToolResultMiddlewarePayload
    ):
        raise ValueError("ToolResult payload 类型错误")
    identities = (
        current.call.tool_call_id,
        current.call.tool_name,
        current.result.tool_call_id,
        current.result.tool_name,
    )
    replacements = (
        replacement.call.tool_call_id,
        replacement.call.tool_name,
        replacement.result.tool_call_id,
        replacement.result.tool_name,
    )
    if identities != replacements:
        raise ValueError("ToolResult Middleware 不能改变调用身份")


def _validate_error_result_identity(
    current: MiddlewarePayload,
    replacement: MiddlewarePayload,
) -> None:
    if not isinstance(current, ErrorMiddlewarePayload) or current.operation != "tool":
        return
    call = current.tool_call
    if call is None or not isinstance(replacement, ToolResultMiddlewarePayload):
        raise ValueError("Tool ON_ERROR 缺少调用身份")
    identities = (call.tool_call_id, call.tool_name)
    if identities != (
        replacement.call.tool_call_id,
        replacement.call.tool_name,
    ) or identities != (
        replacement.result.tool_call_id,
        replacement.result.tool_name,
    ):
        raise ValueError("Tool ON_ERROR 不能改变调用身份")
