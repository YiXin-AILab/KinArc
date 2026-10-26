"""ReminderService 生成规则、频控幂等与审核链路测试。"""

import contextlib
import json
import threading
from collections.abc import Callable
from datetime import date, timedelta

import pytest

from application.auto_after_sales.plugins.service_tools import ServiceBundle
from application.auto_after_sales.services.reminder_service import (
    ReminderService,
)

TODAY = date(2026, 9, 30)

Notifications = Callable[[], list[dict[str, object]]]


def _types(records: tuple[dict[str, object], ...]) -> list[tuple[object, object]]:
    return [(item["customer_id"], item["type"]) for item in records]


def test_generate_creates_due_reminders(bundle: ServiceBundle) -> None:
    report = bundle.reminders.generate(today=TODAY)

    assert _types(report.created) == [
        ("cust-001", "maintenance"),
        ("cust-002", "birthday"),
        ("cust-003", "insurance"),
    ]
    assert report.skipped == ()
    maintenance = report.created[0]
    assert "S4" in str(maintenance["title"])
    assert "超期" in str(maintenance["reason"])
    assert maintenance["status"] == "pending"


def test_generate_is_idempotent_within_freq_window(bundle: ServiceBundle) -> None:
    first = bundle.reminders.generate(today=TODAY)
    second = bundle.reminders.generate(today=TODAY)

    assert len(first.created) == 3
    assert second.created == ()
    assert len(second.skipped) == 3
    pending = bundle.reminders.list_reminders("pending")
    assert len(pending) == 3


def test_freq_control_skips_sent_within_thirty_days(bundle: ServiceBundle) -> None:
    bundle.reminders.generate(today=TODAY)
    bundle.reminders.review("rem-cust-001-maintenance-20260930", "send")

    report = bundle.reminders.generate(today=TODAY + timedelta(days=5))

    created_ids = [item["customer_id"] for item in report.created]
    assert "cust-001" not in created_ids
    assert any(
        item["customer_id"] == "cust-001" and item["type"] == "maintenance"
        for item in report.skipped
    )


def test_generate_after_freq_window_creates_new_reminder(
    bundle: ServiceBundle,
) -> None:
    bundle.reminders.generate(today=TODAY)

    later = TODAY + timedelta(days=31)
    report = bundle.reminders.generate(today=later)

    created_ids = [item["reminder_id"] for item in report.created]
    assert created_ids == ["rem-cust-001-maintenance-20261031"]
    records = bundle.reminders.list_reminders()
    assert len(records) == 4  # 旧 3 条 + cust-001 保养新草稿


def test_review_send_emits_notification_and_marks_sent(
    bundle: ServiceBundle,
    notifications: Notifications,
) -> None:
    bundle.reminders.generate(today=TODAY)

    record = bundle.reminders.review("rem-cust-002-birthday-20260930", "send")

    assert record["status"] == "sent"
    sent = [n for n in notifications() if n["type"] == "reminder_send"]
    assert len(sent) == 1
    assert sent[0]["customer_id"] == "cust-002"
    assert sent[0]["phone"] == "11100000002"
    pending = bundle.reminders.list_reminders("pending")
    assert all(item["customer_id"] != "cust-002" for item in pending)


def test_review_dismiss_marks_dismissed_without_notification(
    bundle: ServiceBundle,
    notifications: Notifications,
) -> None:
    bundle.reminders.generate(today=TODAY)

    record = bundle.reminders.review("rem-cust-003-insurance-20260930", "dismiss")

    assert record["status"] == "dismissed"
    assert notifications() == []


def test_review_rejects_unknown_or_processed_reminder(
    bundle: ServiceBundle,
) -> None:
    bundle.reminders.generate(today=TODAY)
    bundle.reminders.review("rem-cust-001-maintenance-20260930", "send")

    with pytest.raises(ValueError, match="提醒不存在"):
        bundle.reminders.review("rem-cust-404-maintenance-20260930", "send")
    with pytest.raises(ValueError, match="不可审核"):
        bundle.reminders.review("rem-cust-001-maintenance-20260930", "send")


