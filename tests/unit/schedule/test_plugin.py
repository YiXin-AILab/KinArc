"""Schedule Builtin Plugin 注册测试。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agentsystem.middleware.models import MiddlewareFailureMode, MiddlewarePoint
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.scheduler import CronSchedule
from agentsystem.schedule.service import ScheduleService

from .test_tools import Queue


def test_schedule_plugins_register_tools_and_middlewares() -> None:
    """Schedule Tool 与 Middleware 必须由两个标准 Builtin Plugin 注册。"""
    package_root = Path(__file__).parents[3] / "agentsystem"
    sources_by_id = {
        item.plugin_id: item for item in discover_builtin_plugins(package_root)
    }
    tool_source = sources_by_id.get("builtin.schedule.schedule-task-tool")
    middleware_source = sources_by_id.get("builtin.schedule.schedule-run-middleware")
    assert tool_source is not None
    assert middleware_source is not None
    assert "builtin.schedule.scheduled-tasks" not in sources_by_id

    cron = CronSchedule("Asia/Shanghai")
    manager = LocalScheduleManager(cron=cron)
    service = ScheduleService(
        manager=manager,
        cron=cron,
        clock=lambda: datetime(2026, 8, 21, tzinfo=UTC),
        id_factory=lambda: "unused",
        queue=Queue(),
        operation_timeout_seconds=1,
    )
    services = PluginSetupServices()
    services.register(ScheduleService, service)
    services.freeze()

    tool_contribution, middleware_contribution = load_plugin_contributions(
        (tool_source, middleware_source),
        agent_id="agent",
        options={},
        services=services,
    )

    assert tuple(tool.name for tool in tool_contribution.tools) == (
        "schedule_task_create",
        "schedule_task_list",
        "schedule_task_update",
        "schedule_task_delete",
        "schedule_task_run",
        "schedule_task_history",
    )
    tools = {tool.name: tool for tool in tool_contribution.tools}
    create_properties = cast(
        dict[str, Any], tools["schedule_task_create"].parameters["properties"]
    )
    assert create_properties["cron_expression"]["pattern"] == (
        r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$"
    )
    assert create_properties["task_type"]["enum"] == ["recurring", "one_time"]
    assert "default" not in create_properties["task_type"]
    assert create_properties["session_mode"]["enum"] == ["isolated", "fixed"]
    assert create_properties["session_mode"]["default"] == "fixed"
    assert tools["schedule_task_create"].parameters["required"] == [
        "name",
        "prompt",
        "cron_expression",
        "task_type",
    ]

    list_properties = cast(
        dict[str, Any], tools["schedule_task_list"].parameters["properties"]
    )
    assert list_properties["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 20,
    }
    update_properties = cast(
        dict[str, Any], tools["schedule_task_update"].parameters["properties"]
    )
    assert update_properties["version"]["minimum"] == 1
    assert update_properties["status"]["anyOf"][0]["enum"] == [
        "enabled",
        "disabled",
    ]
    history_properties = cast(
        dict[str, Any], tools["schedule_task_history"].parameters["properties"]
    )
    assert history_properties["status"]["anyOf"][0]["enum"] == [
        "pending",
        "running",
        "succeeded",
        "failed",
        "canceled",
    ]
    assert middleware_contribution.tools == ()
    assert tool_contribution.middlewares == ()
    assert tuple(
        item.middleware_id for item in middleware_contribution.middlewares
    ) == (
        "builtin.schedule.schedule-run-middleware.run-start-tracker",
        "builtin.schedule.schedule-run-middleware.run-terminal-tracker",
    )
    before, after = middleware_contribution.middlewares
    assert before.point is MiddlewarePoint.BEFORE_RUN
    assert before.failure_mode is MiddlewareFailureMode.FAIL_CLOSED
    assert after.point is MiddlewarePoint.AFTER_RUN
    assert after.failure_mode is MiddlewareFailureMode.FAIL_OPEN
