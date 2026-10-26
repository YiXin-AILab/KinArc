"""银行账户领域的 Agent discoverable 工具。"""

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

from application.banking_golden_agent.services.discoverable_registry import (
    DiscoverableToolKind,
    discoverable_tool,
)

if TYPE_CHECKING:
    from application.banking_golden_agent.services.banking_db_service import (
        BankingDBService,
    )

_TODAY = "11/14/2025"


class BankAccountDiscoverableToolsMixin:
    """实现银行账户的查询和变更动作。"""

    _database: "BankingDBService"

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def open_bank_account_4821(
        self,
        user_id: str,
        account_type: str,
        account_class: str,
    ) -> str:
        """为已存在客户开立银行账户。"""
        valid_types = {
            "checking",
            "savings",
            "business_checking",
            "business_savings",
        }
        if account_type not in valid_types:
            return f"Error: Invalid account_type. Must be one of: {sorted(valid_types)}"
        if self._database.get_record("users", user_id) is None:
            return f"Error: User '{user_id}' not found."

        account_id = _deterministic_id(
            f"account:{user_id}:{account_type}:{account_class}"
        )
        record = {
            "account_id": account_id,
            "user_id": user_id,
            "account_type": account_type,
            "account_class": account_class,
            "current_holdings": "0.00",
            "status": "OPEN",
            "date_opened": _TODAY,
        }
        if not self._database.add_record("accounts", account_id, record):
            return (
                f"Failed to open account: Account ID '{account_id}' may already exist."
            )
        return (
            "Bank account opened successfully!\n"
            f"  - Account ID: {account_id}\n"
            f"  - Account Type: {account_type}\n"
            f"  - Account Class: {account_class}\n"
            "  - Status: OPEN\n"
            "  - Initial Balance: $0.00"
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def close_bank_account_7392(
        self,
        account_id: str,
        reason: str = "Customer requested closure",
        waive_early_closure_fee: bool = False,
    ) -> str:
        """关闭余额为零的银行账户。"""
        del waive_early_closure_fee
        account = self._database.get_record("accounts", account_id)
        if account is None:
            return f"Error: Account '{account_id}' not found."
        if account.get("status") == "CLOSED":
            return f"Error: Account '{account_id}' is already closed."
        balance = _parse_balance(
            account.get("current_holdings", account.get("balance"))
        )
        if balance != 0:
            return (
                "Error: Account balance must be $0.00 before closing. "
                f"Current balance: ${balance:.2f}"
            )
        self._database.update_record(
            "accounts",
            account_id,
            {"status": "CLOSED", "date_closed": _TODAY, "closure_reason": reason},
        )
        return f"Bank account closed successfully!\n  - Account ID: {account_id}"

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def apply_checking_account_credit_5829(
        self,
        account_id: str,
        amount: float,
        credit_type: str,
    ) -> str:
        """向 checking 账户发放退款或返利。"""
        return self._apply_account_credit(
            account_id,
            amount,
            credit_type,
            expected_classes={"checking"},
            valid_types={"rebate_credit", "fee_refund"},
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def apply_savings_account_credit_6831(
        self,
        account_id: str,
        amount: float,
        credit_type: str,
    ) -> str:
        """向 savings 账户发放利息修正、退款或善意补偿。"""
        return self._apply_account_credit(
            account_id,
            amount,
            credit_type,
            expected_classes={"saving", "savings"},
            valid_types={"interest_correction", "fee_refund", "goodwill_credit"},
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def submit_interest_discrepancy_report_7294(
        self,
        account_id: str,
        user_id: str,
        expected_apy: float,
        actual_apy: float,
        amount_difference: float,
    ) -> str:
        """提交储蓄账户利息差异调查记录。"""
        account = self._database.get_record("accounts", account_id)
        if account is None:
            return f"Error: Account '{account_id}' not found."
        if self._database.get_record("users", user_id) is None:
            return f"Error: User '{user_id}' not found."
        report_id = "IDR_" + _deterministic_id(
            f"interest_report:{account_id}:{user_id}:{expected_apy}:"
            f"{actual_apy}:{_TODAY}"
        )
        record = {
            "report_id": report_id,
            "account_id": account_id,
            "user_id": user_id,
            "account_level": account.get("level", "Unknown"),
            "expected_apy": expected_apy,
            "actual_apy": actual_apy,
            "apy_difference": round(expected_apy - actual_apy, 4),
            "amount_difference": amount_difference,
            "submitted_date": _TODAY,
            "status": "PENDING_REVIEW",
        }
        self._database.add_record("interest_discrepancy_reports", report_id, record)
        return (
            "Interest Discrepancy Report submitted successfully!\n"
            f"  - Report ID: {report_id}"
        )

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_bank_account_transactions_9173(self, account_id: str) -> str:
        """按时间倒序查询银行账户流水。"""
        if self._database.get_record("accounts", account_id) is None:
            return f"Error: Account '{account_id}' not found."
        records = self._database.query_records(
            "bank_account_transaction_history", account_id=account_id
        )
        records.sort(key=lambda item: _transaction_date(item[1]), reverse=True)
        return (
            "Bank account transactions retrieved successfully.\n\n"
            + _format_records("bank_account_transaction_history", records)
        )

    def _apply_account_credit(
        self,
        account_id: str,
        amount: float,
        credit_type: str,
        *,
        expected_classes: set[str],
        valid_types: set[str],
    ) -> str:
        """校验账户并原子增加余额、写入对应流水。"""
        if amount <= 0:
            return "Error: Credit amount must be positive."
        if credit_type not in valid_types:
            return f"Error: Invalid credit_type. Must be one of: {sorted(valid_types)}"
        with self._database.transaction() as tables:
            account = tables.get("accounts", {}).get(account_id)
            if account is None:
                return f"Error: Account '{account_id}' not found."
            account_class = str(
                account.get("class", account.get("account_type", ""))
            ).lower()
            if account_class not in expected_classes:
                return (
                    f"Error: Account '{account_id}' has an incompatible account type."
                )
            if account.get("status") not in {"ACTIVE", "OPEN"}:
                return f"Error: Account '{account_id}' is not active."
            previous = _parse_balance(account.get("current_holdings"))
            current = previous + amount
            account["current_holdings"] = f"${current:.2f}"
            transaction_id = "txn_" + _deterministic_id(
                f"account_credit:{account_id}:{credit_type}:{amount}:{_TODAY}"
            )
            tables.setdefault("bank_account_transaction_history", {})[
                transaction_id
            ] = {
                "transaction_id": transaction_id,
                "account_id": account_id,
                "date": _TODAY,
                "description": credit_type.upper().replace("_", " "),
                "amount": amount,
                "type": credit_type,
                "status": "posted",
            }
        return (
            "Credit applied successfully!\n"
            f"  - Transaction ID: {transaction_id}\n"
            f"  - Previous Balance: ${previous:.2f}\n"
            f"  - New Balance: ${current:.2f}"
        )


def _deterministic_id(seed: str, length: int = 16) -> str:
    """生成与上游 fixture 兼容的稳定十六进制 ID。"""
    return hashlib.sha256(seed.encode()).digest()[: length // 2].hex()


def _parse_balance(value: Any) -> float:
    """解析数字或带货币符号的余额。"""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value.replace("$", "").replace(",", "").strip() or 0)
    return 0.0


def _transaction_date(record: dict[str, Any]) -> datetime:
    """将两种 fixture 日期格式转换为可排序值。"""
    date_text = str(record.get("date", ""))
    for date_format in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_text, date_format)
        except ValueError:
            continue
    return datetime.min


def _format_records(
    table_name: str,
    records: list[tuple[str, dict[str, Any]]],
) -> str:
    """格式化查询结果。"""
    if not records:
        return f"No records found in '{table_name}'."
    lines = [f"Found {len(records)} record(s) in '{table_name}':", ""]
    for index, (record_id, record) in enumerate(records, 1):
        lines.append(f"{index}. Record ID: {record_id}")
        lines.extend(f"   {field}: {value}" for field, value in record.items())
        lines.append("")
    return "\n".join(lines).strip()
