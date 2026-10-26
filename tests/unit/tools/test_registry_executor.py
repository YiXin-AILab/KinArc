"""ToolRegistry、函数 Tool 构建、Executor 与默认授权测试。"""

import asyncio
import logging
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.message.models import ToolCallBlock
from agentsystem.tool.authorization import (
    AllowAllToolAuthorization,
    ToolAuthorizationDecision,
)
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.context import ToolInvocationContext, get_tool_context
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import (
    ToolDefinition,
    ToolOutput,
    ToolSource,
    ToolSuspension,
)
from agentsystem.tool.registry import ToolRegistry
from pydantic import BaseModel, Field

CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


class AddArguments(BaseModel):
    """加法工具参数。"""

    left: int
    right: int = Field(gt=0)


class AddHandler:
    """测试用异步工具 handler。"""

    async def invoke(
        self,
        *,
        ctx: SessionContext,
        arguments: dict[str, Any],
    ) -> ToolOutput:
        """返回参数之和。"""
        assert ctx == CONTEXT
        return ToolOutput(content=str(arguments["left"] + arguments["right"]))


class RaisingHandler:
    """抛出指定异常的测试 Tool handler。"""

    def __init__(self, error: BaseException) -> None:
        """保存下次调用应抛出的异常。"""
        self._error = error

    async def invoke(
        self,
        *,
        ctx: SessionContext,
        arguments: dict[str, Any],
    ) -> ToolOutput:
        """抛出测试指定的异常。"""
        del ctx, arguments
        raise self._error


def definition(*, can_suspend: bool = False) -> ToolDefinition:
    """构造加法工具定义。"""
    return ToolDefinition(
        name="add",
        description="Add two integers",
        parameters=AddArguments.model_json_schema(),
        source=ToolSource.BUILTIN,
        can_suspend=can_suspend,
        handler=AddHandler(),
        invocation_model=AddArguments,
    )


def call(arguments: dict[str, Any] | None) -> ToolCallBlock:
    """构造工具调用 Block。"""
    return ToolCallBlock(
        tool_call_id="call-1",
        tool_name="add",
        arguments=arguments,
        raw_arguments="{}",
    )


def invocation(tool_call: ToolCallBlock) -> ToolInvocationContext:
    """根据测试 ToolCall 构造可信 Runtime 调用上下文。"""
    return ToolInvocationContext(
        session_context=CONTEXT,
        run_id="run-1",
        runtime_trace_id="trace-1",
        tool_call_id=tool_call.tool_call_id,
        tool_name=tool_call.tool_name,
        turn_index=1,
    )


def test_registry_rejects_duplicates_and_mutation_after_freeze() -> None:
    """Registry 重名失败，freeze 后禁止修改。"""
    registry = ToolRegistry()
    registry.register(definition())
    with pytest.raises(ValueError, match="重复"):
        registry.register(definition())

    registry.freeze()
    assert registry.names() == ("add",)
    function_schema = registry.schemas()[0]["function"]
    assert isinstance(function_schema, dict)
    assert function_schema["name"] == "add"
    with pytest.raises(RuntimeError, match="冻结"):
        registry.register(definition())


def test_tool_registry_register_many_is_atomic() -> None:
    """非法 Tool 批次不能留下已经校验通过的前序定义。"""
    registry = ToolRegistry()
    valid = definition()
    invalid = replace(definition(), name="invalid name")

    with pytest.raises(ValueError, match="名称无效"):
        registry.register_many((valid, invalid))

    assert registry.names() == ()


async def test_build_function_tool_invokes_sync_function() -> None:
    """Builder 从函数签名生成稳定定义，并适配同步 ToolOutput。"""

    def multiply(left: int, right: int = 2) -> ToolOutput:
        """计算两个整数的乘积。

        后续段落不应进入模型可见的工具描述。
        """
        return ToolOutput(content=str(left * right))

    built = build_function_tool(
        multiply,
        source=ToolSource.CUSTOMIZED,
        rules="只接受整数。",
        can_suspend=True,
    )

    assert built.name == "multiply"
    assert built.description == "计算两个整数的乘积。"
    assert built.source is ToolSource.CUSTOMIZED
    assert built.rules == "只接受整数。"
    assert built.can_suspend is True
    assert built.invocation_model is not None
    assert built.invocation_model.model_validate({"left": 3}).model_dump() == {
        "left": 3,
        "right": 2,
    }
    assert await built.handler.invoke(ctx=object(), arguments={"left": 3}) == (
        ToolOutput(content="6")
    )


