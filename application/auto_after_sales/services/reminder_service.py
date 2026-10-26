"""汽车售后定期招揽提醒生成与审核服务。"""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, cast

from application.auto_after_sales.services.customer_service import (
    Customer,
    CustomerService,
)
from application.auto_after_sales.services.notify_service import NotifyService

ReminderType = Literal["maintenance", "birthday", "insurance"]
ReminderStatus = Literal["pending", "sent", "dismissed"]

MAINTENANCE_INTERVAL_DAYS = 183
"""保养周期按半年（约 1 万公里）折算的天数。"""
MAINTENANCE_LEAD_DAYS = 14
"""保养到期前多少天开始提醒。"""
BIRTHDAY_LEAD_DAYS = 7
"""生日前多少天开始关怀提醒。"""
INSURANCE_LEAD_DAYS = 30
"""续保到期前多少天开始提醒。"""
FREQ_LIMIT_DAYS = 30
"""同客户同类型提醒的频控窗口；窗口内已有记录（含已发送）则跳过。"""

_SEASON_BY_MONTH = {
    12: "冬季",
    1: "冬季",
    2: "冬季",
    3: "春季",
    4: "春季",
    5: "春季",
    6: "夏季",
    7: "夏季",
    8: "夏季",
    9: "秋季",
    10: "秋季",
    11: "秋季",
}


@dataclass(frozen=True, slots=True)
class GenerateReport:
    """一次提醒生成的结果汇总。"""

    created: tuple[dict[str, object], ...]
    skipped: tuple[dict[str, str], ...]


