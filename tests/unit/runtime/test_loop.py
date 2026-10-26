"""AgentLoop 普通完整响应路径与保存屏障测试。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Never, cast

import pytest
from agentsystem.common.errors import EventCommitError, LLMError, RunError
from agentsystem.common.models import (
    FinishReason,
    JsonValue,
    OutputMode,
    SessionContext,
)
from agentsystem.context.builder import ContextBuilder
from agentsystem.context.compactor import (
    CompactionResult,
    Compactor,
    context_token_request_fingerprint,
)
from agentsystem.context.providers import ContextProvider, ContextProviderRunner
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    InboundRole,
    RunRequest,
)
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
from agentsystem.message.models import (
    BlockType,
    CompactionBlock,
    ContextMessage,
    InjectedBlock,
    InjectedItem,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    ErrorMiddlewarePayload,
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
    ToolResultMiddlewarePayload,
    TurnMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.runtime import events as runtime_events
from agentsystem.runtime.events import (
    AgentEventPayload,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    LLMCallCompleted,
    LLMCallStarted,
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
from agentsystem.runtime.loop import (
    AgentLoop,
    NewRunInput,
    ResumeInput,
    _execute_tool_group,
)
from agentsystem.runtime.state import (
    AgentState,
    SessionStatus,
    run_timing,
    start_run,
    suspend_run,
)
from agentsystem.tool.authorization import ToolAuthorizationDecision
from agentsystem.tool.context import ToolInvocationContext, get_tool_context
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource
from agentsystem.tool.registry import ToolRegistry
from tests.unit.runtime.port_stubs import InboxStub, MessageManagerStub

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_EARLIER = _NOW - timedelta(minutes=1)
_CONTEXT = SessionContext(
    app_id="app", user_id="user", agent_id="agent", session_id="session"
)


async def test_canceling_tool_group_waits_for_every_child_cleanup() -> None:
    """父 Run Task 取消后不能遗留仍在执行的并行 Tool Task。"""
    calls = (
        ToolCallBlock(
            tool_call_id="call-1",
            tool_name="first",
            arguments={},
            raw_arguments="{}",
        ),
        ToolCallBlock(
            tool_call_id="call-2",
            tool_name="second",
            arguments={},
            raw_arguments="{}",
        ),
    )
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0
    cleaned: list[str] = []

    async def execute(call: ToolCallBlock) -> Never:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        try:
            await release.wait()
            raise AssertionError("取消后不应继续执行")
        finally:
            cleaned.append(call.tool_call_id)

    task = asyncio.create_task(_execute_tool_group(calls, execute))
    await both_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(cleaned) == ["call-1", "call-2"]


class _MemoryMessages(MessageManagerStub):
    def __init__(
        self,
        system: Message,
        *,
        expose_committed_compaction: bool = True,
    ) -> None:
        self._system = system
        self._expose_committed_compaction = expose_committed_compaction
        self.committed: list[Message] = []
        self.latest_compaction: Message | None = None
        self.read_count = 0
        self.operations: list[str] = []

    async def commit(
        self,
        ctx: SessionContext,
        messages: tuple[Message, ...],
    ) -> None:
        assert ctx == _CONTEXT
        self.committed.extend(messages)

    async def read_context_source(self, ctx: SessionContext) -> ContextMessage:
        assert ctx == _CONTEXT
        self.read_count += 1
        self.operations.append("read")
        visible_compaction = (
            self.latest_compaction if self._expose_committed_compaction else None
        )
        covered: set[str] = set()
        if visible_compaction is not None:
            block = visible_compaction.content[0]
            assert isinstance(block, CompactionBlock)
            covered.update(block.compacted_message_ids)
        return ContextMessage(
            system_message=self._system,
            latest_compaction_message=visible_compaction,
            retained_messages=tuple(
                message
                for message in self.committed
                if not isinstance(message.content[0], (CompactionBlock, InjectedBlock))
                and message.message_id not in covered
            ),
            injection_messages=tuple(
                message
                for message in self.committed
                if len(message.content) == 1
                and isinstance(message.content[0], InjectedBlock)
            ),
        )

    async def commit_compaction(
        self,
        ctx: SessionContext,
        message: Message,
        *,
        expected_latest_compaction_id: str | None,
    ) -> None:
        assert ctx == _CONTEXT
        self.operations.append("commit_compaction")
        current = (
            self.latest_compaction.message_id
            if self.latest_compaction is not None
            else None
        )
        assert current == expected_latest_compaction_id
        self.latest_compaction = message


class _Compactor:
    """实现 Loop 所需精确入口的可配置 Compactor fake。"""

    def __init__(
        self,
        result: CompactionResult | None = None,
        *,
        should_compact: bool | None = None,
        should_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.calls = 0
        self.should_error = should_error
        self.should_compact_result = (
            result is not None if should_compact is None else should_compact
        )
        self.should_calls: list[
            tuple[
                ContextMessage,
                AgentState,
                tuple[Message, ...],
                tuple[Mapping[str, JsonValue], ...],
            ]
        ] = []

    def should_compact(
        self,
        source: ContextMessage,
        state: AgentState,
        request_messages: tuple[Message, ...],
        tools: tuple[Mapping[str, JsonValue], ...],
    ) -> bool:
        """记录 Loop 传入的完整上下文并返回配置判断。"""
        self.should_calls.append((source, state, request_messages, tools))
        if self.should_error is not None:
            raise self.should_error
        return self.should_compact_result

    async def compact(
        self,
        *,
        ctx: SessionContext,
        run_id: str,
        trace_id: str,
        source: ContextMessage,
        state: AgentState,
        request_messages: tuple[Message, ...],
        tools: tuple[Mapping[str, JsonValue], ...],
    ) -> CompactionResult | None:
        del (
            ctx,
            run_id,
            trace_id,
            source,
            state,
            request_messages,
            tools,
        )
        self.calls += 1
        return self.result


def _as_compactor(fake: _Compactor) -> Compactor:
    """在测试边界把结构化 fake 交给具体类型构造参数。"""
    return cast(Compactor, fake)


def _as_client(fake: object) -> LLMClient:
    """在测试边界把结构化 Client fake 交给具体类型构造参数。"""
    return cast(LLMClient, fake)


class _RecordingProvider:
    provider_name = "contract-provider"

    def __init__(self, messages: _MemoryMessages, response: LLMResponse) -> None:
        self._messages = messages
        self._response = response
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        # 如果 Loop 没等待 MessagesCompleted 保存屏障，这里看不到 USER。
        assert any(message.role is Role.USER for message in self._messages.committed)
        self.requests.append(request)
        yield LLMStart(
            request_id=request.request_id,
            created_at=_NOW,
            model=request.config.model,
            output_mode=request.output_mode,
        )
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=self._response,
        )


class _SequenceProvider:
    provider_name = "contract-provider"

    def __init__(self, responses: tuple[LLMResponse, ...]) -> None:
        self._responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        yield LLMStart(
            request_id=request.request_id,
            created_at=_NOW,
            model=request.config.model,
            output_mode=request.output_mode,
        )
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=next(self._responses),
        )


class _StreamingProvider(_RecordingProvider):
    """产生完整 text block 边界的 Provider 测试实现。"""

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        assert any(message.role is Role.USER for message in self._messages.committed)
        self.requests.append(request)
        block = self._response.blocks[0]
        assert isinstance(block, TextBlock)
        yield LLMStart(
            request_id=request.request_id,
            created_at=_NOW,
            model=request.config.model,
            output_mode=request.output_mode,
        )
        yield LLMBlockStart(
            request_id=request.request_id,
            created_at=_NOW,
            index=0,
            block_id="provider-block-1",
            block_type=block.type,
        )
        yield LLMBlockDelta(
            request_id=request.request_id,
            created_at=_NOW,
            index=0,
            block_id="provider-block-1",
            block_type=block.type,
            delta=block.text,
        )
        yield LLMBlockEnd(
            request_id=request.request_id,
            created_at=_NOW,
            index=0,
            block_id="provider-block-1",
            block=block,
        )
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=self._response,
        )


class _MissingStartProvider(_RecordingProvider):
    """模拟违反统一协议、直接产生终态的 Provider。"""

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        yield LLMEnd(
            request_id=request.request_id,
            created_at=_NOW,
            response=self._response,
        )


class _Queue(InboxStub):
    def __init__(self, steer: tuple[InboundMessage, ...]) -> None:
        self._steer = steer
        self.calls: list[frozenset[InboundMode]] = []

    async def claim(
        self,
        ctx: SessionContext,
        modes: frozenset[InboundMode],
        *,
        owner_token: str,
    ) -> tuple[InboundMessage, ...]:
        assert ctx == _CONTEXT
        assert owner_token
        self.calls.append(modes)
        result, self._steer = self._steer, ()
        return result


async def _run_loop(
    loop: AgentLoop,
    request: RunRequest,
    loop_input: NewRunInput | ResumeInput,
    messages: _MemoryMessages,
) -> tuple[list[AgentEventPayload], list[AgentState], AgentState]:
    """通过真实异步保存屏障收集 Loop 事件和对应 post-state。"""
    payloads: list[AgentEventPayload] = []
    states: list[AgentState] = []

    async def commit(payload: AgentEventPayload) -> None:
        payloads.append(payload)
        states.append(loop.state.model_copy(deep=True))
        if isinstance(payload, MessagesCompleted):
            await messages.commit(_CONTEXT, payload.messages)
        elif (
            isinstance(payload, ContextCompactionCompleted)
            and payload.compaction_message is not None
        ):
            await messages.commit_compaction(
                _CONTEXT,
                payload.compaction_message,
                expected_latest_compaction_id=payload.expected_latest_compaction_id,
            )

    result = await loop.run(request, loop_input, commit)
    return payloads, states, result


def _pruned_prepare_fixture(
    *,
    expose_committed_compaction: bool = True,
) -> tuple[
    AgentLoop,
    RunRequest,
    _Compactor,
    _MemoryMessages,
    Callable[[AgentEventPayload], Awaitable[None]],
]:
    """构造只执行 `_prepare_context` 的纯剪枝保存场景。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    current = _message(
        "current-user", Role.USER, MessageOrigin.USER, "current", "run-1"
    )
    messages = _MemoryMessages(
        system,
        expose_committed_compaction=expose_committed_compaction,
    )
    messages.committed.append(current)
    snapshot = Message(
        message_id="pruned-compaction",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="pruned",
                pruned_message_ids=("historical-tool",),
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        run_id="run-1",
        created_at=_NOW,
    )
    result = CompactionResult(
        action="pruned",
        request_messages=(system, current),
        before_tokens=90,
        after_tokens=30,
        compaction_message=snapshot,
    )
    fake = _Compactor(result)
    registry = ToolRegistry()
    registry.freeze()
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    ).model_copy(update={"current_message_ids": ("current-user",)})
    loop = AgentLoop(
        initial_state=running,
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(fake),
        llm_client=_as_client(_SequenceProvider(())),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    async def emit(payload: AgentEventPayload) -> None:
        if (
            isinstance(payload, ContextCompactionCompleted)
            and payload.compaction_message is not None
        ):
            await messages.commit_compaction(
                _CONTEXT,
                payload.compaction_message,
                expected_latest_compaction_id=payload.expected_latest_compaction_id,
            )

    return loop, request, fake, messages, emit


async def test_on_error_notification_failure_logs_its_own_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ON_ERROR 派发失败不能覆盖原错误，但必须独立留下堆栈。"""

    async def invalid(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.BLOCK)

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="invalid-on-error",
            middleware_id="invalid-on-error",
            point=MiddlewarePoint.ON_ERROR,
            scope=MiddlewareScope.GLOBAL,
            handler=invalid,
        )
    )
    middleware_registry.freeze()
    loop, request, _, _, _ = _pruned_prepare_fixture()
    loop._middleware = MiddlewareManager(middleware_registry)

    with caplog.at_level("ERROR"):
        await loop._notify_error(
            request,
            ErrorMiddlewarePayload(
                operation="compaction",
                error=RunError(
                    code="context_compaction_failed",
                    message="上下文压缩失败",
                ),
            ),
        )

    failure = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("error_middleware_notification_failed ")
    )
    assert failure.exc_info is not None
    assert "error_id=" in failure.getMessage()


async def test_complete_text_run_commits_barriered_event_sequence() -> None:
    """普通回复必须先保存 USER/Turn，再调用 LLM，最后保存 Assistant 和终态。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="world"),),
        usage=Usage(
            input_tokens=2,
            output_tokens=1,
            total_tokens=3,
            accounting=UsageAccounting.EXACT,
        ),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.STOP,
        response_id="response-1",
    )
    provider = _RecordingProvider(messages, response)
    registry = ToolRegistry()
    registry.freeze()
    generation_config = LLMGenerationConfig(
        model="request-model",
    )
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        generation_config=generation_config,
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "assistant-1")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, states, result = await _run_loop(
        loop,
        request,
        NewRunInput(
            messages=(user,),
            inbound_modes={user.message_id: InboundMode.QUEUE},
        ),
        messages,
    )

    assert [type(payload) for payload in payloads] == [
        RunStarted,
        MessageStarted,
        MessagesCompleted,
        TurnStarted,
        LLMCallStarted,
        LLMCallCompleted,
        MessagesCompleted,
        TurnCompleted,
        RunCompleted,
    ]
    assert isinstance(payloads[0], RunStarted)
    assert payloads[0].initial_messages == (user,)
    assert states[0].current_message_ids == ("user-1",)
    assert states[1].current_message_ids == ("user-1",)
    assert states[2].current_message_ids == ("user-1",)
    assert states[3].turn_index == 1
    assert states[6].current_message_ids == ()
    assert states[6].context_tokens == 3
    assert states[6].context_token_checkpoint_message_id == "assistant-1"
    assert states[6].context_token_request_fingerprint
    assert states[-1].status is SessionStatus.SUCCEEDED
    assert states[-1].final_message_id == "assistant-1"
    assert states[-1].usage_total == response.usage
    assert result == loop.state
    assert messages.read_count == 1
    assert provider.requests[0].messages == (system, user)
    assert provider.requests[0].request_id == "request-1"
    assert provider.requests[0].config.model == "request-model"
    completed = payloads[-1]
    assert isinstance(completed, RunCompleted)
    assert completed.final_message.message_id == "assistant-1"
    assert completed.final_message.content == (TextBlock(text="world"),)
    assert completed.usage == response.usage
    assert completed.timing == run_timing(states[-1])


