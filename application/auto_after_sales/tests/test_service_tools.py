"""汽车售后 Plugin Tool 注册行为测试：并发安全标记与数据目录绑定。"""

from datetime import datetime, tzinfo
from pathlib import Path

import pytest
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.service.agents import ApplicationRoot
from agentsystem.tool.models import ToolDefinition

from application.auto_after_sales.plugins import service_tools


def _register_all(data_root: Path) -> dict[str, ToolDefinition]:
    """在测试数据目录上注册全部 Tool，返回 name → ToolDefinition。"""
    services = PluginSetupServices()
    services.register(ApplicationRoot, ApplicationRoot(path=data_root.parent))
    services.freeze()
    context = PluginSetupContext(
        plugin_id="application.auto_after_sales.service-tools",
        agent_id="auto_after_sales",
        origin=PluginOrigin.APPLICATION,
        options={},
        services=services,
    )
    service_tools.register(context)
    contribution = context._seal_and_extract()
    return {tool.name: tool for tool in contribution.tools}


def test_reminder_write_tools_are_not_concurrency_safe(data_root: Path) -> None:
    """reminders.json 的写 Tool 不得在同一 Tool batch 内并发执行。"""
    tools = _register_all(data_root)

    assert tools["generate_reminders"].is_concurrency_safe is False
    assert tools["review_reminder"].is_concurrency_safe is False
    assert tools["list_reminders"].is_concurrency_safe is True


def test_create_appointment_is_not_concurrency_safe(data_root: Path) -> None:
    """appointments.jsonl 的写 Tool 不得在同一 Tool batch 内并发执行。"""
    tools = _register_all(data_root)

    assert tools["create_appointment"].is_concurrency_safe is False
    assert tools["customer_lookup"].is_concurrency_safe is True


async def test_registered_reminder_tools_invoke_against_data_root(
    data_root: Path,
) -> None:
    """注册的提醒 Tool 通过测试数据目录闭环：生成后列表可读。"""
    tools = _register_all(data_root)

    generated = await tools["generate_reminders"].handler.invoke(
        ctx=object(),
        arguments={},
    )
    listed = await tools["list_reminders"].handler.invoke(
        ctx=object(),
        arguments={"status": "pending"},
    )

    assert "提醒草稿" in generated.content
    assert "rem-cust-001-maintenance" in listed.content


@pytest.mark.parametrize(
    ("date", "time_slot"),
    [
        ("2026-06-25", "15:00-16:00"),
        ("2026-10-20", "16:00-15:00"),
        ("2026-10-20", "invalid"),
    ],
)
async def test_booking_tool_rejects_invalid_times(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, date: str, time_slot: str
) -> None:
    """无效日期和时段应在登记前拒绝，不写入正式预约。"""
    from agentsystem.common.errors import ToolError

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "FrozenDatetime":
            return cls(2026, 10, 19, 17, tzinfo=tz)

    monkeypatch.setattr(service_tools, "datetime", FrozenDatetime)
    tools = _register_all(data_root)
    with pytest.raises(ToolError, match="当前时间"):
        await tools["create_appointment"].handler.invoke(
            ctx=object(),
            arguments={
                "customer_id": "cust-001",
                "service_type": "空调检测",
                "date": date,
                "time_slot": time_slot,
            },
        )
    assert not (data_root / "appointments.jsonl").exists()
