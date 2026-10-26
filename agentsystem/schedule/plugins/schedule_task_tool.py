"""对话式定时任务管理的六个 Builtin Tool。"""

import json
from collections.abc import Awaitable
from typing import TypeVar

from agentsystem.common.errors import AdapterError, ToolError
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.schedule.models import (
    MutableScheduleTaskStatus,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleSessionMode,
    ScheduleTask,
    ScheduleTaskPatch,
    ScheduleTaskStatus,
    ScheduleTaskType,
)
from agentsystem.schedule.service import ScheduleService
from agentsystem.tool.context import get_tool_context
from agentsystem.tool.models import ToolOutput

_Value = TypeVar("_Value")


class _ScheduleTaskTools:
    """将共享 ScheduleService 映射为六个对话式定时任务工具。

    每次调用从 Tool 上下文取得可信身份，负责输出序列化和 adapter
    错误转换，不拥有存储连接或调度循环。
    """

    def __init__(self, service: ScheduleService) -> None:
        """绑定一个 Application 级 ScheduleService。

        Args:
            service: Application 装配的共享业务服务。
        """
        self._service = service

    async def create(
        self,
        name: str,
        prompt: str,
        cron_expression: str,
        task_type: ScheduleTaskType,
        session_mode: ScheduleSessionMode = ScheduleSessionMode.FIXED,
    ) -> ToolOutput:
        """为当前 Agent 创建定时任务。

        Args:
            name: 供用户识别的任务名称。
            prompt: 触发时作为普通输入投递的文本。
            cron_expression: 部署时区下的标准五段 Cron 表达式。
            task_type: 周期或首次自动物化后完成的一次性任务。
            session_mode: 固定会话或每次独立会话的策略。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        context = get_tool_context()
        task = await self._call(
            self._service.create_task(
                context.session_context,
                name=name,
                prompt=prompt,
                cron_expression=cron_expression,
                task_type=task_type,
                session_mode=session_mode,
            )
        )
        return _output(_task_summary(task))

    async def list_tasks(
        self,
        status: ScheduleTaskStatus | None = None,
        task_type: ScheduleTaskType | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ToolOutput:
        """分页列出当前 Agent 的定时任务。

        Args:
            status: 任务状态筛选或修改值，None 表示未指定。
            task_type: 周期或首次自动物化后完成的一次性任务。
            cursor: 上一页返回的分页边界，None 表示首页。
            limit: 本次最多返回的记录数量。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        context = get_tool_context()
        page = await self._call(
            self._service.list_tasks(
                context.session_context,
                status=status,
                task_type=task_type,
                cursor=cursor,
                limit=limit,
            )
        )
        return _output(
            {
                "items": [_task_summary(task) for task in page.items],
                "cursor": page.cursor,
            }
        )

    async def update(
        self,
        task_id: str,
        version: int,
        name: str | None = None,
        prompt: str | None = None,
        cron_expression: str | None = None,
        task_type: ScheduleTaskType | None = None,
        session_mode: ScheduleSessionMode | None = None,
        status: MutableScheduleTaskStatus | None = None,
    ) -> ToolOutput:
        """修改、启用或停用当前 Agent 的定时任务。

        Args:
            task_id: 目标定时任务 ID。
            version: 调用方读取的任务版本，用于拒绝过期修改。
            name: 供用户识别的任务名称。
            prompt: 触发时作为普通输入投递的文本。
            cron_expression: 部署时区下的标准五段 Cron 表达式。
            task_type: 周期或首次自动物化后完成的一次性任务。
            session_mode: 固定会话或每次独立会话的策略。
            status: 任务状态筛选或修改值，None 表示未指定。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        changes = {
            key: value
            for key, value in {
                "name": name,
                "prompt": prompt,
                "cron_expression": cron_expression,
                "task_type": task_type,
                "session_mode": session_mode,
                "status": status,
            }.items()
            if value is not None
        }
        if not changes:
            raise ToolError(
                code="schedule_update_empty",
                message="请至少提供一个需修改的字段",
            )
        context = get_tool_context()
        task = await self._call(
            self._service.update_task(
                context.session_context,
                task_id=task_id,
                expected_version=version,
                patch=ScheduleTaskPatch.model_validate(changes),
            )
        )
        return _output(_task_summary(task))

    async def delete(self, task_id: str, version: int) -> ToolOutput:
        """软删除当前 Agent 的定时任务。

        Args:
            task_id: 目标定时任务 ID。
            version: 调用方读取的任务版本，用于拒绝过期修改。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        context = get_tool_context()
        task = await self._call(
            self._service.delete_task(
                context.session_context,
                task_id=task_id,
                expected_version=version,
            )
        )
        return _output(
            {
                "task_id": task.task_id,
                "deleted": True,
                "version": task.version,
            }
        )

    async def run(self, task_id: str) -> ToolOutput:
        """立即执行一次已有定时任务。

        Args:
            task_id: 目标定时任务 ID。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        context = get_tool_context()
        result = await self._call(
            self._service.run_now(
                context.session_context,
                task_id=task_id,
                invocation_key=f"{context.run_id}:{context.tool_call_id}",
            )
        )
        payload = _run_summary(result.run)
        payload["dispatch_result"] = result.dispatch_result.value
        return _output(payload)

    async def history(
        self,
        task_id: str | None = None,
        status: ScheduleRunStatus | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ToolOutput:
        """分页查询定时任务执行历史与失败原因。

        Args:
            task_id: 目标定时任务 ID。
            status: 执行状态筛选，None 表示不筛选。
            cursor: 上一页返回的分页边界，None 表示首页。
            limit: 本次最多返回的记录数量。

        Returns:
            含操作结果 JSON 文本的工具输出。
        """
        context = get_tool_context()
        page = await self._call(
            self._service.history(
                context.session_context,
                task_id=task_id,
                status=status,
                cursor=cursor,
                limit=limit,
            )
        )
        return _output(
            {
                "items": [_run_summary(run) for run in page.items],
                "cursor": page.cursor,
            }
        )

    async def _call(self, awaitable: Awaitable[_Value]) -> _Value:
        try:
            return await awaitable
        except AdapterError as error:
            raise ToolError(
                code=error.code,
                message=_safe_adapter_message(error.code),
            ) from error


def _task_summary(task: ScheduleTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "name": task.name,
        "prompt": task.prompt,
        "agent_id": task.agent_id,
        "cron_expression": task.cron_expression,
        "task_type": task.task_type.value,
        "session_mode": task.session_mode.value,
        "status": task.status.value,
        "version": task.version,
    }


def _run_summary(run: ScheduleRun) -> dict[str, object]:
    return {
        "schedule_run_id": run.schedule_run_id,
        "task_id": run.task_id,
        "task_name": run.task_name_snapshot,
        "prompt": run.prompt_snapshot,
        "agent_id": run.agent_id_snapshot,
        "session_id": run.session_id,
        "runtime_run_id": run.runtime_run_id,
        "occurrence_key": run.occurrence_key,
        "trigger_kind": run.trigger_kind.value,
        "scheduled_for": run.scheduled_for.isoformat(),
        "status": run.status.value,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "dispatch_error_code": run.dispatch_error_code,
        "dispatch_error_message": run.dispatch_error_message,
    }


def _output(payload: dict[str, object]) -> ToolOutput:
    return ToolOutput(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _safe_adapter_message(code: str) -> str:
    messages = {
        "schedule_task_not_found": "定时任务不存在",
        "schedule_task_conflict": "定时任务已存在",
        "schedule_version_conflict": "定时任务已被修改，请重新查询",
        "schedule_state_conflict": "定时任务当前状态不允许此操作",
        "schedule_run_conflict": "定时任务执行实例冲突",
    }
    return messages.get(code, "定时任务存储暂时不可用")


def register(context: PluginSetupContext) -> None:
    """Scheduler 启用时注册六个定时任务管理 Tool。

    Args:
        context: 插件装配上下文，提供依赖查询及注册入口。
    """
    service = context.get(ScheduleService)
    if service is None:
        return
    tools = _ScheduleTaskTools(service)
    context.register_tool(
        tools.create,
        name="schedule_task_create",
        description="创建定时任务，按指定时间执行任务内容。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "prompt": {"type": "string", "minLength": 1},
                "cron_expression": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "pattern": r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$",
                    "description": "不含年份和秒的五位 Cron 表达式。",
                },
                "task_type": {
                    "type": "string",
                    "enum": ["recurring", "one_time"],
                    "description": ("重复时间使用 recurring；单次时间使用 one_time。"),
                },
                "session_mode": {
                    "type": "string",
                    "enum": ["isolated", "fixed"],
                    "default": "fixed",
                    "description": "fixed 复用当前会话；isolated 每次创建独立会话。",
                },
            },
            "required": ["name", "prompt", "cron_expression", "task_type"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
    )
    context.register_tool(
        tools.list_tasks,
        name="schedule_task_list",
        description="查询已有定时任务，支持按状态和类型筛选。",
        rules=(
            "使用定时任务工具时遵守以下要求：按名称操作已有任务前，"
            "先调用 schedule_task_list 获取对应标识；存在同名任务时，"
            "结合执行时间、状态或类型确认目标，不要要求用户提供内部标识。"
            "向用户展示任务名称、执行时间、状态及必要的失败原因。"
            "除非用户明确要求技术详情，不展示内部标识、版本号或原始工具结果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": ["enabled", "disabled", "completed"],
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "task_type": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": ["recurring", "one_time"],
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "cursor": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    context.register_tool(
        tools.update,
        name="schedule_task_update",
        description="修改定时任务的名称、内容、执行时间或启停状态。",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "查询所得的当前版本号，用于校验任务未被修改。",
                },
                "name": {
                    "anyOf": [
                        {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "prompt": {
                    "anyOf": [
                        {"type": "string", "minLength": 1},
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "cron_expression": {
                    "anyOf": [
                        {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "pattern": r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "不含年份和秒的五位 Cron 表达式；null 保持不变。",
                },
                "task_type": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": ["recurring", "one_time"],
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "recurring 周期；one_time 单次；null 不修改。",
                },
                "session_mode": {
                    "anyOf": [
                        {"type": "string", "enum": ["isolated", "fixed"]},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "fixed 固定会话；isolated 独立会话；null 不修改。",
                },
                "status": {
                    "anyOf": [
                        {"type": "string", "enum": ["enabled", "disabled"]},
                        {"type": "null"},
                    ],
                    "default": None,
                },
            },
            "required": ["task_id", "version"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
    )
    context.register_tool(
        tools.delete,
        name="schedule_task_delete",
        description="删除定时任务，保留历史执行记录。",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "查询所得的当前版本号，用于校验任务未被修改。",
                },
            },
            "required": ["task_id", "version"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
    )
    context.register_tool(
        tools.run,
        name="schedule_task_run",
        description="立即执行一次定时任务，不影响后续执行计划。",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        is_concurrency_safe=False,
    )
    context.register_tool(
        tools.history,
        name="schedule_task_history",
        description="查询定时任务的执行历史和失败原因。",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "anyOf": [
                        {"type": "string", "minLength": 1},
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "status": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": [
                                "pending",
                                "running",
                                "succeeded",
                                "failed",
                                "canceled",
                            ],
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                },
                "cursor": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )


__all__ = ["register"]