async def test_main_model_without_usage_clears_older_context_checkpoint() -> None:
    """未知调用成本不能继续复用更早主模型的 token 锚点。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "hello", "run-1")
    response = LLMResponse(
        blocks=(TextBlock(text="world"),),
        usage=Usage(),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    registry = ToolRegistry()
    registry.freeze()
    running = start_run(
        AgentState(
            session_context=_CONTEXT,
            context_tokens=50_000,
            context_token_checkpoint_message_id="assistant-old",
            context_token_request_fingerprint="fingerprint-old",
        ),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    loop = AgentLoop(
        initial_state=running,
        message_manager=_MemoryMessages(system),
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(_SequenceProvider((response,))),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "assistant-1")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    async def emit(_: AgentEventPayload) -> None:
        return None

    await loop._call_llm(request, (system, user), (), emit)

    assert loop.state.context_tokens is None
    assert loop.state.context_token_checkpoint_message_id is None
    assert loop.state.context_token_request_fingerprint is None


def test_inbound_events_accept_dynamic_system_and_reject_assistant() -> None:
    """Inbox 事件接受带 run_id 的 SYSTEM，但不放宽到模型输出。"""
    system = _message(
        "system-1",
        Role.SYSTEM,
        "workflow",
        "notice",
        "run-1",
    )

    started = MessageStarted(message=system, mode=InboundMode.QUEUE)
    completed = MessagesCompleted(
        messages=(system,),
        inbound_modes={system.message_id: InboundMode.QUEUE},
    )

    assert started.message.role is Role.SYSTEM
    assert completed.inbound_modes == {"system-1": InboundMode.QUEUE}

    assistant = _message(
        "assistant-1",
        Role.ASSISTANT,
        MessageOrigin.MODEL,
        "answer",
        "run-1",
    )
    with pytest.raises(ValueError):
        MessageStarted(message=assistant, mode=InboundMode.QUEUE)


async def test_event_commit_failure_is_not_converted_to_run_failed() -> None:
    """保存屏障失败属于 Session 故障，Loop 不得伪造业务 RunFailed。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    registry = ToolRegistry()
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(_SequenceProvider(())),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
    )
    attempted: list[AgentEventPayload] = []

    async def fail_commit(payload: AgentEventPayload) -> None:
        attempted.append(payload)
        raise EventCommitError(RuntimeError("state CAS failed"))

    with pytest.raises(EventCommitError) as captured:
        await loop.run(
            RunRequest(
                ctx=_CONTEXT,
                run_id="run-1",
                trace_id="trace-1",
                output_mode=OutputMode.COMPLETE,
            ),
            NewRunInput(messages=(user,)),
            fail_commit,
        )

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "state CAS failed"
    assert [type(payload) for payload in attempted] == [RunStarted]
    assert loop.state.status is SessionStatus.RUNNING


