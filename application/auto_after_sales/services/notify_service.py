"""汽车售后 Mock 通知通道：写 notifications.jsonl 并输出日志。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

NotificationType = Literal[
    "advisor_forward",
    "emergency_escalation",
    "reminder_send",
]

logger = logging.getLogger(__name__)


class NotifyService:
    """把业务通知持久化为 JSONL；真实环境可替换为短信/企微通道。"""

    def __init__(self, notifications_path: Path) -> None:
        """绑定通知落地文件路径，父目录缺失时自动创建。"""
        self._path = notifications_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """返回通知落地文件路径。"""
        return self._path

    def forward_to_advisor(
        self,
        *,
        summary: str,
        advisor_id: str,
        advisor_name: str,
        customer_id: str | None,
    ) -> dict[str, object]:
        """记录顾问转接通知，追加到本地 JSONL 文件，不调用外部发送通道。

        Args:
            summary: 待记录的对话摘要。
            advisor_id: 顾问标识。
            advisor_name: 顾问姓名。
            customer_id: 客户标识。

        Returns:
            已追加到本地通知文件的顾问转接记录。

        Raises:
            OSError: 通知文件无法追加。
        """
        return self._emit(
            "advisor_forward",
            {
                "advisor_id": advisor_id,
                "advisor_name": advisor_name,
                "customer_id": customer_id,
                "summary": summary,
            },
        )

    def escalate_emergency(
        self,
        *,
        description: str,
        oncall_advisor_id: str,
        oncall_name: str,
        oncall_phone: str,
        customer_id: str | None,
        location: str | None,
    ) -> dict[str, object]:
        """记录应急升级通知，追加到本地 JSONL 文件，不调用外部发送通道。

        Args:
            description: 资源或业务内容的描述。
            oncall_advisor_id: 值班顾问标识。
            oncall_name: 值班顾问姓名。
            oncall_phone: 值班顾问联系电话。
            customer_id: 客户标识。
            location: 事故地点，可省略。

        Returns:
            已追加到本地通知文件的应急升级记录。

        Raises:
            OSError: 通知文件无法追加。
        """
        return self._emit(
            "emergency_escalation",
            {
                "oncall_advisor_id": oncall_advisor_id,
                "oncall_name": oncall_name,
                "oncall_phone": oncall_phone,
                "customer_id": customer_id,
                "location": location,
                "description": description,
            },
        )

    def send_reminder(
        self,
        *,
        reminder_id: str,
        customer_id: str,
        customer_name: str,
        phone: str,
        title: str,
        content: str,
    ) -> dict[str, object]:
        """记录已审核的客户提醒，追加到本地 JSONL 文件，不调用外部发送通道。

        Args:
            reminder_id: 提醒标识。
            customer_id: 客户标识。
            customer_name: 客户姓名。
            phone: 客户联系电话。
            title: 提醒标题。
            content: 正文文本。

        Returns:
            已追加到本地通知文件的客户提醒记录。

        Raises:
            OSError: 通知文件无法追加。
        """
        return self._emit(
            "reminder_send",
            {
                "reminder_id": reminder_id,
                "customer_id": customer_id,
                "customer_name": customer_name,
                "phone": phone,
                "title": title,
                "content": content,
            },
        )

    def list_notifications(
        self,
        notification_type: NotificationType | None = None,
    ) -> tuple[dict[str, object], ...]:
        """读取已发出的通知；可按类型过滤。"""
        if not self._path.exists():
            return ()
        items: list[dict[str, object]] = []
        with self._path.open(encoding="utf-8") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                item = json.loads(stripped)
                if notification_type is None or item.get("type") == notification_type:
                    items.append(item)
        return tuple(items)

    def _emit(
        self,
        notification_type: NotificationType,
        payload: dict[str, object],
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "type": notification_type,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **payload,
        }
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("notification emitted type=%s", notification_type)
        return record
