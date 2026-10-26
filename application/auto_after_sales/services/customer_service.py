"""汽车售后客户与顾问档案服务。"""

import json
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class Customer:
    """单个保有客户档案。"""

    customer_id: str
    name: str
    phone: str
    vin: str
    model: str
    purchase_date: date
    last_service_date: date
    last_service_mileage: int
    insurance_expiry: date
    birthday: date
    preferred_advisor: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Advisor:
    """服务顾问档案；emergency_oncall 标记事故救援值班专员。"""

    advisor_id: str
    name: str
    specialty: str
    shift: str
    phone: str
    emergency_oncall: bool


@dataclass(frozen=True, slots=True)
class Appointment:
    """一条预约登记记录。"""

    appointment_id: str
    customer_id: str
    service_type: str
    date: str
    time_slot: str
    note: str
    created_at: str


class CustomerService:
    """加载并查询客户、顾问档案，登记预约记录。

    一致性边界：appointments.jsonl 是本地 JSONL mock 存储，
    「计数、生成 ID、追加」整个读-改-写由进程内 threading.Lock 串行化，
    每条记录单行一次写入，并发或写入异常时不留下半行 JSON；
    只保证同一进程内的并发一致，不支持跨进程/多 worker 原子性。
    """

    def __init__(self, data_root: Path) -> None:
        """从 data_root 加载 customers.json 与 advisors.json。"""
        self._data_root = data_root
        self._customers = tuple(
            _parse_customer(item)
            for item in _load_json_list(data_root / "customers.json")
        )
        self._advisors = tuple(
            _parse_advisor(item)
            for item in _load_json_list(data_root / "advisors.json")
        )
        self._appointments_path = data_root / "appointments.jsonl"
        self._appointment_lock = threading.Lock()

    def list_customers(self) -> tuple[Customer, ...]:
        """返回全部客户档案（只读视图）。"""
        return self._customers

    def find_customers(self, query: str) -> tuple[Customer, ...]:
        """按姓名、手机号或车架号模糊查找客户。

        Args:
            query: 姓名片段、手机号片段或 VIN 片段，大小写不敏感。

        Returns:
            命中的客户元组；未命中时为空元组。
        """
        needle = query.strip().lower()
        if not needle:
            return ()
        return tuple(
            customer
            for customer in self._customers
            if needle in customer.name.lower()
            or needle in customer.phone
            or needle in customer.vin.lower()
        )

    def get_customer(self, customer_id: str) -> Customer | None:
        """按 customer_id 精确查询；不存在时返回 None。"""
        for customer in self._customers:
            if customer.customer_id == customer_id:
                return customer
        return None

    def list_advisors(self, specialty: str | None = None) -> tuple[Advisor, ...]:
        """列出顾问；specialty 非空时按专长关键字过滤。"""
        if specialty is None or not specialty.strip():
            return self._advisors
        keyword = specialty.strip()
        return tuple(
            advisor for advisor in self._advisors if keyword in advisor.specialty
        )

    def emergency_advisor(self) -> Advisor | None:
        """返回标记为事故救援值班的顾问；未配置时返回 None。"""
        for advisor in self._advisors:
            if advisor.emergency_oncall:
                return advisor
        return None

    def create_appointment(
        self,
        *,
        customer_id: str,
        service_type: str,
        date: str,
        time_slot: str,
        note: str = "",
        created_at: str,
    ) -> Appointment:
        """登记一条预约并追加写入 appointments.jsonl。

        「读取已有行数 → 生成 ID → 追加 JSONL」整个临界区在同一把进程内
        锁中完成：同步 Tool 经 asyncio.to_thread 后可真正跨线程并发，
        Tool 的 is_concurrency_safe 标记只防同一批次并行，不能防不同
        session/批次的并发，因此锁必须覆盖完整读-改-写。

        Args:
            customer_id: 客户标识。
            service_type: 预约服务类型。
            date: 预约日期。
            time_slot: 预约时段。
            note: 预约备注。
            created_at: 由调用方提供的创建时间。

        Returns:
            已追加到预约文件的预约记录。

        Raises:
            ValueError: 客户不存在时抛出。
            OSError: 预约文件无法读取或追加。
        """
        if self.get_customer(customer_id) is None:
            raise ValueError(f"客户不存在：{customer_id}")
        with self._appointment_lock:
            existing = sum(1 for _ in _iter_jsonl(self._appointments_path))
            appointment = Appointment(
                appointment_id=f"appt-{existing + 1:04d}",
                customer_id=customer_id,
                service_type=service_type,
                date=date,
                time_slot=time_slot,
                note=note,
                created_at=created_at,
            )
            _append_jsonl(
                self._appointments_path,
                {
                    "appointment_id": appointment.appointment_id,
                    "customer_id": appointment.customer_id,
                    "service_type": appointment.service_type,
                    "date": appointment.date,
                    "time_slot": appointment.time_slot,
                    "note": appointment.note,
                    "created_at": appointment.created_at,
                },
            )
        return appointment

    def list_appointments(self, customer_id: str) -> tuple[Appointment, ...]:
        """查询某客户的全部预约记录。"""
        return tuple(
            Appointment(
                appointment_id=str(item["appointment_id"]),
                customer_id=str(item["customer_id"]),
                service_type=str(item["service_type"]),
                date=str(item["date"]),
                time_slot=str(item["time_slot"]),
                note=str(item.get("note", "")),
                created_at=str(item["created_at"]),
            )
            for item in _iter_jsonl(self._appointments_path)
            if item.get("customer_id") == customer_id
        )


def _load_json_list(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        payload: object = json.load(stream)
    if not isinstance(payload, list):
        raise ValueError(f"{path.name} 顶层必须是数组")
    return [cast(dict[str, object], item) for item in payload]


def _iter_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    if not path.exists():
        return ()
    items: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return tuple(items)


def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    """单行一次 write 追加；调用方必须持锁，保证并发时不混入半行 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_customer(item: dict[str, object]) -> Customer:
    return Customer(
        customer_id=str(item["customer_id"]),
        name=str(item["name"]),
        phone=str(item["phone"]),
        vin=str(item["vin"]),
        model=str(item["model"]),
        purchase_date=date.fromisoformat(str(item["purchase_date"])),
        last_service_date=date.fromisoformat(str(item["last_service_date"])),
        last_service_mileage=int(str(item["last_service_mileage"])),
        insurance_expiry=date.fromisoformat(str(item["insurance_expiry"])),
        birthday=date.fromisoformat(str(item["birthday"])),
        preferred_advisor=str(item["preferred_advisor"]),
        tags=_parse_tags(item.get("tags")),
    )


def _parse_tags(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(tag) for tag in raw)


def _parse_advisor(item: dict[str, object]) -> Advisor:
    return Advisor(
        advisor_id=str(item["advisor_id"]),
        name=str(item["name"]),
        specialty=str(item["specialty"]),
        shift=str(item["shift"]),
        phone=str(item["phone"]),
        emergency_oncall=bool(item.get("emergency_oncall", False)),
    )