async def test_length_finish_preserves_partial_message_and_completes_run() -> None:
    """模型输出截断保留已生成文本和原因，并正常完成 run。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="partial answer"),),
        usage=Usage(total_tokens=3),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.LENGTH,
    )
    provider = _RecordingProvider(messages, response)
    registry = ToolRegistry()
    registry.freeze()
    generation_config = LLMGenerationConfig(
        model="request-model",
    )
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        generation_config=generation_config,
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "assistant-1")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    completed = payloads[-1]
    assert isinstance(completed, RunCompleted)
    assert loop.state.status is SessionStatus.SUCCEEDED
    assert loop.state.final_message_id == "assistant-1"
    assistant = completed.final_message
    assert assistant.content == (TextBlock(text="partial answer"),)
    assert assistant.stop_reason is FinishReason.LENGTH


async def test_complete_run_maps_internal_blocks_before_finalized_message() -> None:
    """COMPLETE 也必须转发内部 block，且不得提前保存 partial Assistant。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="流式完成"),),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    provider = _StreamingProvider(messages, response)
    registry = ToolRegistry()
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "assistant-1")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert [type(payload) for payload in payloads] == [
        RunStarted,
        MessagesCompleted,
        TurnStarted,
        LLMCallStarted,
        runtime_events.MessageBlockStarted,
        runtime_events.MessageBlockDelta,
        runtime_events.MessageBlockCompleted,
        LLMCallCompleted,
        MessagesCompleted,
        TurnCompleted,
        RunCompleted,
    ]
    started, delta, completed = payloads[4:7]
    assert isinstance(started, runtime_events.MessageBlockStarted)
    assert isinstance(delta, runtime_events.MessageBlockDelta)
    assert isinstance(completed, runtime_events.MessageBlockCompleted)
    assert started.message_id == delta.message_id == completed.message_id
    assert started.message_id == "assistant-1"
    assert delta.delta == "流式完成"
    assert delta.block_type is BlockType.TEXT
    finalized = payloads[8]
    assert isinstance(finalized, MessagesCompleted)
    assert finalized.messages[0].content == (TextBlock(text="流式完成"),)


async def test_provider_stream_without_start_fails_run() -> None:
    """缺少 LLMStart 的 Provider 流不得被误判为一次成功调用。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="不应成功"),),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    provider = _MissingStartProvider(messages, response)
    registry = ToolRegistry()
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "assistant-1")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert isinstance(payloads[-1], RunFailed)
    assert loop.state.status is SessionStatus.FAILED


async def test_compaction_is_saved_and_reread_before_main_llm() -> None:
    """摘要若未经过压缩完成事件保存后重读，主模型将看到旧历史。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    old_user = _message("old-user", Role.USER, MessageOrigin.USER, "old", "old-run")
    current = _message("current-user", Role.USER, MessageOrigin.USER, "new", "run-1")
    messages = _MemoryMessages(system)
    messages.committed.append(old_user)
    compaction_message = Message(
        message_id="compaction-1",
        session_id="session",
        role=Role.USER,
        content=(
            CompactionBlock(
                action="summarized",
                summary="<summary>历史摘要</summary>",
                compacted_message_ids=("old-user",),
                next_retained_message_id="current-user",
            ),
        ),
        origin=MessageOrigin.COMPACTOR,
        visibility=MessageVisibility.ALL,
        model="summary-model",
        run_id="run-1",
        created_at=_NOW,
    )
    compactor = _Compactor(
        CompactionResult(
            action="summarized",
            request_messages=(system, current),
            compaction_message=compaction_message,
            before_tokens=90,
            after_tokens=30,
            usage=Usage(total_tokens=5),
            summary_provider_name="summary-provider",
            summary_calls=(
                (
                    LLMRequest(
                        request_id="summary-request",
                        messages=(system, old_user),
                        config=LLMGenerationConfig(model="summary-model"),
                        output_mode=OutputMode.COMPLETE,
                    ),
                    LLMResponse(
                        blocks=(TextBlock(text="<summary>历史摘要</summary>"),),
                        usage=Usage(total_tokens=5),
                        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
                        model="summary-model",
                        finish_reason=FinishReason.STOP,
                        response_id="summary-response",
                    ),
                ),
            ),
            selected_summary_index=0,
        ),
    )
    response = LLMResponse(
        blocks=(TextBlock(text="answer"),),
        usage=Usage(total_tokens=3),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=1),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    provider = _RecordingProvider(messages, response)
    registry = ToolRegistry()
    registry.freeze()
    compaction_generation_config = LLMGenerationConfig(
        model="request-model",
        provider_options={"prompt_cache_retention": "24h"},
    )
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(compactor),
        llm_client=_as_client(provider),
        tool_registry=registry,
        generation_config=compaction_generation_config,
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "assistant-1")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, states, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(current,)),
        messages,
    )

    started_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, ContextCompactionStarted)
    )
    compacted_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, ContextCompactionCompleted)
    )
    llm_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, LLMCallStarted)
    )
    assert started_index < compacted_index < llm_index
    assert messages.read_count == 2
    assert messages.operations == ["read", "commit_compaction", "read"]
    assert compactor.calls == 1
    compacted_event = payloads[compacted_index]
    assert isinstance(compacted_event, ContextCompactionCompleted)
    assert compacted_event.action == "summarized"
    assert len(compacted_event.summary_calls) == 1
    assert compacted_event.selected_summary_index == 0
    assert states[compacted_index].context_tokens == 30
    assert states[compacted_index].context_token_checkpoint_message_id == "current-user"
    assert states[
        compacted_index
    ].context_token_request_fingerprint == context_token_request_fingerprint(
        provider_name=provider.provider_name,
        model="request-model",
        messages=provider.requests[0].messages,
        tools=provider.requests[0].tools,
    )
    assert provider.requests[0].messages[1].content == (
        TextBlock(
            text=(
                "以下 <summary> 是前期对话的总结，请基于该总结继续处理后续消息。\n"
                "<summary>历史摘要</summary>"
            )
        ),
    )
    assert loop.state.usage_total.total_tokens == 8
    assert loop.state.context_tokens == 3
    assert loop.state.context_token_checkpoint_message_id == "assistant-1"


