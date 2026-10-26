"""ScheduleService 业务规则测试。"""

import base64
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.common.models import SessionContext
from agentsystem.inbox.models import (
    InboundMessage,
    InboundMode,
    ReadyEntry,
    ResumeReady,
    ResumeRequest,
)
from agentsystem.message.models import TextBlock
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.models import (
    DispatchResult,
    ScheduleRunStatus,
    ScheduleSessionMode,
    ScheduleTaskPatch,
    ScheduleTaskStatus,
    ScheduleTaskType,
)
from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.service import ScheduleService

_NOW = datetime(2026, 8, 18, 1, tzinfo=UTC)
_CONTEXT = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="current-session",
)


class RecordingQueue:
    """只记录 ScheduleService push 的 MessageQueue fake。"""

    def __init__(self, *, push_error: BaseException | None = None) -> None:
        self.push_error = push_error
        self.pushed: list[tuple[SessionContext, InboundMessage]] = []

    async def push(self, ctx: SessionContext, message: InboundMessage) -> None:
        """记录 push 或抛出预置错误。"""
        if self.push_error is not None:
            raise self.push_error
        self.pushed.append((ctx, message))

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


def _id_factory(values: tuple[str, ...]) -> Iterator[str]:
    yield from values


def _service(
    *ids: str,
    queue: RecordingQueue | None = None,
) -> ScheduleService:
    cron = CronSchedule("Asia/Shanghai")
    identifiers = _id_factory(tuple(ids) or ("task-1",))
    return ScheduleService(
        manager=LocalScheduleManager(cron=cron),
        cron=cron,
        clock=lambda: _NOW,
        id_factory=lambda: next(identifiers),
        queue=queue or RecordingQueue(),
        operation_timeout_seconds=1,
    )


async def test_create_uses_defaults_and_current_context() -> None:
    """创建默认为 enabled recurring fixed 且绑定当前会话。"""
    service = _service("task-1")

    task = await service.create_task(
        _CONTEXT,
        name="日报",
        prompt="总结昨天工作",
        cron_expression="0 9 * * *",
    )

    assert task.task_id == "task-1"
    assert task.app_id == _CONTEXT.app_id
    assert task.user_id == _CONTEXT.user_id
    assert task.agent_id == _CONTEXT.agent_id
    assert task.status is ScheduleTaskStatus.ENABLED
    assert task.task_type is ScheduleTaskType.RECURRING
    assert task.session_mode is ScheduleSessionMode.FIXED
    assert task.fixed_session_id == _CONTEXT.session_id


async def test_context_user_controls_run_and_cross_session_access() -> None:
    """工具上下文用户不能手动执行他人的任务，同用户跨会话可以查询。"""
    queue = RecordingQueue()
    service = _service("task-1", queue=queue)
    task = await service.create_task(
        _CONTEXT, name="私人练习", prompt="英语练习", cron_expression="0 9 * * *"
    )
    other = _CONTEXT.model_copy(update={"user_id": "other-user"})
    with pytest.raises(AdapterError) as raised:
        await service.run_now(other, task_id=task.task_id, invocation_key="foreign")
    assert raised.value.code == "schedule_task_not_found"
    assert not queue.pushed
    assert not (await service.list_tasks(other)).items
    same_user = _CONTEXT.model_copy(update={"session_id": "new-session"})
    assert (await service.list_tasks(same_user)).items == (task,)


async def test_fixed_create_binds_current_session() -> None:
    """fixed 创建保存 Tool 所在会话。"""
    service = _service("task-1")

    task = await service.create_task(
        _CONTEXT,
        name="跟踪",
        prompt="继续跟踪项目",
        cron_expression="0 9 * * *",
        session_mode=ScheduleSessionMode.FIXED,
    )

    assert task.fixed_session_id == _CONTEXT.session_id


async def test_create_rejects_invalid_cron_before_storage() -> None:
    """无效 Cron 不会生成 Task。"""
    service = _service("task-1")

    with pytest.raises(ToolError) as raised:
        await service.create_task(
            _CONTEXT,
            name="日报",
            prompt="总结",
            cron_expression="0 9 * *",
        )

    assert raised.value.code == "invalid_cron_expression"
    assert "分钟 小时 日 月 星期" in raised.value.message
    page = await service.list_tasks(_CONTEXT)
    assert page.items == ()


async def test_list_uses_opaque_cursor_and_current_scope() -> None:
    """Service 编码 Manager 的键集游标并隐藏内部结构。"""
    service = _service("task-a", "task-b")
    await service.create_task(
        _CONTEXT,
        name="A",
        prompt="A",
        cron_expression="0 9 * * *",
    )
    await service.create_task(
        _CONTEXT,
        name="B",
        prompt="B",
        cron_expression="0 10 * * *",
    )

    first = await service.list_tasks(_CONTEXT, limit=1)
    assert len(first.items) == 1
    assert first.cursor is not None
    second = await service.list_tasks(_CONTEXT, limit=1, cursor=first.cursor)
    assert len(second.items) == 1
    assert second.items[0].task_id != first.items[0].task_id


async def test_list_rejects_malformed_cursor_without_echoing_it() -> None:
    """非法游标返回稳定安全错误。"""
    service = _service("task-1")
    raw_cursor = "not-a-valid-cursor"

    with pytest.raises(ToolError) as raised:
        await service.list_tasks(_CONTEXT, cursor=raw_cursor)

    assert raised.value.code == "schedule_cursor_invalid"
    assert raw_cursor not in raised.value.message


