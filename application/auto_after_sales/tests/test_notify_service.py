"""NotifyService 通知落地与过滤测试。"""

from pathlib import Path

from application.auto_after_sales.services.notify_service import NotifyService


def test_emit_writes_jsonl_with_type_and_timestamp(data_root: Path) -> None:
    service = NotifyService(data_root / "notifications.jsonl")

    service.forward_to_advisor(
        summary="客户要预约 B 保",
        advisor_id="adv-001",
        advisor_name="何军",
        customer_id="cust-001",
    )
    service.escalate_emergency(
        description="高速追尾，人员安全",
        oncall_advisor_id="adv-999",
        oncall_name="高磊",
        oncall_phone="11100000018",
        customer_id="cust-002",
        location="G2 高速 120 公里处",
    )
    service.send_reminder(
        reminder_id="rem-cust-001-maintenance-20260930",
        customer_id="cust-001",
        customer_name="王建国",
        phone="11100000001",
        title="保养到期提醒",
        content="您的爱车该保养了",
    )

    records = service.list_notifications()
    assert [item["type"] for item in records] == [
        "advisor_forward",
        "emergency_escalation",
        "reminder_send",
    ]
    assert all("created_at" in item for item in records)
    emergency = records[1]
    assert emergency["oncall_name"] == "高磊"
    assert emergency["location"] == "G2 高速 120 公里处"


def test_list_notifications_filters_by_type(data_root: Path) -> None:
    service = NotifyService(data_root / "notifications.jsonl")
    service.forward_to_advisor(
        summary="s",
        advisor_id="adv-001",
        advisor_name="何军",
        customer_id=None,
    )
    service.send_reminder(
        reminder_id="r1",
        customer_id="cust-001",
        customer_name="王建国",
        phone="11100000001",
        title="t",
        content="c",
    )

    reminders = service.list_notifications("reminder_send")

    assert len(reminders) == 1
    assert reminders[0]["reminder_id"] == "r1"


def test_list_notifications_empty_before_any_send(data_root: Path) -> None:
    service = NotifyService(data_root / "notifications.jsonl")

    assert service.list_notifications() == ()