def test_build_function_tool_rejects_unsupported_signatures_and_schema() -> None:
    """不可建模的签名与错位 schema 必须在启动期失败。"""

    def missing_annotation(value: object) -> ToolOutput:
        """用于验证缺少类型注解时的拒绝行为。"""
        return ToolOutput(content=str(value))

    del missing_annotation.__annotations__["value"]

    def variadic(*values: int) -> ToolOutput:
        """用于验证可变位置参数的拒绝行为。"""
        return ToolOutput(content=str(values))

    def one_argument(value: str) -> ToolOutput:
        """接收单个字符串参数。"""
        return ToolOutput(content=value)

    with pytest.raises(ValueError, match="缺少类型注解"):
        build_function_tool(missing_annotation, source=ToolSource.BUILTIN)
    with pytest.raises(ValueError, match=r"\*args"):
        build_function_tool(variadic, source=ToolSource.BUILTIN)
    with pytest.raises(ValueError, match="可见参数不一致"):
        build_function_tool(
            one_argument,
            source=ToolSource.BUILTIN,
            parameters={"type": "object", "properties": {}},
        )


async def test_build_function_tool_enforces_tool_output_at_invocation() -> None:
    """普通返回值不能越过统一 ToolOutput 运行协议。"""

    async def invalid_result(value: str) -> str:
        """返回不符合工具输出协议的结果。"""
        return value

    built = build_function_tool(
        invalid_result,
        source=ToolSource.BUILTIN,
    )

    with pytest.raises(TypeError, match="ToolOutput"):
        await built.handler.invoke(ctx=object(), arguments={"value": "invalid"})


async def test_build_function_tool_definition_exposes_rules() -> None:
    """Builder 根据类型约束同时生成 Schema 和调用模型。"""

    async def multiply(left: int, right: int = 2) -> ToolOutput:
        """计算两个整数的乘积。"""
        return ToolOutput(content=str(left * right))

    built = build_function_tool(
        multiply,
        source=ToolSource.CUSTOMIZED,
        name="multiply",
        description="Multiply two integers.",
        rules="只接受整数。",
    )
    registry = ToolRegistry()
    registry.register(built)
    registry.freeze()

    assert registry.names() == ("multiply",)
    assert registry.rules() == (("multiply", "只接受整数。"),)
    schema = registry.schemas()[0]["function"]
    assert isinstance(schema, dict)
    parameters = schema["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["required"] == ["left"]
    executor = ToolExecutor(registry)
    tool_call = ToolCallBlock(
        tool_call_id="multiply-call",
        tool_name="multiply",
        arguments={"left": 3},
        raw_arguments='{"left":3}',
    )
    result = await executor.execute(invocation(tool_call), tool_call)
    assert not isinstance(result, ToolSuspension)
    assert result.block.content == "6"


async def test_built_tool_reads_context_without_schema_fields() -> None:
    """普通函数读取可信上下文，但模型 Schema 只包含业务参数。"""
    observed: list[ToolInvocationContext] = []

    async def observe(value: str) -> ToolOutput:
        """读取并记录可信的工具调用上下文。"""
        observed.append(get_tool_context())
        return ToolOutput(content=value)

    registry = ToolRegistry()
    registry.register(
        build_function_tool(
            observe,
            source=ToolSource.CUSTOMIZED,
            name="observe",
            description="Observe invocation context.",
        )
    )
    registry.freeze()
    tool_call = ToolCallBlock(
        tool_call_id="observe-call",
        tool_name="observe",
        arguments={"value": "ok"},
        raw_arguments='{"value":"ok"}',
    )
    context = invocation(tool_call)

    result = await ToolExecutor(registry).execute(context, tool_call)

    assert not isinstance(result, ToolSuspension)
    assert observed == [context]
    schema = registry.schemas()[0]["function"]
    assert isinstance(schema, dict)
    parameters = schema["parameters"]
    assert isinstance(parameters, dict)
    properties = parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"value"}
    assert parameters["required"] == ["value"]
    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_sync_handler_runs_in_worker_thread_without_blocking_loop() -> None:
    """同步 handler 必须离开事件循环线程，慢速同步 I/O 不得冻结其他协程。"""
    loop_thread = threading.get_ident()
    handler_threads: list[int] = []

    def slow(value: str) -> ToolOutput:
        """模拟耗时的同步处理函数。"""
        handler_threads.append(threading.get_ident())
        time.sleep(0.3)
        return ToolOutput(content=value)

    built = build_function_tool(slow, source=ToolSource.CUSTOMIZED)

    start = time.monotonic()
    result, _ = await asyncio.gather(
        built.handler.invoke(ctx=object(), arguments={"value": "ok"}),
        asyncio.sleep(0.3),
    )
    elapsed = time.monotonic() - start

    assert result == ToolOutput(content="ok")
    assert handler_threads != [loop_thread]
    assert elapsed < 0.5