async def test_update_switches_to_current_fixed_session() -> None:
    """isolated 切换到 fixed 时绑定本次 update 会话。"""
    service = _service("task-1")
    task = await service.create_task(
        _CONTEXT,
        name="日报",
        prompt="总结",
        cron_expression="0 9 * * *",
    )
    next_context = _CONTEXT.model_copy(update={"session_id": "next-session"})

    updated = await service.update_task(
        next_context,
        task_id=task.task_id,
        expected_version=task.version,
        patch=ScheduleTaskPatch(session_mode=ScheduleSessionMode.FIXED),
    )

    assert updated.fixed_session_id == "next-session"
    assert updated.version == 2


async def test_delete_preserves_run_history_contract() -> None:
    """软删除仅阻止未来执行。"""
    service = _service("task-1")
    task = await service.create_task(
        _CONTEXT,
        name="日报",
        prompt="总结",
        cron_expression="0 9 * * *",
    )

    deleted = await service.delete_task(
        _CONTEXT,
        task_id=task.task_id,
        expected_version=task.version,
    )

    assert deleted.deleted_at == _NOW
    assert deleted.status is ScheduleTaskStatus.DISABLED
    assert (await service.list_tasks(_CONTEXT)).items == ()


async def test_history_cursor_kind_cannot_be_used_for_task_list() -> None:
    """Task 和 Run 游标不可混用。"""
    service = _service("task-1")
    payload = json.dumps(
        {
            "v": 1,
            "kind": "run",
            "created_at": (_NOW + timedelta(seconds=1)).isoformat(),
            "schedule_run_id": "run-1",
        },
        separators=(",", ":"),
    )
    run_cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    with pytest.raises(ToolError) as raised:
        await service.list_tasks(_CONTEXT, cursor=run_cursor)

    assert raised.value.code == "schedule_cursor_invalid"


async def test_manual_run_materializes_and_pushes_exact_message_once() -> None:
    """手动执行物化后仅直投一条普通 QUEUE 消息。"""
    queue = RecordingQueue()
    service = _service(
        "task-1",
        "run-1",
        "session-1",
        "run-2",
        "session-2",
        queue=queue,
    )
    task = await service.create_task(
        _CONTEXT,
        name="日报",
        prompt="总结昨天工作",
        cron_expression="0 9 * * *",
    )

    result = await service.run_now(
        _CONTEXT,
        task_id=task.task_id,
        invocation_key="runtime-1:tool-call-1",
    )

    assert result.dispatch_result is DispatchResult.ACCEPTED
    assert result.run.status is ScheduleRunStatus.PENDING
    assert result.run.occurrence_key == "manual:runtime-1:tool-call-1"
    assert len(queue.pushed) == 1
    pushed_ctx, message = queue.pushed[0]
    assert pushed_ctx.session_id == _CONTEXT.session_id
    assert message.message_id == "schedule-run:run-1"
    assert message.session_id == _CONTEXT.session_id
    assert message.origin == "schedule"
    assert message.mode is InboundMode.QUEUE
    assert message.content == (TextBlock(text="总结昨天工作"),)

    repeated = await service.run_now(
        _CONTEXT,
        task_id=task.task_id,
        invocation_key="runtime-1:tool-call-1",
    )
    assert repeated.dispatch_result is DispatchResult.ALREADY_MATERIALIZED
    assert repeated.run.schedule_run_id == result.run.schedule_run_id
    assert len(queue.pushed) == 1


async def test_manual_run_works_while_task_is_disabled() -> None:
    """disabled 只禁止自动调度，不禁止立即执行。"""
    queue = RecordingQueue()
    service = _service("task-1", "run-1", "session-1", queue=queue)
    task = await service.create_task(
        _CONTEXT,
        name="日报",
        prompt="总结",
        cron_expression="0 9 * * *",
    )
    await service.update_task(
        _CONTEXT,
        task_id=task.task_id,
        expected_version=task.version,
        patch=ScheduleTaskPatch(status=ScheduleTaskStatus.DISABLED),
    )

    result = await service.run_now(
        _CONTEXT,
        task_id=task.task_id,
        invocation_key="runtime-1:tool-call-1",
    )

    assert result.dispatch_result is DispatchResult.ACCEPTED


async def test_fixed_manual_run_reuses_bound_session() -> None:
    """fixed 手动 Run 使用 Task 保存的会话。"""
    queue = RecordingQueue()
    service = _service("task-1", "run-1", queue=queue)
    task = await service.create_task(
        _CONTEXT,
        name="跟踪",
        prompt="继续跟踪",
        cron_expression="0 9 * * *",
        session_mode=ScheduleSessionMode.FIXED,
    )

    result = await service.run_now(
        _CONTEXT,
        task_id=task.task_id,
        invocation_key="runtime-1:tool-call-1",
    )

    assert result.run.session_id == _CONTEXT.session_id


async def test_queue_failure_stays_pending_and_records_safe_diagnostic() -> None:
    """MQ 失败不改状态、不重试，仅记录安全诊断。"""
    queue = RecordingQueue(push_error=RuntimeError("raw secret payload"))
    service = _service("task-1", "run-1", "session-1", queue=queue)
    task = await service.create_task(
        _CONTEXT,
        name="日报",
        prompt="sensitive prompt",
        cron_expression="0 9 * * *",
    )

    result = await service.run_now(
        _CONTEXT,
        task_id=task.task_id,
        invocation_key="runtime-1:tool-call-1",
    )

    assert result.dispatch_result is DispatchResult.FAILED
    history = await service.history(_CONTEXT)
    stored = history.items[0]
    assert stored.status is ScheduleRunStatus.PENDING
    assert stored.dispatch_error_code == "schedule_dispatch_failed"
    assert "secret" not in (stored.dispatch_error_message or "")
    assert "sensitive" not in (stored.dispatch_error_message or "")