async def test_compaction_fails_when_reread_does_not_expose_candidate() -> None:
    """保存屏障返回后仍未读到候选快照时必须 fail-close。"""
    loop, request, _, messages, emit = _pruned_prepare_fixture(
        expose_committed_compaction=False
    )

    with pytest.raises(RuntimeError, match="保存后重读"):
        await loop._prepare_context(request, emit)

    assert messages.operations == ["read", "commit_compaction", "read"]


async def test_loop_skips_compaction_lifecycle_when_compactor_declines() -> None:
    """Compactor 判断无需压缩时不得调用 compact 或产生生命周期事件。"""
    loop, request, fake, _, persist = _pruned_prepare_fixture()
    fake.should_compact_result = False
    below_events: list[AgentEventPayload] = []

    async def capture_below(payload: AgentEventPayload) -> None:
        below_events.append(payload)
        await persist(payload)

    await loop._prepare_context(request, capture_below)

    assert fake.calls == 0
    assert below_events == []


async def test_compaction_scan_error_uses_compaction_error_mapping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """should_compact 扫描失败仍须以 compaction 错误通知和终态收敛。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    orphan = Message(
        message_id="orphan-result",
        session_id="session",
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id="missing-call",
                tool_name="lookup",
                content="stale",
            ),
        ),
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id="old-run",
        created_at=_NOW,
    )
    user = _message(
        "current-user",
        Role.USER,
        MessageOrigin.USER,
        "x" * 1_000,
        "run-1",
    )
    messages = _MemoryMessages(system)
    messages.committed.append(orphan)
    provider = _SequenceProvider(())
    observed_errors: list[ErrorMiddlewarePayload] = []

    async def observe_error(context: MiddlewareContext) -> MiddlewareDecision:
        assert isinstance(context.payload, ErrorMiddlewarePayload)
        observed_errors.append(context.payload)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="observe-compaction-error",
            middleware_id="observe-compaction-error",
            point=MiddlewarePoint.ON_ERROR,
            scope=MiddlewareScope.GLOBAL,
            handler=observe_error,
        )
    )
    middleware_registry.freeze()
    registry = ToolRegistry()
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(
            _Compactor(should_error=ValueError("invalid compaction scan"))
        ),
        llm_client=_as_client(provider),
        tool_registry=registry,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    with caplog.at_level("ERROR"):
        payloads, _, _ = await _run_loop(
            loop,
            request,
            NewRunInput(messages=(user,)),
            messages,
        )

    assert [(item.operation, item.error.code) for item in observed_errors] == [
        ("compaction", "context_compaction_failed")
    ]
    assert not any(isinstance(item, ContextCompactionStarted) for item in payloads)
    assert isinstance(payloads[-1], RunFailed)
    assert loop.state.error is not None
    assert loop.state.error.code == "context_compaction_failed"
    assert loop.state.error.error_id is not None
    assert loop.state.error.exception_type == "ValueError"
    assert loop.state.error.stacktrace is not None
    assert "invalid compaction scan" in loop.state.error.stacktrace
    assert (
        sum(
            record.getMessage().startswith("agent_loop_failed")
            for record in caplog.records
        )
        == 1
    )
    assert f"error_id={loop.state.error.error_id}" in caplog.text
    assert provider.requests == []


async def test_compaction_started_commit_error_propagates_without_error_mapping() -> (
    None
):
    """Started 保存屏障失败不得伪造成 compaction 错误或 RunFailed。"""
    loop, request, _, _, _ = _pruned_prepare_fixture()
    emitted: list[AgentEventPayload] = []
    observed_errors: list[ErrorMiddlewarePayload] = []

    async def observe_error(context: MiddlewareContext) -> MiddlewareDecision:
        assert isinstance(context.payload, ErrorMiddlewarePayload)
        observed_errors.append(context.payload)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="observe-started-commit-error",
            middleware_id="observe-started-commit-error",
            point=MiddlewarePoint.ON_ERROR,
            scope=MiddlewareScope.GLOBAL,
            handler=observe_error,
        )
    )
    middleware_registry.freeze()
    loop._middleware = MiddlewareManager(middleware_registry)

    async def fail_started(payload: AgentEventPayload) -> None:
        emitted.append(payload)
        if isinstance(payload, ContextCompactionStarted):
            raise EventCommitError(RuntimeError("compaction started commit failed"))

    with pytest.raises(EventCommitError) as captured:
        await loop._prepare_context(request, fail_started)

    assert captured.value.cause is not None
    assert str(captured.value.cause) == "compaction started commit failed"
    assert [type(payload) for payload in emitted] == [ContextCompactionStarted]
    assert not any(isinstance(payload, RunFailed) for payload in emitted)
    assert observed_errors == []


async def test_loop_does_not_reject_authoritative_rebuild_by_estimate() -> None:
    """保存后权威重建不再根据本地估算拒绝主模型请求。"""
    loop, request, fake, _, emit = _pruned_prepare_fixture()

    await loop._prepare_context(request, emit)

    assert len(fake.should_calls) == 1


async def test_before_llm_middleware_can_replace_provider_response() -> None:
    """BEFORE_LLM REPLACE 跳过 Provider，但替代结果仍走完整保存链。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    provider = _SequenceProvider(())

    async def replace(context: MiddlewareContext) -> MiddlewareDecision:
        assert context.point is MiddlewarePoint.BEFORE_LLM
        assert isinstance(context.payload, LLMRequest)
        return MiddlewareDecision(
            type=MiddlewareDecisionType.REPLACE,
            payload=LLMResponse(
                blocks=(TextBlock(text="middleware response"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="middleware-model",
                finish_reason=FinishReason.STOP,
            ),
        )

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="replace-llm",
            middleware_id="replace-llm",
            point=MiddlewarePoint.BEFORE_LLM,
            scope=MiddlewareScope.GLOBAL,
            handler=replace,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert provider.requests == []
    assert messages.committed[-1].content == (TextBlock(text="middleware response"),)
    assert loop.state.status is SessionStatus.SUCCEEDED


async def test_before_turn_messages_cross_save_barrier_before_llm() -> None:
    """Turn Reminder 保存并重读后才进入 Compaction、Trace input 和 LLM。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    reminder = _message(
        "reminder",
        Role.USER,
        "test-reminder",
        "<system-reminder>检查任务进度。</system-reminder>",
        "run-1",
        visibility=MessageVisibility.MODEL,
    )
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="done"),),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    provider = _SequenceProvider((response,))
    compactor = _Compactor()

    async def contribute(context: MiddlewareContext) -> MiddlewareDecision:
        assert context.point is MiddlewarePoint.BEFORE_TURN
        assert context.turn_index == 1
        assert isinstance(context.payload, TurnMiddlewarePayload)
        assert user in context.payload.messages
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=(reminder,)),
        )

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="reminder",
            middleware_id="reminder",
            point=MiddlewarePoint.BEFORE_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=contribute,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(compactor),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    reminder_event_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, MessagesCompleted) and reminder in payload.messages
    )
    llm_event_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, LLMCallStarted)
    )
    assert reminder_event_index < llm_event_index
    assert reminder in messages.committed
    assert reminder in provider.requests[0].messages
    assert messages.read_count == 3
    assert compactor.should_calls[0][2] == provider.requests[0].messages
    started = payloads[llm_event_index]
    assert isinstance(started, LLMCallStarted)
    assert started.request is not None
    assert reminder in started.request.messages


async def test_before_turn_continue_does_not_reread_before_llm() -> None:
    """CONTINUE 不增加 LLM 前上下文读取，AFTER_TURN 只做完成后观察读取。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="done"),),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    provider = _SequenceProvider((response,))

    async def proceed(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="continue",
            middleware_id="continue",
            point=MiddlewarePoint.BEFORE_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=proceed,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant")).__next__,
    )

    await _run_loop(
        loop,
        RunRequest(
            ctx=_CONTEXT,
            run_id="run-1",
            trace_id="trace-1",
            output_mode=OutputMode.COMPLETE,
        ),
        NewRunInput(messages=(user,)),
        messages,
    )

    assert provider.requests[0].messages == (system, user)
    assert messages.read_count == 2


