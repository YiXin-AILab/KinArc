"""普通 Tool 调用上下文的生命周期与并发隔离测试。"""

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.tool.context import (
    ToolInvocationContext,
    _bind_tool_context,
    get_tool_context,
)

SESSION = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def invocation(name: str = "lookup") -> ToolInvocationContext:
    """构造一份确定性的测试调用上下文。"""
    return ToolInvocationContext(
        session_context=SESSION,
        run_id="run-1",
        runtime_trace_id="trace-1",
        tool_call_id=f"call-{name}",
        tool_name=name,
        turn_index=1,
    )


def test_get_tool_context_requires_bound_invocation() -> None:
    """Tool 调用域外不得返回伪造或上次遗留的 Context。"""
    with pytest.raises(RuntimeError, match="只能在普通 Tool 调用期间使用"):
        get_tool_context()


def test_bind_tool_context_returns_frozen_context_and_restores_outer() -> None:
    """嵌套调用退出后恢复外层，并保持公开 Context 不可变。"""
    outer = invocation("outer")
    inner = invocation("inner")

    with _bind_tool_context(outer):
        assert get_tool_context() is outer
        with pytest.raises(FrozenInstanceError):
            outer.__setattr__("run_id", "changed")
        with _bind_tool_context(inner):
            assert get_tool_context() is inner
        assert get_tool_context() is outer

    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_parallel_tasks_keep_distinct_tool_contexts() -> None:
    """并发 Tool Task 各自读取创建它们时绑定的 Context。"""
    both_bound = asyncio.Event()
    bound_count = 0
    lock = asyncio.Lock()

    async def read_own(context: ToolInvocationContext) -> str:
        nonlocal bound_count
        with _bind_tool_context(context):
            async with lock:
                bound_count += 1
                if bound_count == 2:
                    both_bound.set()
            await both_bound.wait()
            return get_tool_context().tool_call_id

    first, second = await asyncio.gather(
        read_own(invocation("first")),
        read_own(invocation("second")),
    )

    assert (first, second) == ("call-first", "call-second")


async def test_child_task_keeps_context_after_parent_binding_exits() -> None:
    """Tool 派生 Task 保留创建时复制的调用上下文。"""
    release = asyncio.Event()

    async def child() -> ToolInvocationContext:
        await release.wait()
        return get_tool_context()

    context = invocation()
    with _bind_tool_context(context):
        task = asyncio.create_task(child())
        await asyncio.sleep(0)

    release.set()
    assert await task is context
    with pytest.raises(RuntimeError):
        get_tool_context()


async def test_to_thread_inherits_tool_context() -> None:
    """Tool 使用 asyncio.to_thread 时继承当前 Context。"""
    context = invocation()
    with _bind_tool_context(context):
        inherited = await asyncio.to_thread(get_tool_context)

    assert inherited is context
