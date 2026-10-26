"""借记卡领域的 Agent discoverable 工具。"""

from typing import TYPE_CHECKING

from application.banking_golden_agent.services.discoverable_account_tools import (
    _TODAY,
    _deterministic_id,
    _format_records,
)
from application.banking_golden_agent.services.discoverable_registry import (
    DiscoverableToolKind,
    discoverable_tool,
)

if TYPE_CHECKING:
    from application.banking_golden_agent.services.banking_db_service import (
        BankingDBService,
    )


class DebitCardDiscoverableToolsMixin:
    """实现借记卡争议、下单、激活和安全控制。"""

    _database: "BankingDBService"

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def file_debit_card_transaction_dispute_6281(
        self,
        transaction_id: str,
        account_id: str,
        card_id: str,
        user_id: str,
        dispute_category: str,
        transaction_date: str,
        discovery_date: str,
        disputed_amount: float,
        transaction_type: str,
        card_in_possession: bool,
        pin_compromised: str,
        contacted_merchant: bool,
        police_report_filed: bool,
        written_statement_provided: bool,
        provisional_credit_eligible: bool,
        customer_max_liability_amount: float,
        card_action: str,
    ) -> str:
        """为已存在借记卡交易创建 Regulation E 争议。"""
        transaction = self._database.get_record(
            "bank_account_transaction_history", transaction_id
        )
        if transaction is None:
            return f"Error: Transaction '{transaction_id}' not found."
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("account_id") != account_id or card.get("user_id") != user_id:
            return "Error: Debit card does not match the specified account and user."
        dispute_id = "ddsp_" + _deterministic_id(
            f"debit_dispute:{user_id}:{transaction_id}", 12
        )
        record = {
            "dispute_id": dispute_id,
            "transaction_id": transaction_id,
            "account_id": account_id,
            "card_id": card_id,
            "user_id": user_id,
            "dispute_category": dispute_category,
            "transaction_date": transaction_date,
            "discovery_date": discovery_date,
            "disputed_amount": disputed_amount,
            "transaction_type": transaction_type,
            "card_in_possession": card_in_possession,
            "pin_compromised": pin_compromised,
            "contacted_merchant": contacted_merchant,
            "police_report_filed": police_report_filed,
            "written_statement_provided": written_statement_provided,
            "provisional_credit_eligible": provisional_credit_eligible,
            "customer_max_liability_amount": customer_max_liability_amount,
            "card_action": card_action,
            "submitted_at": _TODAY,
            "status": "UNDER_REVIEW",
        }
        if not self._database.add_record("debit_card_disputes", dispute_id, record):
            return (
                f"Error: A dispute already exists for transaction '{transaction_id}'."
            )
        if card_action in {"freeze_pending_investigation", "close_and_replace"}:
            self._database.update_record("debit_cards", card_id, {"status": "FROZEN"})
        return "Debit card transaction dispute filed successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def order_debit_card_5739(
        self,
        account_id: str,
        user_id: str,
        delivery_option: str,
        delivery_fee: float,
        card_design: str,
        design_fee: float,
        shipping_address: str,
        excess_replacement_fee: float | None = None,
    ) -> str:
        """为客户 checking 账户创建借记卡订单。"""
        account = self._database.get_record("accounts", account_id)
        if account is None:
            return f"Error: Account '{account_id}' not found."
        if account.get("user_id") != user_id:
            return "Error: Account does not belong to the specified user."
        account_class = str(
            account.get("class", account.get("account_type", ""))
        ).lower()
        if account_class not in {"checking", "business_checking"}:
            return "Error: Debit cards can only be ordered for checking accounts."
        if delivery_fee < 0 or design_fee < 0 or (excess_replacement_fee or 0) < 0:
            return "Error: Fees cannot be negative."
        order_id = "dcord_" + _deterministic_id(
            f"debit_order:{account_id}:{card_design}:{_TODAY}", 12
        )
        card_id = "dbc_" + _deterministic_id(f"debit_card:{account_id}:{order_id}", 12)
        self._database.add_record(
            "debit_card_orders",
            order_id,
            {
                "order_id": order_id,
                "card_id": card_id,
                "account_id": account_id,
                "user_id": user_id,
                "delivery_option": delivery_option,
                "delivery_fee": delivery_fee,
                "card_design": card_design,
                "design_fee": design_fee,
                "shipping_address": shipping_address,
                "excess_replacement_fee": excess_replacement_fee,
                "order_date": _TODAY,
                "status": "PENDING",
            },
        )
        return f"Debit card ordered successfully.\n  - Order ID: {order_id}"

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def activate_debit_card_8291(
        self,
        card_id: str,
        last_4_digits: str,
        expiration_date: str,
        cvv: str,
        pin: str,
    ) -> str:
        """激活首次签发的借记卡。"""
        return self._activate_card(
            card_id,
            last_4_digits,
            expiration_date,
            cvv,
            pin,
            allowed_reasons={"new_account"},
            label="New debit card",
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def activate_debit_card_8292(
        self,
        card_id: str,
        last_4_digits: str,
        expiration_date: str,
        cvv: str,
        pin: str,
    ) -> str:
        """激活遗失、盗刷或损坏后的 replacement 借记卡。"""
        return self._activate_card(
            card_id,
            last_4_digits,
            expiration_date,
            cvv,
            pin,
            allowed_reasons={"lost", "stolen", "damaged", "fraud"},
            label="Replacement debit card",
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def activate_debit_card_8293(
        self,
        card_id: str,
        last_4_digits: str,
        expiration_date: str,
        cvv: str,
        pin: str,
    ) -> str:
        """激活到期续发的借记卡。"""
        return self._activate_card(
            card_id,
            last_4_digits,
            expiration_date,
            cvv,
            pin,
            allowed_reasons={"expired", "expiring"},
            label="Reissued debit card",
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def close_debit_card_4721(self, card_id: str, reason: str) -> str:
        """永久关闭借记卡。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("status") == "CLOSED":
            return f"Error: Debit card '{card_id}' is already closed."
        self._database.update_record(
            "debit_cards",
            card_id,
            {"status": "CLOSED", "closure_reason": reason, "date_closed": _TODAY},
        )
        return "Debit card closed successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def freeze_debit_card_3892(self, card_id: str) -> str:
        """临时冻结活跃借记卡。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("status") != "ACTIVE":
            return "Error: Only an active debit card can be frozen."
        self._database.update_record("debit_cards", card_id, {"status": "FROZEN"})
        return "Debit card frozen successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def unfreeze_debit_card_3893(self, card_id: str) -> str:
        """解除借记卡临时冻结。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("status") != "FROZEN":
            return "Error: Only a frozen debit card can be unfrozen."
        self._database.update_record("debit_cards", card_id, {"status": "ACTIVE"})
        return "Debit card unfrozen successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def clear_debit_card_fraud_alert_4892(self, card_id: str, reason: str) -> str:
        """清除已核验借记卡上的 fraud/velocity 标记。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if reason not in {"customer_verified", "legitimate_activity_confirmed"}:
            return "Error: Invalid reason for clearing fraud alert."
        self._database.update_record(
            "debit_cards",
            card_id,
            {"fraud_alert": False, "velocity_block": False, "status": "ACTIVE"},
        )
        return "Fraud alert/velocity block cleared successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def reset_debit_card_pin_6284(
        self, card_id: str, last_4_digits: str, new_pin: str
    ) -> str:
        """通过卡号后四位校验后重置借记卡 PIN。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if str(card.get("last_4_digits")) != last_4_digits:
            return "Error: Card verification failed."
        pin_error = _validate_pin(new_pin)
        if pin_error:
            return f"Error: {pin_error}"
        self._database.update_record("debit_cards", card_id, {"pin": new_pin})
        return "Debit card PIN reset successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def change_debit_card_pin_6285(
        self, card_id: str, current_pin: str, new_pin: str
    ) -> str:
        """在当前 PIN 校验通过后修改 PIN。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("pin") != current_pin:
            return "Error: Current PIN is incorrect."
        pin_error = _validate_pin(new_pin)
        if pin_error:
            return f"Error: {pin_error}"
        self._database.update_record("debit_cards", card_id, {"pin": new_pin})
        return "Debit card PIN changed successfully."

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_debit_cards_by_account_id_7823(self, account_id: str) -> str:
        """查询银行账户关联的全部借记卡。"""
        records = self._database.query_records("debit_cards", account_id=account_id)
        return "Debit cards retrieved successfully.\n\n" + _format_records(
            "debit_cards", records
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def request_temporary_debit_card_limit_increase_8374(
        self, card_id: str, limit_type: str, new_limit: int
    ) -> str:
        """设置借记卡的临时 ATM 或消费限额。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("status") != "ACTIVE":
            return "Error: Debit card must be active."
        field_by_type = {
            "daily_purchase": "temporary_daily_purchase_limit",
            "daily_atm": "temporary_daily_atm_limit",
            "atm_withdrawal": "temporary_daily_atm_limit",
        }
        field = field_by_type.get(limit_type)
        if field is None:
            return "Error: Invalid limit_type."
        if new_limit <= 0 or new_limit > 10000:
            return "Error: Requested limit is outside the allowed range."
        self._database.update_record(
            "debit_cards",
            card_id,
            {field: new_limit, "temporary_limit_expiration": "11/15/2025"},
        )
        return "Temporary debit card limit increase approved."

    def _activate_card(
        self,
        card_id: str,
        last_4_digits: str,
        expiration_date: str,
        cvv: str,
        pin: str,
        *,
        allowed_reasons: set[str],
        label: str,
    ) -> str:
        """执行三类激活工具共享的严格校验和状态更新。"""
        card = self._database.get_record("debit_cards", card_id)
        if card is None:
            return f"Error: Debit card '{card_id}' not found."
        if card.get("status") != "PENDING":
            return "Error: Debit card is not pending activation."
        if str(card.get("issue_reason", "")).lower() not in allowed_reasons:
            return "Error: This activation tool does not match the card issue reason."
        if (
            str(card.get("last_4_digits")) != last_4_digits
            or str(card.get("expiration_date")) != expiration_date
            or str(card.get("cvv")) != cvv
        ):
            return "Error: Card verification details do not match."
        pin_error = _validate_pin(pin)
        if pin_error:
            return f"Error: {pin_error}"
        self._database.update_record(
            "debit_cards",
            card_id,
            {"status": "ACTIVE", "pin": pin, "activation_date": _TODAY},
        )
        return f"{label} activated successfully."


def _validate_pin(pin: str) -> str | None:
    """执行上游场景要求的 PIN 强度校验。"""
    if len(pin) != 4 or not pin.isdigit():
        return "PIN must be exactly 4 digits."
    sequential = {
        "0123",
        "1234",
        "2345",
        "3456",
        "4567",
        "5678",
        "6789",
        "9876",
        "8765",
        "7654",
        "6543",
        "5432",
        "4321",
        "3210",
    }
    if pin in sequential:
        return "PIN cannot be sequential."
    if len(set(pin)) == 1:
        return "PIN cannot use the same digit four times."
    return None