async def test_before_turn_block_prevents_llm_and_fails_run() -> None:
    """显式 BLOCK 在初始消息保存后阻止本轮 LLM。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    provider = _SequenceProvider(())

    async def block(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.BLOCK,
            reason_code="turn_policy_blocked",
        )

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="block",
            middleware_id="block",
            point=MiddlewarePoint.BEFORE_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=block,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
    )

    await _run_loop(
        loop,
        RunRequest(
            ctx=_CONTEXT,
            run_id="run-1",
            trace_id="trace-1",
            output_mode=OutputMode.COMPLETE,
        ),
        NewRunInput(messages=(user,)),
        messages,
    )

    assert provider.requests == []
    assert messages.committed == [user]
    assert loop.state.status is SessionStatus.FAILED


async def test_after_turn_observes_each_completed_turn() -> None:
    """AFTER_TURN 在 Tool Result 和最终 Assistant 保存后各观察一次。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    call = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="lookup",
        arguments={},
        raw_arguments="{}",
    )
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(call,),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            LLMResponse(
                blocks=(TextBlock(text="done"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.STOP,
            ),
        )
    )
    observed: list[tuple[int | None, tuple[Message, ...]]] = []

    async def observe(context: MiddlewareContext) -> MiddlewareDecision:
        assert context.point is MiddlewarePoint.AFTER_TURN
        assert isinstance(context.payload, TurnMiddlewarePayload)
        observed.append((context.turn_index, context.payload.messages))
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="observe-turn",
            middleware_id="observe-turn",
            point=MiddlewarePoint.AFTER_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=observe,
        )
    )
    middleware_registry.freeze()

    class Handler:
        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            del ctx, arguments
            return ToolOutput(content="result")

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="lookup",
            description="lookup",
            parameters={"type": "object"},
            source=ToolSource.BUILTIN,
            handler=Handler(),
        )
    )
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        tool_executor=ToolExecutor(tools),
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(
            ("request-1", "assistant-1", "tool-1", "request-2", "assistant-2")
        ).__next__,
    )

    payloads, _, _ = await _run_loop(
        loop,
        RunRequest(
            ctx=_CONTEXT,
            run_id="run-1",
            trace_id="trace-1",
            output_mode=OutputMode.COMPLETE,
        ),
        NewRunInput(messages=(user,)),
        messages,
    )

    assert [turn for turn, _ in observed] == [1, 2]
    assert any(message.role is Role.TOOL for message in observed[0][1])
    assert any(message.role is Role.ASSISTANT for message in observed[1][1])
    tool_events = [
        payload for payload in payloads if isinstance(payload, ToolExecutionCompleted)
    ]
    assert all("<system-reminder>" not in item.result.content for item in tool_events)
    assert loop.state.status is SessionStatus.SUCCEEDED


async def test_context_provider_injections_cover_all_current_run_users() -> None:
    """当前 run 的每条 USER 都必须先保存 Provider 注入再发送给模型。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    first_user = _message("user-first", Role.USER, MessageOrigin.USER, "first", "run-1")
    last_user = _message("user-last", Role.USER, MessageOrigin.USER, "last", "run-1")
    messages = _MemoryMessages(system)
    response = LLMResponse(
        blocks=(TextBlock(text="done"),),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    llm = _SequenceProvider((response,))
    context_calls: list[str] = []
    before_turn_texts: list[str] = []

    async def observe_turn(context: MiddlewareContext) -> MiddlewareDecision:
        assert isinstance(context.payload, TurnMiddlewarePayload)
        before_turn_texts.extend(
            block.text
            for message in context.payload.messages
            if message.role is Role.USER
            for block in message.content
            if isinstance(block, TextBlock)
        )
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    class Provider(ContextProvider):
        provider_name = "profile"

        async def get(
            self,
            ctx: SessionContext,
            *,
            message: Message,
        ) -> InjectedItem | None:
            assert ctx == _CONTEXT
            context_calls.append(message.message_id)
            return InjectedItem(
                provider_name="profile",
                content=f"context:{message.message_id}",
            )

        async def put(self, ctx: SessionContext, *, message: Message) -> None:
            del ctx, message

    tools = ToolRegistry()
    tools.freeze()
    context_runner = ContextProviderRunner(
        timeout_seconds=1.0,
        clock=lambda: _NOW,
    )
    context_runner.register_many((Provider(),))
    context_runner.freeze()
    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="observe-injections",
            middleware_id="observe-injections",
            point=MiddlewarePoint.BEFORE_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=observe_turn,
        )
    )
    middleware_registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        context_provider_runner=context_runner,
        llm_client=_as_client(llm),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(first_user, last_user)),
        messages,
    )

    assert context_calls == ["user-first", "user-last"]
    injection_events = [
        payload
        for payload in payloads
        if isinstance(payload, MessagesCompleted)
        and isinstance(payload.messages[0].content[0], InjectedBlock)
    ]
    assert len(injection_events) == 1
    targets = tuple(
        block.target_user_message_id
        for message in injection_events[0].messages
        if isinstance(block := message.content[0], InjectedBlock)
    )
    assert targets == ("user-first", "user-last")
    sent_users = tuple(
        message for message in llm.requests[0].messages if message.role is Role.USER
    )
    first_injection = sent_users[0].content[0]
    last_injection = sent_users[1].content[0]
    assert isinstance(first_injection, TextBlock)
    assert isinstance(last_injection, TextBlock)
    assert "context:user-first" in first_injection.text
    assert "context:user-last" in last_injection.text
    assert any("context:user-first" in text for text in before_turn_texts)
    assert any("context:user-last" in text for text in before_turn_texts)


async def test_before_run_block_happens_after_initial_message_barrier() -> None:
    """BEFORE_RUN BLOCK 必须保住 destructive pop 的 USER，再收敛为 FAILED。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    provider = _SequenceProvider(())

    async def block(context: MiddlewareContext) -> MiddlewareDecision:
        assert context.point is MiddlewarePoint.BEFORE_RUN
        assert messages.committed == [user]
        return MiddlewareDecision(
            type=MiddlewareDecisionType.BLOCK,
            reason_code="run_policy_blocked",
        )

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="block-run",
            middleware_id="block-run",
            point=MiddlewarePoint.BEFORE_RUN,
            scope=MiddlewareScope.GLOBAL,
            handler=block,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert messages.committed == [user]
    assert provider.requests == []
    assert loop.state.status is SessionStatus.FAILED