class ReminderService:
    """扫描客户档案生成提醒草稿，并支持审核发送；频控保证幂等。

    一致性边界：存储是本地 JSON 文件的 mock 实现，读-改-写由进程内
    threading.Lock 串行化，落盘通过临时文件加 os.replace 原子覆盖；
    只保证同一进程内的并发一致，不支持跨进程/多 worker 原子性。
    review 的状态变更与发送通知在同一把锁内完成，同一提醒并发审核
    只会成功一次、发送一次。
    """

    def __init__(
        self,
        customer_service: CustomerService,
        reminders_path: Path,
        notify_service: NotifyService | None = None,
    ) -> None:
        """绑定客户档案、提醒存储文件与可选通知通道。

        Args:
            customer_service: 客户档案服务。
            reminders_path: 提醒草稿存储文件路径。
            notify_service: 可选本地通知记录服务。
        """
        self._customers = customer_service
        self._path = reminders_path
        self._notify = notify_service
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """返回提醒存储文件路径。"""
        return self._path

    def generate(self, today: date | None = None) -> GenerateReport:
        """扫描全部客户，生成到期提醒草稿。

        频控：同 customer_id+type 在 30 天内已存在记录（含已发送/已驳回）
        时跳过，因此重复调用不会产生重复 pending 草稿。

        Args:
            today: 业务当天日期，默认取系统当天；测试可注入。

        Returns:
            新建草稿与被跳过原因的汇总。
        """
        today = today or date.today()
        with self._lock:
            existing = self._load()
            cutoff = today - timedelta(days=FREQ_LIMIT_DAYS)
            recent_keys = {
                (str(item["customer_id"]), str(item["type"]))
                for item in existing
                if date.fromisoformat(str(item["created_at"])) >= cutoff
            }
            created: list[dict[str, object]] = []
            skipped: list[dict[str, str]] = []
            for customer in self._customers.list_customers():
                for candidate in _candidates_for(customer, today):
                    key = (customer.customer_id, str(candidate["type"]))
                    if key in recent_keys:
                        skipped.append(
                            {
                                "customer_id": customer.customer_id,
                                "type": str(candidate["type"]),
                                "reason": f"{FREQ_LIMIT_DAYS} 天内已有同类型提醒记录",
                            }
                        )
                        continue
                    record: dict[str, object] = {
                        "reminder_id": (
                            f"rem-{customer.customer_id}-"
                            f"{candidate['type']}-{today:%Y%m%d}"
                        ),
                        "customer_id": customer.customer_id,
                        "status": "pending",
                        "created_at": today.isoformat(),
                        **candidate,
                    }
                    created.append(record)
                    recent_keys.add(key)
            if created:
                self._save((*existing, *created))
        return GenerateReport(created=tuple(created), skipped=tuple(skipped))

    def list_reminders(
        self,
        status: ReminderStatus | None = None,
    ) -> tuple[dict[str, object], ...]:
        """列出提醒草稿；status 非空时按状态过滤。"""
        records = self._load()
        if status is None:
            return records
        return tuple(item for item in records if item.get("status") == status)

    def review(
        self,
        reminder_id: str,
        action: Literal["send", "dismiss"],
    ) -> dict[str, object]:
        """审核 pending 提醒，保存发送或驳回状态，并按需记录本地通知。

        状态变更与通知记录在同一把进程内锁中完成，避免并发重复审核。
        提醒状态先落盘；未配置通知服务时仅更新状态，通知写入失败时状态不回滚。

        Args:
            reminder_id: 提醒标识。
            action: 审核动作：send 或 dismiss。

        Returns:
            审核后的提醒记录；send 在配置了通知服务时追加本地通知。

        Raises:
            ValueError: 提醒不存在或当前状态不是 pending 时抛出。
            OSError: 提醒文件或通知文件无法写入。
        """
        with self._lock:
            records = list(self._load())
            for index, record in enumerate(records):
                if record.get("reminder_id") != reminder_id:
                    continue
                if record.get("status") != "pending":
                    raise ValueError(f"提醒 {reminder_id} 已处理，当前状态不可审核")
                record["status"] = "sent" if action == "send" else "dismissed"
                records[index] = record
                self._save(tuple(records))
                if action == "send":
                    self._notify_send(record)
                return record
        raise ValueError(f"提醒不存在：{reminder_id}")

    def _notify_send(self, record: dict[str, object]) -> None:
        if self._notify is None:
            return
        customer = self._customers.get_customer(str(record["customer_id"]))
        self._notify.send_reminder(
            reminder_id=str(record["reminder_id"]),
            customer_id=str(record["customer_id"]),
            customer_name=customer.name if customer is not None else "",
            phone=customer.phone if customer is not None else "",
            title=str(record["title"]),
            content=str(record["content"]),
        )

    def _load(self) -> tuple[dict[str, object], ...]:
        if not self._path.exists():
            return ()
        with self._path.open(encoding="utf-8") as stream:
            payload: object = json.load(stream)
        if not isinstance(payload, list):
            raise ValueError(f"{self._path.name} 顶层必须是数组")
        return tuple(cast(dict[str, object], item) for item in payload)

    def _save(self, records: tuple[dict[str, object], ...]) -> None:
        """先写临时文件再 os.replace 原子覆盖，避免取消/崩溃留下半截 JSON。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(list(records), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary_path, self._path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _candidates_for(customer: Customer, today: date) -> tuple[dict[str, str], ...]:
    """按三类招揽规则为单个客户产出候选提醒。"""
    candidates: list[dict[str, str]] = []
    maintenance = _maintenance_candidate(customer, today)
    if maintenance is not None:
        candidates.append(maintenance)
    birthday = _birthday_candidate(customer, today)
    if birthday is not None:
        candidates.append(birthday)
    insurance = _insurance_candidate(customer, today)
    if insurance is not None:
        candidates.append(insurance)
    return tuple(candidates)


def _maintenance_candidate(customer: Customer, today: date) -> dict[str, str] | None:
    due = customer.last_service_date + timedelta(days=MAINTENANCE_INTERVAL_DAYS)
    days_to_due = (due - today).days
    if days_to_due > MAINTENANCE_LEAD_DAYS:
        return None
    season = _SEASON_BY_MONTH[today.month]
    car_age = max(1, today.year - customer.purchase_date.year)
    if days_to_due >= 0:
        reason = f"距上次保养已近半年，预计 {due.isoformat()} 到期"
    else:
        reason = f"保养已超期 {-days_to_due} 天（应于 {due.isoformat()} 前完成）"
    return {
        "type": "maintenance",
        "title": f"{customer.model} 保养到期提醒",
        "content": (
            f"尊敬的{customer.name}，您的爱车 {customer.model} 已陪伴您约 "
            f"{car_age} 年，行驶至上回保养里程 {customer.last_service_mileage} "
            f"公里。按半年/1 万公里的保养周期，您的爱车{reason}。{season}用车"
            "对油液与空调系统要求更高，及时保养能让爱车保持最佳状态。"
            "回复本消息即可一键预约到店保养。"
        ),
        "reason": reason,
    }


def _birthday_candidate(customer: Customer, today: date) -> dict[str, str] | None:
    next_birthday = _next_birthday(customer.birthday, today)
    days_to_birthday = (next_birthday - today).days
    if days_to_birthday > BIRTHDAY_LEAD_DAYS:
        return None
    reason = f"客户生日 {next_birthday.isoformat()} 临近（{days_to_birthday} 天后）"
    return {
        "type": "birthday",
        "title": f"{customer.name} 生日关怀",
        "content": (
            f"亲爱的{customer.name}，{next_birthday.month} 月 "
            f"{next_birthday.day} 日是您的生日，汽车售后团队提前祝您"
            f"生日快乐！生日当月驾驶您的 {customer.model} 进店保养可享工时费 "
            "8 折生日礼遇，期待与您相见。"
        ),
        "reason": reason,
    }


def _insurance_candidate(customer: Customer, today: date) -> dict[str, str] | None:
    days_to_expiry = (customer.insurance_expiry - today).days
    if days_to_expiry < 0 or days_to_expiry > INSURANCE_LEAD_DAYS:
        return None
    reason = (
        f"车险将于 {customer.insurance_expiry.isoformat()} 到期"
        f"（还剩 {days_to_expiry} 天）"
    )
    return {
        "type": "insurance",
        "title": f"{customer.model} 续保提醒",
        "content": (
            f"尊敬的{customer.name}，您爱车 {customer.model} 的车险即将到期。"
            "续保选择本店可享原厂配件保障与直赔通道，还可按保费档位获赠保养"
            "代金券。如需报价，回复本消息或直接到店，我们将为您提供多家保险"
            "公司比价方案。"
        ),
        "reason": reason,
    }


def _next_birthday(birthday: date, today: date) -> date:
    """计算下一个生日日期；今天生日视为今天，2 月 29 日在平年按 2 月 28 日计。"""
    candidate = _replace_year(birthday, today.year)
    if candidate < today:
        candidate = _replace_year(birthday, today.year + 1)
    return candidate


def _replace_year(value: date, year: int) -> date:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)