async def test_async_handler_runs_inline_on_event_loop() -> None:
    """异步 handler 继续在事件循环线程内直接 await，不引入额外线程。"""
    loop_thread = threading.get_ident()

    async def fast(value: str) -> ToolOutput:
        """模拟立即返回的异步处理函数。"""
        assert threading.get_ident() == loop_thread
        return ToolOutput(content=value)

    built = build_function_tool(fast, source=ToolSource.CUSTOMIZED)

    assert await built.handler.invoke(ctx=object(), arguments={"value": "ok"}) == (
        ToolOutput(content="ok")
    )


async def test_sync_handler_exception_propagates_from_worker_thread() -> None:
    """同步 handler 在线程中抛出的异常必须原样传播给调用方。"""

    def broken(value: str) -> ToolOutput:
        """模拟抛出异常的同步处理函数。"""
        raise ValueError(f"bad {value}")

    built = build_function_tool(broken, source=ToolSource.CUSTOMIZED)

    with pytest.raises(ValueError, match="bad input"):
        await built.handler.invoke(ctx=object(), arguments={"value": "input"})


async def test_sync_handler_cancellation_releases_caller_but_thread_finishes() -> None:
    """取消只放弃等待；线程中的同步执行继续完成，写入必须自行保证原子性。"""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking(value: str) -> ToolOutput:
        """模拟阻塞的同步处理函数。"""
        started.set()
        release.wait(timeout=5)
        finished.set()
        return ToolOutput(content=value)

    built = build_function_tool(blocking, source=ToolSource.CUSTOMIZED)
    task = asyncio.create_task(
        built.handler.invoke(ctx=object(), arguments={"value": "ok"})
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not finished.is_set()

    release.set()
    await asyncio.to_thread(finished.wait, 5)
    assert finished.is_set()


async def test_sync_handler_timeout_is_enforced_by_executor() -> None:
    """同步 handler 阻塞超过 timeout_seconds 时必须收敛为 tool_timeout。"""

    def slow(left: int, right: int = 1) -> ToolOutput:
        """模拟耗时的同步加法工具。"""
        time.sleep(0.5)
        return ToolOutput(content=str(left + right))

    registry = ToolRegistry()
    registry.register(build_function_tool(slow, source=ToolSource.BUILTIN, name="add"))
    registry.freeze()
    executor = ToolExecutor(registry, timeout_seconds=0.05)
    tool_call = call({"left": 1, "right": 1})

    result = await executor.execute(invocation(tool_call), tool_call)

    assert not isinstance(result, ToolSuspension)
    assert result.block.is_error is True
    assert result.block.error_code == "tool_timeout"


async def test_allow_all_authorization_executes_valid_tool() -> None:
    """默认授权全部放行，并执行校验后的参数。"""
    registry = ToolRegistry()
    registry.register(definition())
    registry.freeze()
    executor = ToolExecutor(
        registry,
        authorization=AllowAllToolAuthorization(),
        timeout_seconds=1,
    )

    tool_call = call({"left": 2, "right": 3})
    result = await executor.execute(invocation(tool_call), tool_call)

    assert not isinstance(result, ToolSuspension)
    assert result.block.content == "5"
    assert result.block.is_error is False


async def test_executor_converts_invalid_arguments_and_unknown_tool_to_errors() -> None:
    """调用边界错误被归一化为 error ToolResult。"""
    registry = ToolRegistry()
    registry.register(definition())
    registry.freeze()
    executor = ToolExecutor(registry, authorization=AllowAllToolAuthorization())

    invalid_call = call({"left": 2, "right": 0})
    unknown_call = call({"left": 1, "right": 1}).model_copy(
        update={"tool_name": "missing"}
    )
    invalid = await executor.execute(invocation(invalid_call), invalid_call)
    unknown = await executor.execute(invocation(unknown_call), unknown_call)

    assert not isinstance(invalid, ToolSuspension)
    assert not isinstance(unknown, ToolSuspension)
    assert invalid.block.is_error is True
    assert unknown.block.is_error is True


async def test_expected_tool_error_is_returned_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ToolError 是模型可处理的预期结果，不应污染错误日志。"""
    registry = ToolRegistry()
    registry.register(
        replace(
            definition(),
            handler=RaisingHandler(
                ToolError(code="order_closed", message="订单已关闭")
            ),
        )
    )
    registry.freeze()
    executor = ToolExecutor(registry)
    tool_call = call({"left": 1, "right": 1})

    with caplog.at_level(logging.ERROR):
        result = await executor.execute(invocation(tool_call), tool_call)

    assert not isinstance(result, ToolSuspension)
    assert result.block.is_error is True
    assert result.block.error_code == "order_closed"
    assert result.block.content == "订单已关闭"
    assert not caplog.records
    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_unexpected_tool_error_is_safe_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未知 Tool 异常保留完整栈，但不得泄漏异常消息给模型或日志。"""
    registry = ToolRegistry()
    registry.register(
        replace(
            definition(),
            handler=RaisingHandler(RuntimeError("secret provider response")),
        )
    )
    registry.freeze()
    executor = ToolExecutor(registry, error_id_factory=lambda: "fixed-error-id")
    tool_call = call({"left": 1, "right": 1})

    with caplog.at_level(logging.ERROR):
        result = await executor.execute(invocation(tool_call), tool_call)

    assert not isinstance(result, ToolSuspension)
    assert result.block.is_error is True
    assert result.block.error_code == "tool_error"
    assert "fixed-error-id" in result.block.content
    assert "secret provider response" not in result.block.content
    assert "secret provider response" not in caplog.text
    assert caplog.text.count("Traceback (most recent call last)") == 1
    assert "error_id=fixed-error-id" in caplog.text
    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_tool_cancelled_error_propagates() -> None:
    """Task 取消是 run 控制信号，不得转换为 ToolResult。"""
    registry = ToolRegistry()
    registry.register(
        replace(definition(), handler=RaisingHandler(asyncio.CancelledError()))
    )
    registry.freeze()
    executor = ToolExecutor(registry)
    tool_call = call({"left": 1, "right": 1})

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(invocation(tool_call), tool_call)
    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_authorization_runs_outside_tool_context() -> None:
    """Authorization 只接收显式 SessionContext，不继承 handler 调用域。"""

    class ContextCheckingAuthorization:
        async def authorize(
            self,
            ctx: SessionContext,
            tool_call: ToolCallBlock,
            tool_definition: ToolDefinition,
        ) -> ToolAuthorizationDecision:
            del tool_call, tool_definition
            assert ctx == CONTEXT
            with pytest.raises(RuntimeError):
                get_tool_context()
            return ToolAuthorizationDecision.allow()

    registry = ToolRegistry()
    registry.register(definition())
    registry.freeze()
    tool_call = call({"left": 1, "right": 1})

    result = await ToolExecutor(
        registry,
        authorization=ContextCheckingAuthorization(),
    ).execute(invocation(tool_call), tool_call)

    assert not isinstance(result, ToolSuspension)
    assert result.block.is_error is False


async def test_timeout_cleans_current_task_tool_context() -> None:
    """Handler timeout 后 Executor 当前 Task 不保留 Tool Context。"""

    class WaitingHandler:
        async def invoke(
            self,
            *,
            ctx: SessionContext,
            arguments: dict[str, Any],
        ) -> ToolOutput:
            del ctx, arguments
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    registry = ToolRegistry()
    registry.register(replace(definition(), handler=WaitingHandler()))
    registry.freeze()
    tool_call = call({"left": 1, "right": 1})

    result = await ToolExecutor(registry, timeout_seconds=0.01).execute(
        invocation(tool_call),
        tool_call,
    )

    assert not isinstance(result, ToolSuspension)
    assert result.block.error_code == "tool_timeout"
    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_suspension_requires_tool_opt_in() -> None:
    """授权策略不得挂起未声明 can_suspend 的工具。"""

    class SuspendAuthorization:
        async def authorize(self, *_: Any, **__: Any) -> ToolAuthorizationDecision:
            return ToolAuthorizationDecision.suspend("resume-token")

    registry = ToolRegistry()
    registry.register(definition(can_suspend=False))
    registry.freeze()
    executor = ToolExecutor(registry, authorization=SuspendAuthorization())
    tool_call = call({"left": 1, "right": 1})

    result = await executor.execute(invocation(tool_call), tool_call)

    assert not isinstance(result, ToolSuspension)
    assert result.block.is_error is True


async def test_opted_in_tool_returns_suspension_token() -> None:
    """声明 can_suspend 的工具可把 continuation token 交给 AgentLoop。"""

    class SuspendAuthorization:
        async def authorize(self, *_: Any, **__: Any) -> ToolAuthorizationDecision:
            return ToolAuthorizationDecision.suspend("resume-token")

    registry = ToolRegistry()
    registry.register(definition(can_suspend=True))
    registry.freeze()
    executor = ToolExecutor(registry, authorization=SuspendAuthorization())
    tool_call = call({"left": 1, "right": 1})

    result = await executor.execute(invocation(tool_call), tool_call)

    assert isinstance(result, ToolSuspension)
    assert result.resume_token == "resume-token"