async def test_after_llm_retry_discards_first_response_before_assistant_save(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AFTER_LLM RETRY 仅重试一次，丢弃响应不能形成 Assistant Message。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(TextBlock(text="discarded"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.STOP,
            ),
            LLMResponse(
                blocks=(TextBlock(text="kept"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.STOP,
            ),
        )
    )
    middleware_calls = 0
    before_turn_calls = 0

    async def observe_turn(_: MiddlewareContext) -> MiddlewareDecision:
        nonlocal before_turn_calls
        before_turn_calls += 1
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    async def retry_once(context: MiddlewareContext) -> MiddlewareDecision:
        nonlocal middleware_calls
        assert context.point is MiddlewarePoint.AFTER_LLM
        middleware_calls += 1
        if middleware_calls == 1:
            return MiddlewareDecision(type=MiddlewareDecisionType.RETRY)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="before-turn-once",
            middleware_id="before-turn-once",
            point=MiddlewarePoint.BEFORE_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=observe_turn,
        )
    )
    middleware_registry.register(
        MiddlewareRegistration(
            name="retry-once",
            middleware_id="retry-once",
            point=MiddlewarePoint.AFTER_LLM,
            scope=MiddlewareScope.GLOBAL,
            handler=retry_once,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request-1", "request-2", "assistant")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert len(provider.requests) == 2
    assert before_turn_calls == 1
    assert middleware_calls == 2
    assert sum(isinstance(item, LLMCallStarted) for item in payloads) == 2
    assistants = [item for item in messages.committed if item.role is Role.ASSISTANT]
    assert len(assistants) == 1
    assert assistants[0].content == (TextBlock(text="kept"),)
    assert loop.state.status is SessionStatus.SUCCEEDED
    assert "agent_loop_failed" not in caplog.text


async def test_on_error_middleware_can_handle_llm_error() -> None:
    """LLM 错误经 ON_ERROR HANDLED 后使用替代响应继续保存。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.ERROR,
                error=LLMError(
                    code="temporary",
                    message="provider detail",
                    retryable=True,
                ),
            ),
        )
    )

    async def handle(context: MiddlewareContext) -> MiddlewareDecision:
        assert context.point is MiddlewarePoint.ON_ERROR
        return MiddlewareDecision(
            type=MiddlewareDecisionType.HANDLED,
            payload=LLMResponse(
                blocks=(TextBlock(text="fallback"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="fallback-model",
                finish_reason=FinishReason.STOP,
            ),
        )

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="handle-error",
            middleware_id="handle-error",
            point=MiddlewarePoint.ON_ERROR,
            scope=MiddlewareScope.GLOBAL,
            handler=handle,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert messages.committed[-1].content == (TextBlock(text="fallback"),)
    assert loop.state.status is SessionStatus.SUCCEEDED


async def test_unhandled_llm_error_preserves_sanitized_provider_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未处理 LLM 错误不得退化为内部包装异常名称。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "hello", "run-1")
    messages = _MemoryMessages(system)
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.ERROR,
                error=LLMError(
                    code="connection_error",
                    message="private provider detail",
                    retryable=True,
                ),
            ),
        )
    )
    tools = ToolRegistry()
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    with caplog.at_level("ERROR"):
        payloads, _, _ = await _run_loop(
            loop,
            request,
            NewRunInput(messages=(user,)),
            messages,
        )

    failed = payloads[-1]
    assert isinstance(failed, RunFailed)
    assert failed.error.code == "connection_error"
    assert failed.error.message == "LLM provider 返回错误"
    assert failed.error.retryable is True
    assert "private provider detail" not in failed.error.message
    assert failed.error.error_id is not None
    assert failed.error.exception_type == "agentsystem.common.errors.RunFailure"
    assert failed.error.stacktrace is not None
    assert "_call_llm" in failed.error.stacktrace
    assert "private provider detail" in failed.error.stacktrace
    assert caplog.text.count("Traceback (most recent call last)") == 1
    assert f"error_id={failed.error.error_id}" in caplog.text


@pytest.mark.parametrize(
    ("inbound_created_at", "expected_created_at"),
    ((None, _NOW), (_EARLIER, _EARLIER)),
)
async def test_tool_round_promotes_system_steer_as_separate_input_message(
    inbound_created_at: datetime | None,
    expected_created_at: datetime,
) -> None:
    """SYSTEM STEER 只在工具安全点领取，并在下一 Turn 保留真实角色。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "start", "run-1")
    messages = _MemoryMessages(system)
    call = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="lookup",
        arguments={"query": "x"},
        raw_arguments='{"query":"x"}',
    )
    tool_response = LLMResponse(
        blocks=(call,),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.TOOL_CALLS,
    )
    final_response = LLMResponse(
        blocks=(TextBlock(text="done"),),
        timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
        model="provider-model",
        finish_reason=FinishReason.STOP,
    )
    provider = _SequenceProvider((tool_response, final_response))

    class Handler:
        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            assert ctx == _CONTEXT
            assert arguments == {"query": "x"}
            invocation = get_tool_context()
            assert invocation.session_context == _CONTEXT
            assert invocation.run_id == "run-1"
            assert invocation.runtime_trace_id == "trace-1"
            assert invocation.tool_call_id == "call-1"
            assert invocation.tool_name == "lookup"
            assert invocation.turn_index == 1
            return ToolOutput(content="tool-result")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="lookup",
            description="lookup",
            parameters={"type": "object"},
            source=ToolSource.BUILTIN,
            handler=Handler(),
        )
    )
    registry.freeze()
    queue = _Queue(
        (
            InboundMessage(
                message_id="system-steer",
                session_id="session",
                role=InboundRole.SYSTEM,
                content=(TextBlock(text="new direction"),),
                origin="workflow.steer",
                mode=InboundMode.STEER,
                created_at=inbound_created_at,
            ),
        )
    )
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        tool_executor=ToolExecutor(registry),
        inbox=queue,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(
            (
                "request-1",
                "assistant-1",
                "tool-message-1",
                "request-2",
                "assistant-2",
            )
        ).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, states, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )
    steer_state = next(
        state
        for payload, state in zip(payloads, states, strict=True)
        if isinstance(payload, MessagesCompleted)
        and payload.messages[0].message_id == "system-steer"
    )

    assert sum(isinstance(item, TurnStarted) for item in payloads) == 2
    assert sum(isinstance(item, ToolExecutionStarted) for item in payloads) == 1
    assert sum(isinstance(item, ToolExecutionCompleted) for item in payloads) == 1
    assert queue.calls == [frozenset({InboundMode.STEER})]
    assert steer_state is not None
    assert steer_state.steer_message_ids == ("system-steer",)
    started = next(
        payload
        for payload in payloads
        if isinstance(payload, MessageStarted)
        and payload.message.message_id == "system-steer"
    )
    assert started.message.role is Role.SYSTEM
    assert started.message.origin == "workflow.steer"
    assert provider.requests[1].messages[-3].role is Role.ASSISTANT
    assert provider.requests[1].messages[-2].role is Role.TOOL
    assert provider.requests[1].messages[-2].content == (
        ToolResultBlock(
            tool_call_id="call-1",
            tool_name="lookup",
            content="tool-result",
        ),
    )
    assert provider.requests[1].messages[-1].role is Role.SYSTEM
    assert provider.requests[1].messages[-1].content == (
        TextBlock(text="new direction"),
    )
    assert provider.requests[1].messages[-1].created_at == expected_created_at


