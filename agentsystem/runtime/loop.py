"""只产生 AgentEvent、通过异步提交函数建立保存屏障的 ReAct Loop。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

from agentsystem.common.errors import (
    EventCommitError,
    RunError,
    RunFailure,
    format_run_failure,
    new_error_id,
)
from agentsystem.common.logging import log_run_failure, log_unexpected_exception
from agentsystem.common.models import JsonValue
from agentsystem.context.builder import ContextBuilder
from agentsystem.context.compactor import (
    CompactionResult,
    Compactor,
    context_token_request_fingerprint,
)
from agentsystem.context.providers import ContextProviderRunner
from agentsystem.inbox.models import InboundMessage, InboundMode, RunRequest
from agentsystem.inbox.protocol import Inbox
from agentsystem.llm.client import LLMClient
from agentsystem.llm.models import (
    LLMBlockDelta,
    LLMBlockEnd,
    LLMBlockStart,
    LLMEnd,
    LLMEvent,
    LLMGenerationConfig,
    LLMRequest,
    LLMResponse,
    LLMStart,
    Timing,
    Usage,
    UsageAccounting,
)
from agentsystem.message.manager import MessageManager
from agentsystem.message.models import (
    CompactionBlock,
    ContextMessage,
    InjectedBlock,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    ToolCallBlock,
    ToolResultBlock,
    is_inbound_message,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    ErrorMiddlewarePayload,
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePayload,
    MiddlewarePoint,
    RunMiddlewarePayload,
    ToolCallMiddlewarePayload,
    ToolResultMiddlewarePayload,
    TurnMiddlewarePayload,
)
from agentsystem.runtime.events import (
    AgentEventPayload,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    LLMCallCompleted,
    LLMCallStarted,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    MessagesCompleted,
    MessageStarted,
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
from agentsystem.runtime.state import (
    AgentState,
    SessionStatus,
    _replace_state,
    cancel_run,
    clear_context_token_checkpoint,
    complete_run,
    fail_run,
    resume_run,
    run_timing,
    set_context_token_checkpoint,
    start_run,
    start_turn,
    suspend_run,
)
from agentsystem.tool.context import ToolInvocationContext
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolExecutionResult, ToolSuspension
from agentsystem.tool.registry import ToolRegistry

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class NewRunInput:
    """保存 Session 已领取的新 run 输入，不负责领取或持久化。

    Attributes:
        messages: 已关联当前 session/run 的 USER 或 SYSTEM 消息。
        inbound_modes: 输入身份与领取模式的映射，供事件提交后确认交接。
    """

    messages: tuple[Message, ...]
    inbound_modes: Mapping[str, InboundMode] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResumeInput:
    """保存 Session 预校验后的恢复凭证，供 Loop 再次验证并清除。

    Attributes:
        resume_token: 对应当前 PENDING run 的单次恢复凭证。
    """

    resume_token: str


type LoopInput = NewRunInput | ResumeInput
type EmitEvent = Callable[[AgentEventPayload], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _PreparedContext:
    """一次 LLM 调用使用的不可变上下文。"""

    messages: tuple[Message, ...]
    tool_schemas: tuple[dict[str, JsonValue], ...]


class AgentLoop:
    """编排一个执行段的 ReAct 控制流，并维护其不可变状态快照。

    依赖领域消息读取、上下文构建、模型和工具端口；可以在工具循环安全点
    领取 STEER，但消息和状态写入统一交给 Session 注入的事件提交函数。
    每次 emit 返回才继续执行，因此投影失败必须越过普通失败收敛逻辑。
    新 run 与恢复仅前置步骤不同，之后共用同一循环；实例不供并发 run 复用。
    """

    def __init__(
        self,
        *,
        initial_state: AgentState,
        message_manager: MessageManager,
        context_builder: ContextBuilder,
        context_provider_runner: ContextProviderRunner | None = None,
        compactor: Compactor,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor | None = None,
        inbox: Inbox | None = None,
        middleware_manager: MiddlewareManager | None = None,
        generation_config: LLMGenerationConfig,
        middleware_llm_retry_limit: int = 1,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """注入执行依赖并复制初始状态，不创建外部连接。

        Args:
            initial_state: 执行段起始状态，构造时深复制。
            message_manager: 权威上下文读取端口。
            context_builder: 根据消息和状态构建模型上下文。
            context_provider_runner: 可选动态上下文注入器。
            compactor: 压缩阈值判断与候选快照生成器。
            llm_client: 提供归一化模型事件流的客户端。
            tool_registry: 本 Agent 的工具定义和 schema 快照。
            tool_executor: 单次工具调用执行器；有工具调用时必须配置。
            inbox: 工具循环安全点领取 STEER 的可选端口。
            middleware_manager: 可选生命周期中间件执行器。
            generation_config: 每次模型调用使用的生成配置。
            middleware_llm_retry_limit: 中间件允许请求的额外模型尝试次数。
            clock: 时间来源，默认当前 UTC 时间。
            id_factory: 请求及消息身份来源，默认 UUID。

        Raises:
            ValueError: 中间件重试次数为负。
        """
        if middleware_llm_retry_limit < 0:
            raise ValueError("Middleware LLM retry limit 不能为负数")
        self._state = initial_state.model_copy(deep=True)
        self._messages = message_manager
        self._context_builder = context_builder
        self._context_providers = context_provider_runner
        self._compactor = compactor
        self._llm_client = llm_client
        self._tools = tool_registry
        self._tool_executor = tool_executor
        self._inbox = inbox
        self._middleware = middleware_manager
        self._generation_config = generation_config
        self._middleware_llm_retry_limit = middleware_llm_retry_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    @property
    def state(self) -> AgentState:
        """返回当前冻结的 post-state，供 Session 在事件提交边界复制保存。"""
        return self._state

    # ReAct 编排刻意保留终态顺序，避免为圈复杂度再次隐藏关键分支。
    async def run(  # noqa: C901
        self,
        request: RunRequest,
        loop_input: LoopInput,
        emit: EmitEvent,
    ) -> AgentState:
        """执行新 run 或恢复，逐事件等待保存屏障并收敛普通执行失败。

        Args:
            request: Session 在持锁后分配的运行身份与输出模式。
            loop_input: 初始输入消息或当前挂起 run 的恢复凭证。
            emit: 异步事件提交函数；成功返回表示该事件保存屏障完成。

        Returns:
            挂起或终态时的状态深复制。

        Raises:
            ValueError: 请求所属会话与初始状态不同。
            EventCommitError: 事件提交失败；不再尝试业务失败投影。
            asyncio.CancelledError: 执行被取消，由 Session 判定取消来源。

        普通执行异常会记录并进入失败收敛；收敛自身的异常继续传播。
        """
        _validate_request(request, self._state)
        try:
            if isinstance(loop_input, ResumeInput):
                suspended = await self._resume_current_turn(
                    request,
                    loop_input,
                    emit,
                )
                if suspended:
                    return self._state.model_copy(deep=True)
            else:
                await self._start_new_run(request, loop_input, emit)

            while True:
                self._state = start_turn(self._state)
                await emit(TurnStarted(turn_index=self._state.turn_index))

                prepared = await self._prepare_context(request, emit)
                assistant = await self._call_llm(
                    request,
                    prepared.messages,
                    prepared.tool_schemas,
                    emit,
                )
                self._state = _replace_state(self._state, current_message_ids=())
                await emit(MessagesCompleted(messages=(assistant,)))

                tool_calls = tuple(
                    block
                    for block in assistant.content
                    if isinstance(block, ToolCallBlock)
                )
                if tool_calls:
                    suspended = await self._execute_tools(
                        request,
                        tool_calls,
                        emit,
                    )
                    if suspended:
                        return self._state.model_copy(deep=True)
                    await self._collect_steer(request, emit)

                await emit(TurnCompleted(turn_index=self._state.turn_index))
                await self._notify_after_turn(request)
                if tool_calls:
                    continue

                self._state = complete_run(
                    self._state,
                    final_message_id=assistant.message_id,
                    now=self._clock(),
                )
                await emit(
                    RunCompleted(
                        final_message=assistant,
                        usage=self._state.usage_total,
                        timing=run_timing(self._state),
                    )
                )
                return self._state.model_copy(deep=True)
        except EventCommitError:
            raise
        except RunFailure as run_failure:
            return await self._finalize_run_failure(request, run_failure, emit)
        except Exception as exception:
            unexpected_failure = RunFailure(
                operation="run",
            )
            unexpected_failure.__cause__ = exception
            return await self._finalize_run_failure(
                request,
                unexpected_failure,
                emit,
            )

    async def _finalize_run_failure(
        self,
        request: RunRequest,
        failure: RunFailure,
        emit: EmitEvent,
    ) -> AgentState:
        """在唯一所有权边界观察异常并提交 FAILED 终态。"""
        if self._state.status not in {
            SessionStatus.RUNNING,
            SessionStatus.PENDING,
        }:
            raise failure
        error_id = new_error_id()
        exception_type, stacktrace = format_run_failure(failure)
        log_run_failure(
            LOGGER,
            "agent_loop_failed",
            failure,
            error_id=error_id,
            operation=failure.operation,
            code=failure.code,
        )
        error = RunError(
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
            error_id=error_id,
            exception_type=exception_type,
            stacktrace=stacktrace,
        )
        if failure.notify_middleware:
            await self._notify_error(
                request,
                ErrorMiddlewarePayload(
                    operation=failure.operation,
                    error=error,
                ),
            )
        return await self.finalize(
            request,
            target_status=SessionStatus.FAILED,
            error=error,
            emit=emit,
        )

    async def finalize(
        self,
        request: RunRequest,
        *,
        target_status: SessionStatus,
        error: RunError,
        emit: EmitEvent,
    ) -> AgentState:
        """补齐未闭合工具结果，并把当前活动状态收敛为失败或取消终态。

        Args:
            request: 与当前状态属于同一会话的运行身份。
            target_status: 仅允许 FAILED 或 CANCELED。
            error: 终态携带的结构化原因。
            emit: 负责按顺序保存合成消息和终态事件的提交函数。

        Returns:
            已提交终态事件后的状态深复制。

        Raises:
            ValueError: 会话不匹配、当前非活动状态或目标终态不支持。
            EventCommitError: 合成消息或终态事件投影失败。
        """
        _validate_request(request, self._state)
        if self._state.status not in {SessionStatus.RUNNING, SessionStatus.PENDING}:
            raise ValueError("只有活动 run 可以执行 terminal finalize")
        if target_status not in {SessionStatus.FAILED, SessionStatus.CANCELED}:
            raise ValueError("terminal finalize 只支持 FAILED 或 CANCELED")
        source = await self._messages.read_context_source(request.ctx)
        synthetic_messages: list[Message] = []
        for call in _derive_unresolved_tool_calls(source.retained_messages):
            result = ToolResultBlock(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                content="工具执行结果未知或已中断",
                is_error=True,
            )
            message = _tool_message(
                request,
                result,
                message_id=self._id_factory(),
                created_at=self._clock(),
            )
            synthetic_messages.append(message)
            await emit(
                ToolExecutionCancelled(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    reason_code=error.code,
                    message=message,
                )
            )
        if synthetic_messages:
            await emit(MessagesCompleted(messages=tuple(synthetic_messages)))
        if target_status is SessionStatus.CANCELED:
            self._state = cancel_run(self._state, now=self._clock(), error=error)
            await emit(
                RunCanceled(
                    error=error,
                    usage=self._state.usage_total,
                    timing=run_timing(self._state),
                )
            )
            return self._state.model_copy(deep=True)
        self._state = fail_run(self._state, error=error, now=self._clock())
        await emit(
            RunFailed(
                error=error,
                usage=self._state.usage_total,
                timing=run_timing(self._state),
            )
        )
        return self._state.model_copy(deep=True)

    async def _start_new_run(
        self,
        request: RunRequest,
        loop_input: NewRunInput,
        emit: EmitEvent,
    ) -> None:
        _validate_initial_messages(request, loop_input.messages)
        self._state = start_run(
            self._state,
            run_id=request.run_id,
            output_mode=request.output_mode,
            now=self._clock(),
        )
        message_ids = tuple(message.message_id for message in loop_input.messages)
        self._state = _replace_state(
            self._state,
            current_message_ids=message_ids,
        )
        await emit(RunStarted(initial_messages=loop_input.messages))
        for message in loop_input.messages:
            mode = loop_input.inbound_modes.get(message.message_id)
            if mode is not None:
                await emit(MessageStarted(message=message, mode=mode))
        await emit(
            MessagesCompleted(
                messages=loop_input.messages,
                inbound_modes=loop_input.inbound_modes,
            )
        )
        decision = await self._dispatch_middleware(
            request,
            MiddlewarePoint.BEFORE_RUN,
            RunMiddlewarePayload(
                messages=loop_input.messages,
                output_mode=request.output_mode,
            ),
        )
        if decision.type is MiddlewareDecisionType.BLOCK:
            raise RuntimeError(
                f"BEFORE_RUN blocked: {decision.reason_code or 'policy_blocked'}"
            )
        if decision.type is MiddlewareDecisionType.MODIFY and decision.payload != (
            RunMiddlewarePayload(
                messages=loop_input.messages,
                output_mode=request.output_mode,
            )
        ):
            raise RuntimeError("BEFORE_RUN 不能修改已定稿的初始消息或 output_mode")

    async def _resume_current_turn(
        self,
        request: RunRequest,
        loop_input: ResumeInput,
        emit: EmitEvent,
    ) -> bool:
        """校验未闭合工具差集，提交 token 消费状态后继续原 turn。

        不重复发送 TurnStarted；恢复工具再挂起时立即结束本执行段。
        """
        if self._state.run_id != request.run_id:
            raise ValueError("ResumeInput 只能恢复 AgentState 中的同一个 run")
        pending_tool_call_id = self._state.pending_tool_call_id
        if pending_tool_call_id is None:
            raise ValueError("PENDING state 缺少 pending_tool_call_id")
        resumed_state = resume_run(
            self._state,
            resume_token=loop_input.resume_token,
            now=self._clock(),
        )
        source = await self._messages.read_context_source(request.ctx)
        logical = self._context_builder.build(source, self._state)
        tool_calls = _derive_unresolved_tool_calls(logical)
        if pending_tool_call_id not in {item.tool_call_id for item in tool_calls}:
            raise ValueError("pending ToolCall 不在未闭合工具差集中")
        self._state = resumed_state
        await emit(RunResumed())
        suspended = await self._execute_tools(
            request,
            tool_calls,
            emit,
        )
        if suspended:
            return True
        await self._collect_steer(request, emit)
        await emit(TurnCompleted(turn_index=self._state.turn_index))
        await self._notify_after_turn(request)
        return False

    async def _prepare_context(
        self,
        request: RunRequest,
        emit: EmitEvent,
    ) -> _PreparedContext:
        """装配一次 LLM 调用所需的消息上下文与工具 schema。"""
        source = await self._load_context_with_injection(request, emit)
        prepared = self._build_context(source)
        source, prepared = await self._apply_before_turn(
            request,
            source,
            prepared,
            emit,
        )
        return await self._compact_if_needed(request, source, prepared, emit)

    async def _apply_before_turn(
        self,
        request: RunRequest,
        source: ContextMessage,
        prepared: _PreparedContext,
        emit: EmitEvent,
    ) -> tuple[ContextMessage, _PreparedContext]:
        """接纳 Turn Middleware Message，并在保存后重建权威上下文。"""
        decision = await self._dispatch_middleware(
            request,
            MiddlewarePoint.BEFORE_TURN,
            TurnMiddlewarePayload(messages=prepared.messages),
        )
        if decision.type is MiddlewareDecisionType.BLOCK:
            raise RuntimeError(
                f"BEFORE_TURN blocked: {decision.reason_code or 'policy_blocked'}"
            )
        if decision.type is not MiddlewareDecisionType.MESSAGES:
            return source, prepared
        if not isinstance(decision.payload, MessagesMiddlewarePayload):
            raise TypeError("BEFORE_TURN MESSAGES payload 类型错误")
        await emit(MessagesCompleted(messages=decision.payload.messages))
        refreshed = await self._messages.read_context_source(request.ctx)
        return refreshed, self._build_context(refreshed)

    async def _notify_after_turn(self, request: RunRequest) -> None:
        """以 fail-open 语义通知已经保存完成的 Turn。"""
        if self._middleware is None:
            return
        try:
            source = await self._messages.read_context_source(request.ctx)
            messages = self._context_builder.build(source, self._state)
            await self._dispatch_middleware(
                request,
                MiddlewarePoint.AFTER_TURN,
                TurnMiddlewarePayload(messages=messages),
            )
        except Exception as error:
            log_unexpected_exception(
                LOGGER,
                "after_turn_middleware_notification_failed",
                error,
                operation="middleware.after_turn",
            )

    async def _load_context_with_injection(
        self,
        request: RunRequest,
        emit: EmitEvent,
    ) -> ContextMessage:
        """读取上下文，并在需要时提交注入消息后重新读取。"""
        source = await self._messages.read_context_source(request.ctx)
        if self._context_providers is None:
            return source
        targets = tuple(
            target
            for target in _current_user_targets(source, self._state)
            if not _has_injection(source, target.message_id)
        )
        if not targets:
            return source
        injections = await asyncio.gather(
            *(
                self._context_providers.build_injection(request.ctx, target)
                for target in targets
            )
        )
        await emit(MessagesCompleted(messages=tuple(injections)))
        return await self._messages.read_context_source(request.ctx)

    def _build_context(self, source: ContextMessage) -> _PreparedContext:
        """构建 LLM 消息和当前注册工具的 schema 快照。"""
        return _PreparedContext(
            messages=self._context_builder.build(source, self._state),
            tool_schemas=self._tools.schemas(),
        )

    async def _compact_if_needed(
        self,
        request: RunRequest,
        source: ContextMessage,
        prepared: _PreparedContext,
        emit: EmitEvent,
    ) -> _PreparedContext:
        """在达到阈值时压缩上下文，并从持久化快照重建消息。"""
        expected_latest = _latest_compaction_id(source)
        try:
            should_compact = self._compactor.should_compact(
                source,
                self._state,
                prepared.messages,
                prepared.tool_schemas,
            )
        except Exception as error:
            raise RunFailure(
                operation="compaction",
                code="context_compaction_failed",
                message="Context compaction failed",
            ) from error
        if not should_compact:
            return prepared
        await emit(ContextCompactionStarted())
        try:
            compaction = await self._compactor.compact(
                ctx=request.ctx,
                run_id=request.run_id,
                trace_id=request.trace_id,
                source=source,
                state=self._state,
                request_messages=prepared.messages,
                tools=prepared.tool_schemas,
            )
        except Exception as error:
            raise RunFailure(
                operation="compaction",
                code="context_compaction_failed",
                message="Context compaction failed",
            ) from error
        if compaction is None:
            return prepared
        self._state = set_context_token_checkpoint(
            self._state,
            tokens=compaction.after_tokens,
            message_id=compaction.request_messages[-1].message_id,
            request_fingerprint=context_token_request_fingerprint(
                provider_name=self._llm_client.provider_name,
                model=self._generation_config.model,
                messages=compaction.request_messages,
                tools=prepared.tool_schemas,
            ),
        )
        self._state = _replace_state(
            self._state,
            usage_total=_add_usage(self._state.usage_total, compaction.usage),
        )
        await emit(_context_compaction_completed_event(compaction, expected_latest))
        source = await self._messages.read_context_source(request.ctx)
        if _latest_compaction_id(source) != compaction.compaction_message.message_id:
            raise RuntimeError("Compaction 保存后重读未看到候选 Message")
        return _PreparedContext(
            messages=self._context_builder.build(source, self._state),
            tool_schemas=prepared.tool_schemas,
        )

    # 有限 attempt 状态机保持线性，避免把最终错误契约拆回转发 helper。
    async def _call_llm(  # noqa: C901
        self,
        request: RunRequest,
        request_messages: tuple[Message, ...],
        tool_schemas: tuple[dict[str, JsonValue], ...],
        emit: EmitEvent,
    ) -> Message:
        """执行有限 LLM attempts，并返回由最终成功响应构造的 Assistant。"""
        attempt = 1
        request_id = self._id_factory()
        assistant_message_id = self._id_factory()
        while True:
            llm_request = LLMRequest(
                request_id=request_id,
                messages=request_messages,
                tools=tool_schemas,
                config=self._generation_config,
                output_mode=request.output_mode,
            )
            before = await self._dispatch_middleware(
                request,
                MiddlewarePoint.BEFORE_LLM,
                llm_request,
                attempt=attempt,
            )
            if before.type is MiddlewareDecisionType.BLOCK:
                raise RuntimeError(
                    f"BEFORE_LLM blocked: {before.reason_code or 'policy_blocked'}"
                )
            replacement = (
                before.payload
                if before.type is MiddlewareDecisionType.REPLACE
                and isinstance(before.payload, LLMResponse)
                else None
            )
            await emit(
                LLMCallStarted(
                    request_id=llm_request.request_id,
                    provider_name=self._llm_client.provider_name,
                    model=self._generation_config.model,
                    output_mode=request.output_mode,
                    request=llm_request,
                )
            )
            if replacement is None:
                response = await self._consume_provider_stream(
                    llm_request,
                    assistant_message_id,
                    emit,
                )
            else:
                response = replacement
                await emit(
                    LLMCallCompleted(
                        request_id=llm_request.request_id,
                        response=response,
                    )
                )
            self._state = _replace_state(
                self._state,
                usage_total=_add_usage(self._state.usage_total, response.usage),
            )
            after = await self._dispatch_middleware(
                request,
                MiddlewarePoint.AFTER_LLM,
                response,
                attempt=attempt,
            )
            if after.type is MiddlewareDecisionType.BLOCK:
                raise RuntimeError(
                    f"AFTER_LLM blocked: {after.reason_code or 'policy_blocked'}"
                )
            if after.type is MiddlewareDecisionType.MODIFY and isinstance(
                after.payload,
                LLMResponse,
            ):
                response = after.payload
            retry = after.type is MiddlewareDecisionType.RETRY
            if response.error is not None and not retry:
                on_error = await self._dispatch_middleware(
                    request,
                    MiddlewarePoint.ON_ERROR,
                    ErrorMiddlewarePayload(
                        operation="llm",
                        error=RunError(
                            code=response.error.code,
                            message="LLM provider 返回错误",
                            retryable=response.error.retryable,
                        ),
                    ),
                    attempt=attempt,
                )
                retry = on_error.type is MiddlewareDecisionType.RETRY
                if on_error.type is MiddlewareDecisionType.HANDLED and isinstance(
                    on_error.payload,
                    LLMResponse,
                ):
                    response = on_error.payload
            if retry:
                if attempt > self._middleware_llm_retry_limit:
                    raise RuntimeError("Middleware LLM retry limit exceeded")
                attempt += 1
                request_id = self._id_factory()
                continue
            if response.error is not None:
                raise RunFailure(
                    operation="llm",
                    code=response.error.code,
                    message="LLM provider 返回错误",
                    exception_message=response.error.message,
                    retryable=response.error.retryable,
                    notify_middleware=False,
                )
            if response.usage.total_tokens > 0:
                self._state = set_context_token_checkpoint(
                    self._state,
                    tokens=response.usage.total_tokens,
                    message_id=assistant_message_id,
                    request_fingerprint=context_token_request_fingerprint(
                        provider_name=self._llm_client.provider_name,
                        model=self._generation_config.model,
                        messages=llm_request.messages,
                        tools=llm_request.tools,
                    ),
                )
            else:
                self._state = clear_context_token_checkpoint(self._state)
            return Message(
                message_id=assistant_message_id,
                session_id=request.ctx.session_id,
                role=Role.ASSISTANT,
                content=response.blocks,
                origin=MessageOrigin.MODEL,
                visibility=MessageVisibility.ALL,
                model=response.model,
                run_id=request.run_id,
                created_at=self._clock(),
                stop_reason=response.finish_reason,
            )

    async def _consume_provider_stream(
        self,
        request: LLMRequest,
        assistant_message_id: str,
        emit: EmitEvent,
    ) -> LLMResponse:
        """校验一个 Provider 事件流并转发可观察 Runtime 事件。"""
        started = False
        ended = False
        response: LLMResponse | None = None
        async for provider_event in self._llm_client.generate(request):
            started = _validate_provider_event_order(
                provider_event,
                started=started,
                ended=ended,
            )
            mapped = self._map_llm_event(
                request,
                provider_event,
                assistant_message_id,
            )
            if mapped is None:
                continue
            if isinstance(mapped, LLMCallCompleted):
                ended = True
                response = mapped.response
            await emit(mapped)
        if not started:
            raise RuntimeError("LLMProvider 事件流为空")
        if not ended:
            raise RuntimeError("LLMProvider 事件流缺少 LLMEnd")
        if response is None:
            raise RuntimeError("LLMProvider 事件流缺少 LLMEnd response")
        return response

    @staticmethod
    def _map_llm_event(
        request: LLMRequest,
        event: LLMEvent,
        assistant_message_id: str,
    ) -> AgentEventPayload | None:
        """校验 Provider 关联身份并映射为 Runtime 事件。"""
        if event.request_id != request.request_id:
            raise RuntimeError("LLMProvider event request_id 不一致")
        if isinstance(event, LLMStart):
            if (
                event.model != request.config.model
                or event.output_mode is not request.output_mode
            ):
                raise RuntimeError("LLMProvider start 与请求不一致")
            return None
        if isinstance(event, LLMBlockStart):
            return MessageBlockStarted(
                message_id=assistant_message_id,
                index=event.index,
                block_id=event.block_id,
                block_type=event.block_type,
                tool_name=event.tool_name,
                tool_call_id=event.tool_call_id,
            )
        if isinstance(event, LLMBlockDelta):
            return MessageBlockDelta(
                message_id=assistant_message_id,
                index=event.index,
                block_id=event.block_id,
                block_type=event.block_type,
                delta=event.delta,
            )
        if isinstance(event, LLMBlockEnd):
            return MessageBlockCompleted(
                message_id=assistant_message_id,
                index=event.index,
                block_id=event.block_id,
                block=event.block,
            )
        if isinstance(event, LLMEnd):
            return LLMCallCompleted(
                request_id=request.request_id,
                response=event.response,
            )
        raise RuntimeError(f"未知 LLMProvider event：{type(event).__name__}")

    async def _execute_tools(
        self,
        request: RunRequest,
        tool_calls: tuple[ToolCallBlock, ...],
        emit: EmitEvent,
    ) -> bool:
        """按安全组执行工具，批量保存已完成结果后才提交挂起状态。

        同组任务在正常挂起时自然排空；后续组不再启动。返回是否挂起。
        """
        if self._tool_executor is None:
            raise RuntimeError("Agent 返回 ToolCall，但未配置 ToolExecutor")
        messages: list[Message] = []
        for group in _tool_execution_groups(
            tool_calls,
            self._tools,
            parallel=self._generation_config.parallel_tool_calls is True,
        ):
            for call in group:
                await emit(
                    ToolExecutionStarted(
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        raw_arguments=call.raw_arguments,
                        parse_error=call.parse_error,
                    )
                )
            completed_results, suspension = await _execute_tool_group(
                group,
                lambda call: self._execute_tool_call(request, call),
            )
            for call, result in completed_results:
                message = _tool_message(
                    request,
                    result.block,
                    message_id=self._id_factory(),
                    created_at=self._clock(),
                )
                messages.append(message)
                await emit(
                    ToolExecutionCompleted(
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        result=result.block,
                        message=message,
                        timing=result.timing,
                    )
                )
            if suspension is None:
                continue
            if messages:
                await emit(MessagesCompleted(messages=tuple(messages)))
            self._state = suspend_run(
                self._state,
                tool_call_id=suspension.tool_call_id,
                resume_token=suspension.resume_token,
                now=self._clock(),
                expires_at=suspension.expires_at,
            )
            await emit(
                ToolExecutionSuspended(
                    tool_call_id=suspension.tool_call_id,
                    tool_name=suspension.tool_name,
                    resume_token=suspension.resume_token,
                    suspended_at=suspension.suspended_at,
                    expires_at=suspension.expires_at,
                )
            )
            await emit(
                RunSuspended(
                    resume_token=suspension.resume_token,
                    expires_at=suspension.expires_at,
                )
            )
            return True
        if messages:
            await emit(MessagesCompleted(messages=tuple(messages)))
        return False

    async def _execute_tool_call(
        self,
        request: RunRequest,
        call: ToolCallBlock,
    ) -> tuple[ToolCallBlock, ToolExecutionResult | ToolSuspension]:
        """执行已开始的单个调用，供并发组保留调用身份。"""
        executor = self._tool_executor
        if executor is None:
            raise RuntimeError("Agent 返回 ToolCall，但未配置 ToolExecutor")
        before = await self._dispatch_middleware(
            request,
            MiddlewarePoint.BEFORE_TOOL_CALL,
            ToolCallMiddlewarePayload(call=call),
        )
        active_call = call
        if before.type is MiddlewareDecisionType.MODIFY and isinstance(
            before.payload, ToolCallMiddlewarePayload
        ):
            active_call = before.payload.call
        if before.type is MiddlewareDecisionType.REPLACE and isinstance(
            before.payload, ToolResultMiddlewarePayload
        ):
            result: ToolExecutionResult | ToolSuspension = _middleware_tool_result(
                before.payload.result,
                self._clock(),
            )
        elif before.type is MiddlewareDecisionType.BLOCK:
            result = _middleware_tool_result(
                ToolResultBlock(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    content=(f"tool_blocked:{before.reason_code or 'policy_blocked'}"),
                    is_error=True,
                ),
                self._clock(),
            )
        else:
            invocation_context = ToolInvocationContext(
                session_context=request.ctx,
                run_id=request.run_id,
                runtime_trace_id=request.trace_id,
                tool_call_id=active_call.tool_call_id,
                tool_name=active_call.tool_name,
                turn_index=self._state.turn_index,
            )
            result = await executor.execute(invocation_context, active_call)
        if isinstance(result, ToolSuspension):
            return call, result
        if result.block.is_error:
            error_decision = await self._dispatch_middleware(
                request,
                MiddlewarePoint.ON_ERROR,
                ErrorMiddlewarePayload(
                    operation="tool",
                    error=RunError(
                        code="tool_error",
                        message="Tool 执行返回错误",
                    ),
                    tool_call=active_call,
                ),
            )
            result = _apply_tool_error_decision(
                active_call,
                result,
                error_decision,
            )
        after = await self._dispatch_middleware(
            request,
            MiddlewarePoint.AFTER_TOOL_CALL,
            ToolResultMiddlewarePayload(call=active_call, result=result.block),
        )
        if after.type is MiddlewareDecisionType.MODIFY and isinstance(
            after.payload, ToolResultMiddlewarePayload
        ):
            result = replace(result, block=after.payload.result)
        return call, result

    async def _collect_steer(
        self,
        request: RunRequest,
        emit: EmitEvent,
    ) -> None:
        """仅在工具循环必须继续时领取 STEER，保存后留待下一轮提升。"""
        if self._inbox is None:
            return
        inbound = await self._inbox.claim(
            request.ctx,
            frozenset({InboundMode.STEER}),
            owner_token=request.inbox_owner_token or request.run_id,
        )
        messages = tuple(
            _steer_message(request, item, created_at=self._clock()) for item in inbound
        )
        self._state = _replace_state(
            self._state,
            steer_message_ids=tuple(item.message_id for item in messages),
        )
        if messages:
            for message in messages:
                await emit(MessageStarted(message=message, mode=InboundMode.STEER))
            await emit(
                MessagesCompleted(
                    messages=messages,
                    inbound_modes={
                        message.message_id: InboundMode.STEER for message in messages
                    },
                )
            )

    async def _dispatch_middleware(
        self,
        request: RunRequest,
        point: MiddlewarePoint,
        payload: MiddlewarePayload,
        *,
        attempt: int = 1,
    ) -> MiddlewareDecision:
        """构造不可变快照；未装配中间件时等价于 CONTINUE。"""
        if self._middleware is None:
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)
        return await self._middleware.dispatch(
            MiddlewareContext(
                invocation_id="runtime-dispatch",
                point=point,
                ctx=request.ctx,
                run_id=request.run_id,
                trace_id=request.trace_id,
                turn_index=self._state.turn_index or None,
                attempt=attempt,
                state_snapshot=self._state.model_copy(deep=True),
                payload=payload,
            )
        )

    async def _notify_error(
        self,
        request: RunRequest,
        payload: ErrorMiddlewarePayload,
    ) -> None:
        """错误通知自身永不得阻断原错误的终态收敛。"""
        try:
            await self._dispatch_middleware(
                request,
                MiddlewarePoint.ON_ERROR,
                payload,
            )
        except Exception as error:
            log_unexpected_exception(
                LOGGER,
                "error_middleware_notification_failed",
                error,
                operation=f"middleware.on_error.{payload.operation}",
            )
            return


def _apply_tool_error_decision(
    call: ToolCallBlock,
    result: ToolExecutionResult,
    decision: MiddlewareDecision,
) -> ToolExecutionResult:
    """校验并应用 ON_ERROR 返回的配对 ToolResult。"""
    if decision.type is not MiddlewareDecisionType.HANDLED or not isinstance(
        decision.payload,
        ToolResultMiddlewarePayload,
    ):
        return result
    replacement = decision.payload
    identities = (call.tool_call_id, call.tool_name)
    if identities != (
        replacement.call.tool_call_id,
        replacement.call.tool_name,
    ) or identities != (
        replacement.result.tool_call_id,
        replacement.result.tool_name,
    ):
        raise RuntimeError("ON_ERROR 返回的 ToolCall/ToolResult 身份不一致")
    return replace(result, block=replacement.result)


def _validate_provider_event_order(
    event: LLMEvent,
    *,
    started: bool,
    ended: bool,
) -> bool:
    """校验 Provider 流的 start/end 顺序并返回新的 started 状态。"""
    if isinstance(event, LLMStart):
        if started or ended:
            raise RuntimeError("LLMProvider 重复或延迟产生 LLMStart")
        return True
    if not started:
        raise RuntimeError("LLMProvider 事件流缺少起始 LLMStart")
    if ended:
        raise RuntimeError("LLMProvider 在 LLMEnd 后继续产生事件")
    return started


async def _execute_tool_group(
    group: tuple[ToolCallBlock, ...],
    execute: Callable[
        [ToolCallBlock],
        Awaitable[tuple[ToolCallBlock, ToolExecutionResult | ToolSuspension]],
    ],
) -> tuple[
    list[tuple[ToolCallBlock, ToolExecutionResult]],
    ToolSuspension | None,
]:
    """并发执行安全组，并按完成顺序归类结果。

    普通挂起等待同组任务自然结束；任务异常由 TaskGroup 取消其余任务并传播。
    分组调用方须保证一个组最多包含一个可挂起工具。
    """
    results: list[tuple[ToolCallBlock, ToolExecutionResult]] = []
    suspension: ToolSuspension | None = None

    async def execute_one(
        call: ToolCallBlock,
    ) -> tuple[ToolCallBlock, ToolExecutionResult | ToolSuspension]:
        """把执行回调适配为 TaskGroup 可直接调度的协程。"""
        return await execute(call)

    async with asyncio.TaskGroup() as task_group:
        tasks: tuple[
            asyncio.Task[tuple[ToolCallBlock, ToolExecutionResult | ToolSuspension]],
            ...,
        ] = tuple(task_group.create_task(execute_one(call)) for call in group)
        for completed in asyncio.as_completed(tasks):
            call, result = await completed
            if isinstance(result, ToolSuspension):
                suspension = result
            else:
                results.append((call, result))
    return results, suspension


def _tool_message(
    request: RunRequest,
    result: ToolResultBlock,
    *,
    message_id: str,
    created_at: datetime,
) -> Message:
    return Message(
        message_id=message_id,
        session_id=request.ctx.session_id,
        role=Role.TOOL,
        content=(result,),
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id=request.run_id,
        created_at=created_at,
    )


def _steer_message(
    request: RunRequest,
    inbound: InboundMessage,
    *,
    created_at: datetime,
) -> Message:
    if (
        inbound.session_id != request.ctx.session_id
        or inbound.mode is not InboundMode.STEER
    ):
        raise ValueError("STEER 输入与当前 session/mode 不一致")
    return Message(
        message_id=inbound.message_id,
        session_id=inbound.session_id,
        role=Role(inbound.role.value),
        content=inbound.content,
        origin=inbound.origin,
        visibility=MessageVisibility.ALL,
        run_id=request.run_id,
        created_at=inbound.created_at or created_at,
    )


def _validate_request(request: RunRequest, state: AgentState) -> None:
    if request.ctx != state.session_context:
        raise ValueError("RunRequest 与 AgentState 的 SessionContext 不一致")


def _validate_initial_messages(
    request: RunRequest,
    messages: Sequence[Message],
) -> None:
    ids: set[str] = set()
    for message in messages:
        if (
            message.session_id != request.ctx.session_id
            or message.run_id != request.run_id
            or not is_inbound_message(message)
        ):
            raise ValueError("初始消息必须是当前 session/run 的 USER/SYSTEM 输入")
        if message.message_id in ids:
            raise ValueError("初始消息 ID 不能重复")
        ids.add(message.message_id)


def _add_usage(current: Usage, delta: Usage) -> Usage:
    if (
        current.total_tokens == 0
        and current.input_tokens == 0
        and current.output_tokens == 0
    ):
        return delta.model_copy(deep=True)
    accounting = (
        UsageAccounting.EXACT
        if current.accounting is UsageAccounting.EXACT
        and delta.accounting is UsageAccounting.EXACT
        else UsageAccounting.PARTIAL
    )
    return Usage(
        input_tokens=current.input_tokens + delta.input_tokens,
        output_tokens=current.output_tokens + delta.output_tokens,
        total_tokens=current.total_tokens + delta.total_tokens,
        accounting=accounting,
        reasoning_tokens=_add_optional(
            current.reasoning_tokens,
            delta.reasoning_tokens,
        ),
        cache_read_tokens=_add_optional(
            current.cache_read_tokens,
            delta.cache_read_tokens,
        ),
        cache_write_tokens=_add_optional(
            current.cache_write_tokens,
            delta.cache_write_tokens,
        ),
    )


def _add_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _latest_compaction_id(source: ContextMessage) -> str | None:
    message = source.latest_compaction_message
    return message.message_id if message is not None else None


def _context_compaction_completed_event(
    result: CompactionResult,
    expected_latest_compaction_id: str | None,
) -> ContextCompactionCompleted:
    block = result.compaction_message.content[0]
    if not isinstance(block, CompactionBlock):
        raise RuntimeError("CompactionResult 缺少 CompactionBlock")
    return ContextCompactionCompleted(
        action=result.action,
        compaction_message=result.compaction_message,
        expected_latest_compaction_id=expected_latest_compaction_id,
        summary_provider_name=result.summary_provider_name,
        summary_calls=result.summary_calls,
        selected_summary_index=result.selected_summary_index,
        compacted_message_ids=block.compacted_message_ids,
        pruned_message_ids=block.pruned_message_ids,
        before_tokens=result.before_tokens,
        after_tokens=result.after_tokens,
        usage=result.usage,
    )


def _derive_unresolved_tool_calls(
    messages: Sequence[Message],
) -> tuple[ToolCallBlock, ...]:
    """从最新 Assistant 工具批次与其后 ToolResult 差集恢复未闭合调用。"""
    assistant_index = -1
    calls: tuple[ToolCallBlock, ...] = ()
    for index, message in enumerate(messages):
        candidate = tuple(
            block for block in message.content if isinstance(block, ToolCallBlock)
        )
        if candidate:
            assistant_index = index
            calls = candidate
    if assistant_index < 0:
        return ()
    completed: set[str] = set()
    for message in messages[assistant_index + 1 :]:
        if message.role is not Role.TOOL:
            break
        block = message.content[0]
        if isinstance(block, ToolResultBlock):
            completed.add(block.tool_call_id)
    return tuple(call for call in calls if call.tool_call_id not in completed)


def _tool_execution_groups(
    calls: tuple[ToolCallBlock, ...],
    catalog: ToolRegistry,
    *,
    parallel: bool,
) -> tuple[tuple[ToolCallBlock, ...], ...]:
    """按 safe/unsafe 与单挂起工具约束形成连续执行组。"""
    if not parallel:
        return tuple((call,) for call in calls)
    groups: list[tuple[ToolCallBlock, ...]] = []
    current: list[ToolCallBlock] = []
    current_can_suspend = False
    for call in calls:
        definition = catalog.get(call.tool_name)
        if definition is None or not definition.is_concurrency_safe:
            if current:
                groups.append(tuple(current))
                current = []
                current_can_suspend = False
            groups.append((call,))
            continue
        if definition.can_suspend and current_can_suspend:
            groups.append(tuple(current))
            current = []
            current_can_suspend = False
        current.append(call)
        current_can_suspend = current_can_suspend or definition.can_suspend
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _middleware_tool_result(
    block: ToolResultBlock,
    now: datetime,
) -> ToolExecutionResult:
    """为未调用真实 Handler 的策略结果补充零耗时元数据。"""
    return ToolExecutionResult(
        block=block,
        timing=Timing(
            started_at=now,
            ended_at=now,
            duration_ms=0,
        ),
    )


def _current_user_targets(
    source: ContextMessage,
    state: AgentState,
) -> tuple[Message, ...]:
    """按 retained 顺序返回 current IDs 中的全部 USER。"""
    current = set(state.current_message_ids)
    return tuple(
        message
        for message in source.retained_messages
        if message.message_id in current and message.role is Role.USER
    )


def _has_injection(source: ContextMessage, target_message_id: str) -> bool:
    """判断确定性目标注入是否已经存在于一致性快照。"""
    return any(
        len(message.content) == 1
        and isinstance(message.content[0], InjectedBlock)
        and message.content[0].target_user_message_id == target_message_id
        for message in source.injection_messages
    )
