"""Tau3 banking discoverable 工具所需的确定性数据库辅助函数。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, overload

if TYPE_CHECKING:
    from application.banking_agent.services.banking_db_service import BankingDBService


def get_today_str() -> str:
    """返回 Tau3 banking 场景锁定日期。"""
    return "11/14/2025"


def get_now() -> datetime:
    """返回 Tau3 banking 场景锁定时间。"""
    return datetime(2025, 11, 14, 3, 40, 0)


def _deterministic_id(seed_string: str, length: int = 16) -> str:
    """生成 Tau 使用的确定性十六进制 ID。"""
    return hashlib.sha256(seed_string.encode()).digest()[: length // 2].hex()


@overload
def query_db(
    db_name: str,
    db: BankingDBService,
    return_ids: Literal[True],
    limit: int | None = None,
    **constraints: object,
) -> list[tuple[str, dict[str, Any]]]: ...


@overload
def query_db(
    db_name: str,
    db: BankingDBService,
    return_ids: Literal[False] = False,
    limit: int | None = None,
    **constraints: object,
) -> list[dict[str, Any]]: ...


def query_db(
    db_name: str,
    db: BankingDBService,
    return_ids: bool = False,
    limit: int | None = None,
    **constraints: object,
) -> list[dict[str, Any]] | list[tuple[str, dict[str, Any]]]:
    """按 Tau 的插入顺序查询数据库。"""
    records = db.query_records(db_name, **constraints)
    if limit is not None:
        records = records[:limit]
    if return_ids:
        return records
    return [record for _, record in records]


def query_database_tool(
    database_name: str,
    constraints: str = "{}",
    db: BankingDBService | None = None,
) -> str:
    """使用 Tau 文本格式查询数据库。"""
    if db is None:
        return "Error: TransactionalDB instance required"
    try:
        parsed = json.loads(constraints) if constraints else {}
    except json.JSONDecodeError as error:
        return f"Error: Invalid JSON: {error}"
    if not isinstance(parsed, dict):
        return "Error: Constraints must be a JSON object"
    if not db.has_table(database_name):
        return f"Error: Database '{database_name}' not found."
    records = db.query_records(database_name, **parsed)
    if not records:
        return f"No records found in '{database_name}'."
    lines = [f"Found {len(records)} record(s) in '{database_name}':\n"]
    for index, (record_id, record) in enumerate(records, 1):
        lines.append(f"{index}. Record ID: {record_id}")
        lines.extend(f"   {field}: {value}" for field, value in record.items())
        lines.append("")
    return "\n".join(lines)


def add_to_db(
    db_name: str,
    record_id: str,
    record: dict[str, Any],
    db: BankingDBService,
) -> bool:
    """按 Tau 语义插入不存在的记录。"""
    if not db.has_table(db_name):
        return False
    return db.add_record(db_name, record_id, record)


def update_record_in_db(
    db_name: str,
    db: BankingDBService,
    record_id: str,
    updates: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """按 Tau 语义更新一条记录。"""
    if not db.update_record(db_name, record_id, updates):
        return False, None
    return True, db.get_record(db_name, record_id)


def generate_dispute_id(user_id: str, transaction_id: str) -> str:
    """生成争议 ID。"""
    return f"dsp_{_deterministic_id(f'dispute:{user_id}:{transaction_id}', 12)}"


def generate_agent_discoverable_tool_id(tool_name: str) -> str:
    """生成 Agent discoverable 工具调用记录 ID。"""
    return _deterministic_id(f"agent_discoverable_tool:{tool_name}")


def generate_credit_card_order_id(
    credit_card_account_id: str,
    user_id: str,
    reason: str,
) -> str:
    """生成信用卡订单 ID。"""
    seed = f"credit_card_order:{credit_card_account_id}:{user_id}:{reason}"
    return f"ccord_{_deterministic_id(seed, 12)}"


def generate_closure_reason_id(
    credit_card_account_id: str,
    user_id: str,
) -> str:
    """生成销户原因 ID。"""
    seed = f"closure_reason:{credit_card_account_id}:{user_id}"
    return f"clsr_{_deterministic_id(seed, 12)}"


def generate_account_flag_id(
    credit_card_account_id: str,
    flag_type: str,
    expiration_date: str,
) -> str:
    """生成信用卡标记 ID。"""
    seed = f"account_flag:{credit_card_account_id}:{flag_type}:{expiration_date}"
    return f"ccflag_{_deterministic_id(seed, 12)}"


def generate_credit_limit_increase_request_id(
    credit_card_account_id: str,
    user_id: str,
    requested_increase_amount: float,
) -> str:
    """生成提额请求 ID。"""
    seed = (
        f"cli_request:{credit_card_account_id}:{user_id}:"
        f"{requested_increase_amount:.2f}"
    )
    return f"cli_{_deterministic_id(seed, 12)}"


def generate_debit_card_order_id(
    account_id: str,
    user_id: str,
    delivery_option: str,
) -> str:
    """生成借记卡订单 ID。"""
    seed = f"debit_card_order:{account_id}:{user_id}:{delivery_option}"
    return f"dcord_{_deterministic_id(seed, 12)}"


def generate_debit_card_id(account_id: str, user_id: str, issue_date: str) -> str:
    """生成借记卡 ID。"""
    seed = f"debit_card:{account_id}:{user_id}:{issue_date}"
    return f"dbc_{_deterministic_id(seed, 12)}"


def generate_transaction_id(
    user_id: str,
    credit_card_type: str,
    merchant_name: str,
    amount: float,
    category: str,
    date: str | None = None,
) -> str:
    """生成信用卡交易 ID。"""
    parts = [
        "transaction",
        user_id,
        credit_card_type,
        merchant_name,
        f"{amount:.2f}",
        category,
    ]
    if date:
        parts.append(date)
    return f"txn_{_deterministic_id(':'.join(parts), 12)}"