async def test_parallel_safe_tools_overlap_and_start_in_model_order() -> None:
    """parallel_tool_calls=True 时连续 safe 工具必须并发执行。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "start", "run-1")
    messages = _MemoryMessages(system)
    calls = (
        ToolCallBlock(
            tool_call_id="call-a",
            tool_name="tool_a",
            arguments={},
            raw_arguments="{}",
        ),
        ToolCallBlock(
            tool_call_id="call-b",
            tool_name="tool_b",
            arguments={},
            raw_arguments="{}",
        ),
    )
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=calls,
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            LLMResponse(
                blocks=(TextBlock(text="done"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.STOP,
            ),
        )
    )
    active = 0
    maximum_active = 0
    entered = 0
    both_entered = asyncio.Event()

    class Handler:
        def __init__(self, name: str) -> None:
            self.name = name

        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            nonlocal active, maximum_active, entered
            del ctx, arguments
            active += 1
            entered += 1
            maximum_active = max(maximum_active, active)
            if entered == 2:
                both_entered.set()
            try:
                await asyncio.wait_for(both_entered.wait(), timeout=0.2)
                return ToolOutput(content=self.name)
            finally:
                active -= 1

    registry = ToolRegistry()
    for name in ("tool_a", "tool_b"):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                parameters={"type": "object"},
                source=ToolSource.BUILTIN,
                handler=Handler(name),
                is_concurrency_safe=True,
            )
        )
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        tool_executor=ToolExecutor(registry),
        generation_config=LLMGenerationConfig(
            model="request-model",
            parallel_tool_calls=True,
        ),
        clock=lambda: _NOW,
        id_factory=iter(
            (
                "request-1",
                "assistant-tools",
                "tool-message-a",
                "tool-message-b",
                "request-2",
                "assistant-final",
            )
        ).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    tool_events = [
        payload
        for payload in payloads
        if isinstance(payload, (ToolExecutionStarted, ToolExecutionCompleted))
    ]
    assert maximum_active == 2
    assert [type(item) for item in tool_events[:2]] == [
        ToolExecutionStarted,
        ToolExecutionStarted,
    ]
    assert [item.tool_call_id for item in tool_events[:2]] == ["call-a", "call-b"]


async def test_parallel_suspension_drains_started_sibling_before_pending() -> None:
    """并发组出现挂起时，已启动 sibling 必须完成并保存后再进入 PENDING。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "start", "run-1")
    messages = _MemoryMessages(system)
    calls = (
        ToolCallBlock(
            tool_call_id="approval-call",
            tool_name="approval",
            arguments={},
            raw_arguments="{}",
        ),
        ToolCallBlock(
            tool_call_id="sibling-call",
            tool_name="sibling",
            arguments={},
            raw_arguments="{}",
        ),
    )
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=calls,
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        )
    )
    sibling_finished = False

    class Handler:
        def __init__(self, content: str) -> None:
            self.content = content

        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            nonlocal sibling_finished
            del ctx, arguments
            if self.content == "sibling-result":
                await asyncio.sleep(0.01)
                sibling_finished = True
            return ToolOutput(content=self.content)

    class SuspendApproval:
        async def authorize(
            self,
            ctx: SessionContext,
            call: ToolCallBlock,
            definition: ToolDefinition,
        ) -> ToolAuthorizationDecision:
            del ctx, definition
            if call.tool_name == "approval":
                return ToolAuthorizationDecision.suspend("resume-token")
            return ToolAuthorizationDecision.allow()

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="approval",
            description="approval",
            parameters={"type": "object"},
            source=ToolSource.BUILTIN,
            handler=Handler("unused"),
            is_concurrency_safe=True,
            can_suspend=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="sibling",
            description="sibling",
            parameters={"type": "object"},
            source=ToolSource.BUILTIN,
            handler=Handler("sibling-result"),
            is_concurrency_safe=True,
        )
    )
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        tool_executor=ToolExecutor(registry, authorization=SuspendApproval()),
        generation_config=LLMGenerationConfig(
            model="request-model",
            parallel_tool_calls=True,
        ),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant", "sibling-message")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert sibling_finished is True
    completed_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, ToolExecutionCompleted)
    )
    finalized_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, MessagesCompleted)
        and payload.messages[0].role is Role.TOOL
    )
    suspended_index = next(
        index
        for index, payload in enumerate(payloads)
        if isinstance(payload, ToolExecutionSuspended)
    )
    assert completed_index < finalized_index < suspended_index
    assert loop.state.status is SessionStatus.PENDING
    assert loop.state.pending_tool_call_id == "approval-call"


async def test_tool_error_can_be_handled_by_on_error_middleware() -> None:
    """Tool 错误进入 ON_ERROR HANDLED，并保存替代 ToolResult。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "start", "run-1")
    messages = _MemoryMessages(system)
    call = ToolCallBlock(
        tool_call_id="blocked-call",
        tool_name="dangerous",
        arguments={},
        raw_arguments="{}",
    )
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(call,),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            LLMResponse(
                blocks=(TextBlock(text="handled"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.STOP,
            ),
        )
    )
    handler_calls = 0

    class Handler:
        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            nonlocal handler_calls
            del ctx, arguments
            handler_calls += 1
            return ToolOutput(content="must not run")

    async def block(_: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(
            type=MiddlewareDecisionType.BLOCK,
            reason_code="policy_denied",
        )

    async def handle_error(context: MiddlewareContext) -> MiddlewareDecision:
        assert isinstance(context.payload, ErrorMiddlewarePayload)
        assert context.payload.operation == "tool"
        assert context.payload.tool_call == call
        error_call = context.payload.tool_call
        assert error_call is not None
        return MiddlewareDecision(
            type=MiddlewareDecisionType.HANDLED,
            payload=ToolResultMiddlewarePayload(
                call=error_call,
                result=ToolResultBlock(
                    tool_call_id=error_call.tool_call_id,
                    tool_name=error_call.tool_name,
                    content="policy fallback",
                    is_error=False,
                ),
            ),
        )

    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="block-tool",
            middleware_id="block-tool",
            point=MiddlewarePoint.BEFORE_TOOL_CALL,
            scope=MiddlewareScope.GLOBAL,
            handler=block,
        )
    )
    middleware_registry.register(
        MiddlewareRegistration(
            name="handle-tool-error",
            middleware_id="handle-tool-error",
            point=MiddlewarePoint.ON_ERROR,
            scope=MiddlewareScope.GLOBAL,
            handler=handle_error,
        )
    )
    middleware_registry.freeze()
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="dangerous",
            description="dangerous",
            parameters={"type": "object"},
            source=ToolSource.BUILTIN,
            handler=Handler(),
        )
    )
    tools.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=tools,
        tool_executor=ToolExecutor(tools),
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(
            ("request-1", "assistant-1", "tool-message", "request-2", "assistant-2")
        ).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert handler_calls == 0
    tool_result = next(
        message.content[0]
        for message in messages.committed
        if message.role is Role.TOOL
    )
    assert isinstance(tool_result, ToolResultBlock)
    assert tool_result.tool_call_id == "blocked-call"
    assert tool_result.is_error is False
    assert tool_result.content == "policy fallback"
    assert loop.state.status is SessionStatus.SUCCEEDED


async def test_suspended_tool_resumes_same_turn_without_replaying_llm() -> None:
    """Resume 消费同一 token、恢复未闭合 ToolCall，且不重复 TurnStarted/LLM。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user-1", Role.USER, MessageOrigin.USER, "start", "run-1")
    messages = _MemoryMessages(system)
    call = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="approval_tool",
        arguments={},
        raw_arguments="{}",
    )
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(call,),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            LLMResponse(
                blocks=(TextBlock(text="resumed"),),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.STOP,
            ),
        )
    )

    observed_contexts: list[ToolInvocationContext] = []
    after_turns: list[int | None] = []

    async def observe_turn(context: MiddlewareContext) -> MiddlewareDecision:
        after_turns.append(context.turn_index)
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    class Handler:
        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, object],
        ) -> ToolOutput:
            del ctx, arguments
            observed_contexts.append(get_tool_context())
            return ToolOutput(content="approved")

    class SuspendOnce:
        def __init__(self) -> None:
            self.calls = 0

        async def authorize(
            self,
            ctx: SessionContext,
            call: ToolCallBlock,
            definition: ToolDefinition,
        ) -> ToolAuthorizationDecision:
            del ctx, call, definition
            self.calls += 1
            if self.calls == 1:
                return ToolAuthorizationDecision.suspend("resume-token")
            return ToolAuthorizationDecision.allow()

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="approval_tool",
            description="approval",
            parameters={"type": "object"},
            source=ToolSource.BUILTIN,
            handler=Handler(),
            can_suspend=True,
        )
    )
    registry.freeze()
    middleware_registry = MiddlewareRegistry()
    middleware_registry.register(
        MiddlewareRegistration(
            name="observe-resumed-turn",
            middleware_id="observe-resumed-turn",
            point=MiddlewarePoint.AFTER_TURN,
            scope=MiddlewareScope.GLOBAL,
            handler=observe_turn,
        )
    )
    middleware_registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        tool_executor=ToolExecutor(registry, authorization=SuspendOnce()),
        middleware_manager=MiddlewareManager(middleware_registry),
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(
            (
                "request-1",
                "assistant-1",
                "tool-message-1",
                "request-2",
                "assistant-2",
            )
        ).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )

    first, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert isinstance(first[-2], ToolExecutionSuspended)
    assert isinstance(first[-1], RunSuspended)
    assert loop.state.pending_tool_call_id == "call-1"
    assert after_turns == []

    resumed_request = request.model_copy(update={"trace_id": "trace-resumed"})
    resumed, _, _ = await _run_loop(
        loop,
        resumed_request,
        ResumeInput(resume_token="resume-token"),
        messages,
    )

    assert isinstance(resumed[0], RunResumed)
    assert isinstance(resumed[1], ToolExecutionStarted)
    assert isinstance(resumed[2], ToolExecutionCompleted)
    assert sum(isinstance(item, TurnStarted) for item in resumed) == 1
    assert len(provider.requests) == 2
    assert after_turns == [1, 2]
    assert loop.state.status is SessionStatus.SUCCEEDED
    assert observed_contexts[-1].run_id == "run-1"
    assert observed_contexts[-1].runtime_trace_id == "trace-resumed"
    assert observed_contexts[-1].turn_index == 1
    assert observed_contexts[-1].tool_call_id == "call-1"


