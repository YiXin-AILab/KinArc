"""由 Agent 授予后、模拟用户亲自执行的 discoverable 工具。"""

import hashlib
import json
from typing import TYPE_CHECKING

from application.banking_agent.services.discoverable_account_tools import (
    _TODAY,
    _deterministic_id,
    _parse_balance,
)
from application.banking_agent.services.discoverable_registry import (
    DiscoverableToolKind,
    build_discoverable_registry,
    discoverable_tool,
)

if TYPE_CHECKING:
    from application.banking_agent.services.banking_db_service import BankingDBService


class UserDiscoverableTools:
    """实现用户侧奖励争议、推荐、卡信息查询和支票存款。"""

    def __init__(self, database: "BankingDBService") -> None:
        self._database = database

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def submit_cash_back_dispute_0589(self, user_id: str, transaction_id: str) -> str:
        """提交指定信用卡交易的现金返还争议。"""
        dispute_id = "dsp_" + _deterministic_id(
            f"dispute:{user_id}:{transaction_id}", 12
        )
        record = {
            "dispute_id": dispute_id,
            "user_id": user_id,
            "transaction_id": transaction_id,
            "submitted_at": _TODAY,
            "status": "SUBMITTED",
        }
        created = self._database.add_record("cash_back_disputes", dispute_id, record)
        suffix = (
            f"Dispute ID: {dispute_id}\n"
            "Status: SUBMITTED - Your dispute has been queued for review."
            if created
            else "Note: Dispute may have already been submitted for this transaction."
        )
        return (
            "Cash back dispute submitted successfully. Your case has been queued "
            "for review.\n\n"
            f"Executed: submit_cash_back_dispute_0589\n{suffix}"
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def get_referral_link(self, user_id: str, card_name: str) -> str:
        """生成指定信用卡产品的客户推荐链接。"""
        referral_id = _deterministic_id(f"referral_link:{user_id}:{card_name}:{_TODAY}")
        self._database.add_record(
            "referrals",
            referral_id,
            {
                "referral_id": referral_id,
                "referrer_id": user_id,
                "referred_account_type": card_name,
                "referral_status": "NO_PROGRESS",
                "date": _TODAY,
            },
        )
        return (
            "Referral link generated successfully. Share this link with the person "
            "you want to refer.\n\n"
            f"Referral ID: {referral_id}\n"
            f"Referral link: https://rhobank.com/refer/{referral_id}"
        )

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_card_last_4_digits(self, credit_card_account_id: str) -> str:
        """确定性地查询信用卡号后四位。"""
        account = self._database.get_record(
            "credit_card_accounts", credit_card_account_id
        )
        if account is None:
            return f"Error: Credit card account '{credit_card_account_id}' not found."
        digest = hashlib.sha256(
            f"card_last4:{credit_card_account_id}".encode()
        ).hexdigest()
        last_four = "".join(char for char in digest if char.isdigit())[:4].ljust(4, "0")
        return (
            "Card information retrieved successfully.\n\n"
            "Executed: get_card_last_4_digits\n"
            f"Last 4 digits of card: {last_four}"
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def deposit_check_3847(self, account_id: str, check_amount: float) -> str:
        """将移动支票存入开放的 checking 或 savings 账户。"""
        if check_amount <= 0:
            return "Error: Check amount must be positive."
        with self._database.transaction() as tables:
            account = tables.get("accounts", {}).get(account_id)
            if account is None:
                return f"Error: Account '{account_id}' not found."
            if account.get("status") not in {"ACTIVE", "OPEN"}:
                return f"Error: Account '{account_id}' is not active."
            previous = _parse_balance(
                account.get("current_holdings", account.get("balance"))
            )
            current = previous + check_amount
            account["current_holdings"] = f"${current:.2f}"
        arguments = json.dumps(
            {"account_id": account_id, "check_amount": check_amount}, indent=2
        )
        return (
            "Check deposited successfully. Funds will be available according to "
            "your account's deposit policy.\n\n"
            "Executed: deposit_check_3847\n"
            f"Arguments: {arguments}\n"
            f"  - Previous Balance: ${previous:.2f}\n"
            f"  - New Balance: ${current:.2f}"
        )


USER_DISCOVERABLE_REGISTRY = build_discoverable_registry(UserDiscoverableTools)
