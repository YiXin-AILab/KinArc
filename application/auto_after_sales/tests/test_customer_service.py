"""CustomerService 查询与预约登记测试。"""

import contextlib
import json
import threading
from datetime import date
from pathlib import Path

import pytest

from application.auto_after_sales.services import customer_service
from application.auto_after_sales.services.customer_service import (
    Appointment,
    CustomerService,
)


def test_find_customers_by_name_fragment(data_root: Path) -> None:
    service = CustomerService(data_root)

    matches = service.find_customers("王建")

    assert [item.customer_id for item in matches] == ["cust-001"]


def test_find_customers_by_phone_and_vin(data_root: Path) -> None:
    service = CustomerService(data_root)

    by_phone = service.find_customers("00000002")
    by_vin = service.find_customers("ml056432")

    assert [item.customer_id for item in by_phone] == ["cust-002"]
    assert [item.customer_id for item in by_vin] == ["cust-003"]


def test_find_customers_with_blank_query_returns_empty(data_root: Path) -> None:
    service = CustomerService(data_root)

    assert service.find_customers("   ") == ()
    assert service.find_customers("不存在的人") == ()


def test_customer_dates_parsed(data_root: Path) -> None:
    service = CustomerService(data_root)

    customer = service.get_customer("cust-001")

    assert customer is not None
    assert customer.last_service_date == date(2026, 3, 22)
    assert customer.insurance_expiry == date(2027, 3, 10)
    assert service.get_customer("cust-404") is None


def test_list_advisors_filters_specialty_and_finds_oncall(data_root: Path) -> None:
    service = CustomerService(data_root)

    rescue = service.list_advisors("救援")

    assert [item.advisor_id for item in rescue] == ["adv-999"]
    oncall = service.emergency_advisor()
    assert oncall is not None
    assert oncall.advisor_id == "adv-999"


def test_create_appointment_appends_jsonl(data_root: Path) -> None:
    service = CustomerService(data_root)

    appointment = service.create_appointment(
        customer_id="cust-001",
        service_type="A保",
        date="2026-10-09",
        time_slot="09:00-10:00",
        note="需要取送车",
        created_at="2026-09-30T10:00:00",
    )

    assert appointment.appointment_id == "appt-0001"
    records = service.list_appointments("cust-001")
    assert len(records) == 1
    assert records[0].note == "需要取送车"


def test_create_appointment_rejects_unknown_customer(data_root: Path) -> None:
    service = CustomerService(data_root)

    with pytest.raises(ValueError, match="客户不存在"):
        service.create_appointment(
            customer_id="cust-404",
            service_type="A保",
            date="2026-10-09",
            time_slot="09:00-10:00",
            created_at="2026-09-30T10:00:00",
        )


def test_concurrent_create_appointment_generates_unique_intact_ids(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个线程并发登记预约：ID 必须唯一，JSONL 每行都是完整可解析 JSON。

    通过在 _iter_jsonl 计数返回后强制两个线程会合，稳定复现「双方读到
    同一行数快照」的竞态窗口；无锁时双方都会生成 appt-0001，修复后
    后进入临界区的线程必须重新计数得到 appt-0002。
    """
    service = CustomerService(data_root)

    barrier = threading.Barrier(2)
    original_iter = customer_service._iter_jsonl

    def iter_with_rendezvous(path: Path) -> tuple[dict[str, object], ...]:
        items = original_iter(path)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=2)
        return items

    monkeypatch.setattr(customer_service, "_iter_jsonl", iter_with_rendezvous)

    appointments: list[Appointment] = []
    errors: list[Exception] = []

    def create() -> None:
        try:
            appointments.append(
                service.create_appointment(
                    customer_id="cust-001",
                    service_type="A保",
                    date="2026-10-09",
                    time_slot="09:00-10:00",
                    created_at="2026-09-30T10:00:00",
                )
            )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert errors == []
    assert len(appointments) == 2
    returned_ids = {item.appointment_id for item in appointments}
    assert len(returned_ids) == 2

    lines = (data_root / "appointments.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    persisted_ids = {str(json.loads(line)["appointment_id"]) for line in lines}
    assert persisted_ids == returned_ids
