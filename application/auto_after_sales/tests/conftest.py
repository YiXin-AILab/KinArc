"""汽车售后测试夹具：测试自有最小数据，部署数据不参与行为测试。"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from application.auto_after_sales.plugins.service_tools import ServiceBundle

TODAY = "2026-09-30"

CUSTOMERS = [
    {
        "customer_id": "cust-001",
        "name": "王建国",
        "phone": "11100000001",
        "vin": "LE4WG8DB5PL012345",
        "model": "S4",
        "purchase_date": "2023-03-15",
        "last_service_date": "2026-03-22",
        "last_service_mileage": 38600,
        "insurance_expiry": "2027-03-10",
        "birthday": "1978-06-12",
        "preferred_advisor": "adv-001",
        "tags": ["金卡会员"],
    },
    {
        "customer_id": "cust-002",
        "name": "李慧敏",
        "phone": "11100000002",
        "vin": "LE4WG4AB2KL098761",
        "model": "S2 运动轿车",
        "purchase_date": "2022-11-08",
        "last_service_date": "2026-09-01",
        "last_service_mileage": 45200,
        "insurance_expiry": "2027-11-01",
        "birthday": "1990-10-03",
        "preferred_advisor": "adv-002",
        "tags": ["银卡会员"],
    },
    {
        "customer_id": "cust-003",
        "name": "张伟",
        "phone": "11100000003",
        "vin": "LE4TG7DB1ML056432",
        "model": "S3 豪华型",
        "purchase_date": "2024-01-20",
        "last_service_date": "2026-09-10",
        "last_service_mileage": 21800,
        "insurance_expiry": "2026-10-20",
        "birthday": "1982-12-01",
        "preferred_advisor": "adv-001",
        "tags": [],
    },
]

ADVISORS = [
    {
        "advisor_id": "adv-001",
        "name": "何军",
        "specialty": "机电维修",
        "shift": "早班 8:30-17:30",
        "phone": "11100000014",
    },
    {
        "advisor_id": "adv-999",
        "name": "高磊",
        "specialty": "应急救援",
        "shift": "7×24 值班",
        "phone": "11100000018",
        "emergency_oncall": True,
    },
]


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """构造测试专用数据目录。"""
    root = tmp_path / "data"
    root.mkdir()
    (root / "customers.json").write_text(
        json.dumps(CUSTOMERS, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "advisors.json").write_text(
        json.dumps(ADVISORS, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def bundle(data_root: Path) -> ServiceBundle:
    """按测试数据目录构建服务集合。"""
    return ServiceBundle.from_data_root(data_root)


@pytest.fixture
def notifications(data_root: Path) -> Callable[[], list[dict[str, object]]]:
    """读取已落地的通知记录。"""

    def _read() -> list[dict[str, object]]:
        path = data_root / "notifications.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    return _read
