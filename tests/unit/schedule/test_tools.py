"""Schedule Builtin Tool 契约测试。"""

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    ReadyEntry,
    ResumeReady,
    ResumeRequest,
)
from agentsystem.message.models import ToolCallBlock
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.service import ScheduleService
from agentsystem.tool.context import ToolInvocationContext
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolExecutionResult, ToolSource
from agentsystem.tool.registry import ToolRegistry

_NOW = datetime(2026, 8, 18, 1, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session-current",
)


class Queue:
    """Tool 测试用 MessageQueue。"""

    def __init__(self) -> None:
        self.pushed: list[InboundMessage] = []

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        self.pushed.append(message)

    async def push_resume(self, request: ResumeRequest) -> None:
        raise AssertionError(request)

    async def read_ready(
        self,
        consumer_id: str,
        *,
        agent_ids: Sequence[str],
        count: int,
        block_ms: int,
    ) -> tuple[ReadyEntry, ...]:
        raise AssertionError((consumer_id, agent_ids, count, block_ms))

    async def requeue_resume(self, ready: ResumeReady) -> None:
        raise AssertionError(ready)

    async def claim(
        self,
        ctx: SessionContext,
        modes: frozenset[InboundMode],
        *,
        owner_token: str,
    ) -> tuple[InboundMessage, ...]:
        raise AssertionError((ctx, modes, owner_token))

    async def complete(
        self,
        ctx: SessionContext,
        message_ids: tuple[str, ...],
        *,
        owner_token: str,
    ) -> None:
        raise AssertionError((ctx, message_ids, owner_token))

    async def list_pending(self, ctx: SessionContext) -> tuple[InboundMessage, ...]:
        raise AssertionError(ctx)


def _ids(values: tuple[str, ...]) -> Iterator[str]:
    yield from values


def _executor() -> tuple[ToolExecutor, Queue]:
    cron = CronSchedule("Asia/Shanghai")
    manager = LocalScheduleManager(cron=cron)
    queue = Queue()
    values = _ids(("task-1", "run-1", "session-1"))
    service = ScheduleService(
        manager=manager,
        cron=cron,
        clock=lambda: _NOW,
        id_factory=lambda: next(values),
        queue=queue,
        operation_timeout_seconds=1,
    )
    services = PluginSetupServices()
    services.register(ScheduleService, service)
    services.freeze()
    source = next(
        item
        for item in discover_builtin_plugins(Path(__file__).parents[3] / "agentsystem")
        if item.plugin_id == "builtin.schedule.schedule-task-tool"
    )
    contribution = load_plugin_contributions(
        (source,),
        agent_id="agent",
        options={},
        services=services,
    )[0]
    registry = ToolRegistry()
    registry.register_many(contribution.tools)
    registry.freeze()
    return ToolExecutor(registry), queue


def _context(call: ToolCallBlock) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_context=_CONTEXT,
        run_id="runtime-1",
        runtime_trace_id="trace-1",
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        turn_index=0,
    )


async def _execute(
    executor: ToolExecutor,
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str,
) -> ToolExecutionResult:
    call = ToolCallBlock(
        tool_call_id=call_id,
        tool_name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )
    result = await executor.execute(_context(call), call)
    assert isinstance(result, ToolExecutionResult)
    return result


def test_definitions_are_exact_builtin_set_without_identity_fields() -> None:
    """六个 Tool 均不对模型暴露 app/user/agent/session 字段。"""
    executor, _ = _executor()
    definitions = executor._registry

    assert definitions.names() == (
        "schedule_task_create",
        "schedule_task_list",
        "schedule_task_update",
        "schedule_task_delete",
        "schedule_task_run",
        "schedule_task_history",
    )
    for definition in definitions._definitions.values():
        assert definition.source is ToolSource.BUILTIN
        properties = definition.parameters["properties"]
        assert isinstance(properties, dict)
        assert not {"app_id", "user_id", "agent_id", "session_id"} & set(properties)
    rules = definitions.rules()
    assert len(rules) == 1
    assert rules[0][0] == "schedule_task_list"


async def test_create_list_and_run_use_tool_context() -> None:
    """Tool 通过当前调用上下文创建并执行任务。"""
    executor, queue = _executor()
    created = await _execute(
        executor,
        "schedule_task_create",
        {
            "name": "日报",
            "prompt": "总结昨天工作",
            "cron_expression": "0 9 * * *",
            "task_type": "recurring",
        },
        call_id="create-call",
    )
    assert created.block.is_error is False
    task = json.loads(created.block.content)
    assert task["task_id"] == "task-1"
    assert task["agent_id"] == _CONTEXT.agent_id

    listed = await _execute(
        executor,
        "schedule_task_list",
        {},
        call_id="list-call",
    )
    assert json.loads(listed.block.content)["items"][0]["task_id"] == "task-1"
    assert json.loads(listed.block.content)["items"][0]["prompt"] == "总结昨天工作"
    assert task["prompt"] == "总结昨天工作"
    assert not {"created_at", "updated_at", "deleted_at"} & task.keys()
    assert (
        not {"created_at", "updated_at", "deleted_at"}
        & json.loads(listed.block.content)["items"][0].keys()
    )

    updated_prompt = "总结本周工作。\n逐字保留：<材料>研发与测试</材料>"
    updated = await _execute(
        executor,
        "schedule_task_update",
        {"task_id": "task-1", "version": 1, "prompt": updated_prompt},
        call_id="update-call",
    )
    assert not updated.block.is_error
    assert json.loads(updated.block.content)["prompt"] == updated_prompt
    assert (
        not {"created_at", "updated_at", "deleted_at"}
        & json.loads(updated.block.content).keys()
    )
    listed_after_update = await _execute(
        executor, "schedule_task_list", {}, call_id="updated-list-call"
    )
    assert (
        json.loads(listed_after_update.block.content)["items"][0]["prompt"]
        == updated_prompt
    )

    run = await _execute(
        executor,
        "schedule_task_run",
        {"task_id": "task-1"},
        call_id="run-call",
    )
    run_payload = json.loads(run.block.content)
    assert run_payload["dispatch_result"] == "accepted"
    assert run_payload["occurrence_key"] == "manual:runtime-1:run-call"
    assert run_payload["prompt"] == updated_prompt
    assert len(queue.pushed) == 1

    await _execute(
        executor,
        "schedule_task_update",
        {"task_id": "task-1", "version": 2, "prompt": "下次执行的新内容"},
        call_id="next-prompt-call",
    )
    history = await _execute(
        executor, "schedule_task_history", {}, call_id="history-call"
    )
    assert json.loads(history.block.content)["items"][0]["prompt"] == updated_prompt


async def test_update_rejects_empty_patch_with_safe_error() -> None:
    """update 必须至少提供一个可修改字段。"""
    executor, _ = _executor()

    result = await _execute(
        executor,
        "schedule_task_update",
        {"task_id": "missing", "version": 1},
        call_id="update-call",
    )

    assert result.block.is_error is True
    assert result.block.error_code == "schedule_update_empty"


async def test_create_exposes_invalid_cron_as_expected_tool_error() -> None:
    """非法 Cron 应给模型稳定修正提示，而不是生成通用 error_id。"""
    executor, _ = _executor()

    result = await _execute(
        executor,
        "schedule_task_create",
        {
            "name": "提醒",
            "prompt": "提醒投资股票",
            "cron_expression": "0 0 8 28 8",
            "task_type": "one_time",
        },
        call_id="invalid-cron-call",
    )

    assert result.block.is_error is True
    assert result.block.error_code == "invalid_cron_expression"
    assert "error_id=" not in result.block.content