def test_reminders_file_roundtrip(bundle: ServiceBundle) -> None:
    bundle.reminders.generate(today=TODAY)

    payload = json.loads(bundle.reminders.path.read_text(encoding="utf-8"))

    assert isinstance(payload, list)
    assert len(payload) == 3
    assert {
        "reminder_id",
        "customer_id",
        "type",
        "title",
        "content",
        "reason",
        "status",
        "created_at",
    } <= set(payload[0])


def test_concurrent_review_same_reminder_succeeds_once_and_notifies_once(
    bundle: ServiceBundle,
    notifications: Notifications,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个线程同时审核同一提醒：只能成功一次，通知也只能发送一次。

    通过在 _load 返回后强制两个线程会合，稳定复现「双方读到同一
    pending 快照」的竞态窗口；修复后第二个线程在锁内重新加载时
    必须看到已处理状态并失败。
    """
    bundle.reminders.generate(today=TODAY)
    reminder_id = "rem-cust-001-maintenance-20260930"

    barrier = threading.Barrier(2)
    original_load = ReminderService._load

    def load_with_rendezvous(self: ReminderService) -> tuple[dict[str, object], ...]:
        records = original_load(self)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=2)
        return records

    monkeypatch.setattr(ReminderService, "_load", load_with_rendezvous)

    outcomes: list[dict[str, object] | ValueError] = []

    def review() -> None:
        try:
            outcomes.append(bundle.reminders.review(reminder_id, "send"))
        except ValueError as error:
            outcomes.append(error)

    threads = [threading.Thread(target=review) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    succeeded = [item for item in outcomes if not isinstance(item, ValueError)]
    failed = [item for item in outcomes if isinstance(item, ValueError)]
    assert len(succeeded) == 1
    assert len(failed) == 1
    assert succeeded[0]["status"] == "sent"

    sent = [n for n in notifications() if n["type"] == "reminder_send"]
    assert len(sent) == 1

    records = bundle.reminders.list_reminders()
    target = [item for item in records if item["reminder_id"] == reminder_id]
    assert len(target) == 1
    assert target[0]["status"] == "sent"


def test_concurrent_generate_and_review_persists_consistent_file(
    bundle: ServiceBundle,
) -> None:
    """生成与审核并发落盘后，reminders.json 必须是完整可解析的快照。"""
    bundle.reminders.generate(today=TODAY)
    reminder_id = "rem-cust-001-maintenance-20260930"
    errors: list[BaseException] = []
    created_ids: list[object] = []

    def generate_repeatedly() -> None:
        for offset in range(1, 6):
            try:
                # 间隔超过 30 天频控窗口，确保每次都会新增记录并落盘。
                report = bundle.reminders.generate(
                    today=TODAY + timedelta(days=31 * offset)
                )
                created_ids.extend(item["reminder_id"] for item in report.created)
            except ValueError as error:
                errors.append(error)

    def review_repeatedly() -> None:
        for _ in range(5):
            with contextlib.suppress(ValueError):
                bundle.reminders.review(reminder_id, "dismiss")

    threads = [
        threading.Thread(target=generate_repeatedly),
        threading.Thread(target=review_repeatedly),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert errors == []
    assert created_ids
    payload = json.loads(bundle.reminders.path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    # 3 条原始草稿 + 每次生成新增的提醒，任何一次落盘都不得丢更新。
    persisted_ids = {item["reminder_id"] for item in payload}
    assert len(payload) == 3 + len(created_ids)
    assert set(created_ids) <= persisted_ids
    dismissed = [item for item in payload if item["reminder_id"] == reminder_id]
    assert len(dismissed) == 1
    assert dismissed[0]["status"] == "dismissed"