async def test_finalize_closes_only_unresolved_tool_calls_before_cancel() -> None:
    """终态收尾只补未闭合 ToolResult，并在消息屏障后形成 CANCELED。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    messages = _MemoryMessages(system)
    first = ToolCallBlock(
        tool_call_id="call-1",
        tool_name="first",
        arguments={},
        raw_arguments="{}",
    )
    second = ToolCallBlock(
        tool_call_id="call-2",
        tool_name="second",
        arguments={},
        raw_arguments="{}",
    )
    assistant = Message(
        message_id="assistant-tools",
        session_id="session",
        role=Role.ASSISTANT,
        content=(first, second),
        origin=MessageOrigin.MODEL,
        visibility=MessageVisibility.ALL,
        run_id="run-1",
        created_at=_NOW,
    )
    completed = Message(
        message_id="tool-1",
        session_id="session",
        role=Role.TOOL,
        content=(
            ToolResultBlock(
                tool_call_id="call-1",
                tool_name="first",
                content="done",
            ),
        ),
        origin=MessageOrigin.TOOL,
        visibility=MessageVisibility.ALL,
        run_id="run-1",
        created_at=_NOW,
    )
    messages.committed.extend((assistant, completed))
    running = start_run(
        AgentState(session_context=_CONTEXT),
        run_id="run-1",
        output_mode=OutputMode.COMPLETE,
        now=_NOW,
    )
    pending = suspend_run(
        running,
        tool_call_id="call-2",
        resume_token="resume-token",
        now=_NOW,
    )
    registry = ToolRegistry()
    registry.freeze()
    loop = AgentLoop(
        initial_state=pending,
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(_SequenceProvider(())),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("synthetic-tool-2",)).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="settlement-trace",
        output_mode=OutputMode.COMPLETE,
    )
    error = RunError(
        code="superseded_by_input",
        message="挂起 run 已被新输入取代",
    )
    payloads: list[AgentEventPayload] = []

    async def collect(payload: AgentEventPayload) -> None:
        payloads.append(payload)

    await loop.finalize(
        request,
        target_status=SessionStatus.CANCELED,
        error=error,
        emit=collect,
    )

    assert [type(payload) for payload in payloads] == [
        ToolExecutionCancelled,
        MessagesCompleted,
        RunCanceled,
    ]
    canceled = payloads[0]
    assert isinstance(canceled, ToolExecutionCancelled)
    assert canceled.tool_call_id == "call-2"
    assert canceled.reason_code == "superseded_by_input"
    finalized = payloads[1]
    assert isinstance(finalized, MessagesCompleted)
    assert len(finalized.messages) == 1
    result = finalized.messages[0].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.tool_call_id == "call-2"
    assert result.is_error is True
    assert result.content == "工具执行结果未知或已中断"
    assert loop.state.status is SessionStatus.CANCELED
    assert loop.state.pending_tool_call_id is None


async def test_loop_failure_after_assistant_synthesizes_tool_result() -> None:
    """Assistant 已保存后发生运行时失败，必须先闭合其 ToolCall 再失败。"""
    system = _message("system", Role.SYSTEM, MessageOrigin.SYSTEM, "system", None)
    user = _message("user", Role.USER, MessageOrigin.USER, "run", "run-1")
    messages = _MemoryMessages(system)
    call = ToolCallBlock(
        tool_call_id="call-unstarted",
        tool_name="missing-executor",
        arguments={},
        raw_arguments="{}",
    )
    provider = _SequenceProvider(
        (
            LLMResponse(
                blocks=(call,),
                timing=Timing(started_at=_NOW, ended_at=_NOW, duration_ms=0),
                model="provider-model",
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        )
    )
    registry = ToolRegistry()
    registry.freeze()
    loop = AgentLoop(
        initial_state=AgentState(session_context=_CONTEXT),
        message_manager=messages,
        context_builder=ContextBuilder(),
        compactor=_as_compactor(_Compactor()),
        llm_client=_as_client(provider),
        tool_registry=registry,
        generation_config=LLMGenerationConfig(model="request-model"),
        clock=lambda: _NOW,
        id_factory=iter(("request", "assistant-tools", "synthetic-result")).__next__,
    )
    request = RunRequest(
        ctx=_CONTEXT,
        run_id="run-1",
        trace_id="trace-1",
        output_mode=OutputMode.COMPLETE,
    )
    payloads, _, _ = await _run_loop(
        loop,
        request,
        NewRunInput(messages=(user,)),
        messages,
    )

    assert [type(payload) for payload in payloads[-3:]] == [
        ToolExecutionCancelled,
        MessagesCompleted,
        RunFailed,
    ]
    synthetic = messages.committed[-1].content[0]
    assert isinstance(synthetic, ToolResultBlock)
    assert synthetic.tool_call_id == "call-unstarted"
    assert synthetic.is_error is True
    assert loop.state.status is SessionStatus.FAILED


def _message(
    message_id: str,
    role: Role,
    origin: str,
    text: str,
    run_id: str | None,
    *,
    visibility: MessageVisibility | None = None,
) -> Message:
    return Message(
        message_id=message_id,
        session_id="session",
        role=role,
        content=(TextBlock(text=text),),
        origin=origin,
        visibility=visibility
        or (
            MessageVisibility.MODEL
            if role is Role.SYSTEM and run_id is None
            else MessageVisibility.ALL
        ),
        run_id=run_id,
        created_at=_NOW,
    )
