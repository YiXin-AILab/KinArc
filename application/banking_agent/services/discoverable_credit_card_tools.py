"""信用卡、争议和人工升级领域的 Agent discoverable 工具。"""

from typing import TYPE_CHECKING

from application.banking_agent.services.discoverable_account_tools import (
    _TODAY,
    _deterministic_id,
    _format_records,
    _parse_balance,
)
from application.banking_agent.services.discoverable_registry import (
    DiscoverableToolKind,
    discoverable_tool,
)

if TYPE_CHECKING:
    from application.banking_agent.services.banking_db_service import BankingDBService


class CreditCardDiscoverableToolsMixin:
    """实现信用卡账务、争议、换卡与额度流程。"""

    _database: "BankingDBService"

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def update_transaction_rewards_3847(
        self, transaction_id: str, new_rewards_earned: str
    ) -> str:
        """修正一笔信用卡交易的奖励值。"""
        record = self._database.update_record(
            "credit_card_transaction_history",
            transaction_id,
            {"rewards_earned": new_rewards_earned},
        )
        if record is None:
            return f"Error: Transaction '{transaction_id}' not found."
        return "Transaction rewards updated successfully."

    @discoverable_tool(DiscoverableToolKind.GENERIC)
    def initial_transfer_to_human_agent_0218(self) -> str:
        """按 02/18 事件流程升级人工。"""
        return self._transfer("02/18 banking incident")

    @discoverable_tool(DiscoverableToolKind.GENERIC)
    def initial_transfer_to_human_agent_1822(self) -> str:
        """按 18/22 事件流程升级人工。"""
        return self._transfer("18/22 banking incident")

    @discoverable_tool(DiscoverableToolKind.GENERIC)
    def emergency_credit_bureau_incident_transfer_1114(self) -> str:
        """按 11/14 征信事件流程紧急升级人工。"""
        return self._transfer("11/14 credit bureau incident")

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def file_credit_card_transaction_dispute_4829(
        self,
        transaction_id: str,
        card_action: str,
        card_last_4_digits: str,
        full_name: str,
        user_id: str,
        phone: str,
        email: str,
        address: str,
        contacted_merchant: bool,
        purchase_date: str,
        issue_noticed_date: str,
        dispute_reason: str,
        resolution_requested: str,
        eligible_for_provisional_credit: bool,
        partial_refund_amount: float | None = None,
    ) -> str:
        """为已存在信用卡交易创建正式争议。"""
        transaction = self._database.get_record(
            "credit_card_transaction_history", transaction_id
        )
        if transaction is None:
            return f"Error: Transaction '{transaction_id}' not found."
        if transaction.get("user_id") != user_id:
            return "Error: Transaction does not belong to the specified user."
        dispute_id = "dsp_" + _deterministic_id(
            f"dispute:{user_id}:{transaction_id}", 12
        )
        record = {
            "dispute_id": dispute_id,
            "transaction_id": transaction_id,
            "user_id": user_id,
            "card_action": card_action,
            "card_last_4_digits": card_last_4_digits,
            "full_name": full_name,
            "phone": phone,
            "email": email,
            "address": address,
            "contacted_merchant": contacted_merchant,
            "purchase_date": purchase_date,
            "issue_noticed_date": issue_noticed_date,
            "dispute_reason": dispute_reason,
            "resolution_requested": resolution_requested,
            "partial_refund_amount": partial_refund_amount,
            "eligible_for_provisional_credit": eligible_for_provisional_credit,
            "submitted_at": _TODAY,
            "status": "UNDER_REVIEW",
        }
        if not self._database.add_record("transaction_disputes", dispute_id, record):
            return (
                f"Error: A dispute already exists for transaction '{transaction_id}'."
            )
        return (
            "Credit card transaction dispute filed successfully.\n"
            f"  - Dispute ID: {dispute_id}"
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def set_debit_card_recurring_block_7382(
        self, card_id: str, block_recurring: bool
    ) -> str:
        """启用或关闭借记卡 recurring merchant block。"""
        updated = self._database.update_record(
            "debit_cards", card_id, {"block_recurring": block_recurring}
        )
        if updated is None:
            return f"Error: Debit card '{card_id}' not found."
        return "Debit card recurring block updated successfully."

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_debit_dispute_status_7483(self, user_id: str) -> str:
        """查询客户的借记卡争议状态。"""
        records = self._database.query_records("debit_card_disputes", user_id=user_id)
        return "Debit dispute status retrieved successfully.\n\n" + _format_records(
            "debit_card_disputes", records
        )

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_atm_deposit_images_8473(self, transaction_id: str) -> str:
        """获取 ATM 存款记录中的图像元数据。"""
        record = self._database.get_record(
            "bank_account_transaction_history", transaction_id
        )
        if record is None:
            return f"Error: Transaction '{transaction_id}' not found."
        if "deposit" not in str(record.get("type", "")).lower():
            return "Error: Transaction is not an ATM deposit."
        image_fields = {
            key: value
            for key, value in record.items()
            if "image" in key.lower() or key in {"transaction_id", "amount", "date"}
        }
        return f"ATM deposit images retrieved successfully.\n{image_fields}"

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def order_replacement_credit_card_7291(
        self,
        credit_card_account_id: str,
        user_id: str,
        shipping_address: str,
        reason: str,
        expedited_shipping: bool = False,
    ) -> str:
        """为客户信用卡账户创建换卡订单。"""
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        pending = self._database.query_records(
            "credit_card_orders",
            credit_card_account_id=credit_card_account_id,
            status="PENDING",
        )
        if pending:
            return "Error: A pending replacement order already exists."
        order_id = "ccord_" + _deterministic_id(
            f"credit_card_order:{credit_card_account_id}:{reason}:{_TODAY}", 12
        )
        self._database.add_record(
            "credit_card_orders",
            order_id,
            {
                "order_id": order_id,
                "credit_card_account_id": credit_card_account_id,
                "user_id": user_id,
                "shipping_address": shipping_address,
                "reason": reason,
                "expedited_shipping": expedited_shipping,
                "order_date": _TODAY,
                "status": "PENDING",
            },
        )
        return (
            f"Replacement credit card ordered successfully.\n  - Order ID: {order_id}"
        )

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_user_dispute_history_7291(self, user_id: str) -> str:
        """查询客户的信用卡及借记卡争议历史。"""
        credit = self._database.query_records("transaction_disputes", user_id=user_id)
        debit = self._database.query_records("debit_card_disputes", user_id=user_id)
        return (
            "User dispute history retrieved successfully.\n\n"
            + _format_records("transaction_disputes", credit)
            + "\n\n"
            + _format_records("debit_card_disputes", debit)
        )

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_pending_replacement_orders_5765(self, credit_card_account_id: str) -> str:
        """查询信用卡账户的未完成换卡订单。"""
        records = [
            item
            for item in self._database.query_records(
                "credit_card_orders",
                credit_card_account_id=credit_card_account_id,
            )
            if item[1].get("status") not in {"DELIVERED", "CANCELLED"}
        ]
        return (
            "Pending replacement orders retrieved successfully.\n\n"
            + _format_records("credit_card_orders", records)
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def log_credit_card_closure_reason_4521(
        self, credit_card_account_id: str, user_id: str, closure_reason: str
    ) -> str:
        """记录客户关闭信用卡的原因。"""
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        record_id = "clsr_" + _deterministic_id(
            f"closure:{credit_card_account_id}:{closure_reason}:{_TODAY}", 12
        )
        self._database.add_record(
            "credit_card_closure_reasons",
            record_id,
            {
                "record_id": record_id,
                "credit_card_account_id": credit_card_account_id,
                "user_id": user_id,
                "closure_reason": closure_reason,
                "logged_at": _TODAY,
                "status": "LOGGED",
            },
        )
        return "Credit card closure reason logged successfully."

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_closure_reason_history_8293(self, credit_card_account_id: str) -> str:
        """查询信用卡账户的关闭原因记录。"""
        records = self._database.query_records(
            "credit_card_closure_reasons",
            credit_card_account_id=credit_card_account_id,
        )
        return "Closure reason history retrieved successfully.\n\n" + _format_records(
            "credit_card_closure_reasons", records
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def apply_statement_credit_8472(
        self,
        user_id: str,
        credit_card_account_id: str,
        amount: float,
        reason: str,
    ) -> str:
        """向信用卡账户发放 statement credit。"""
        if amount <= 0:
            return "Error: Credit amount must be positive."
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        with self._database.transaction() as tables:
            account = tables["credit_card_accounts"][credit_card_account_id]
            previous = _parse_balance(account.get("current_balance"))
            current = max(0.0, previous - amount)
            account["current_balance"] = f"${current:.2f}"
            transaction_id = "txn_" + _deterministic_id(
                f"statement_credit:{credit_card_account_id}:{amount}:{reason}:{_TODAY}",
                12,
            )
            tables.setdefault("credit_card_transaction_history", {})[transaction_id] = {
                "transaction_id": transaction_id,
                "user_id": user_id,
                "credit_card_account_id": credit_card_account_id,
                "transaction_amount": f"-${amount:.2f}",
                "transaction_date": _TODAY,
                "category": "Statement Credit",
                "description": reason,
                "status": "COMPLETED",
            }
        return "Statement credit applied successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def apply_credit_card_account_flag_6147(
        self,
        credit_card_account_id: str,
        user_id: str,
        flag_type: str,
        expiration_date: str,
        reason: str,
    ) -> str:
        """为信用卡账户添加有期限的服务标记。"""
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        flag_id = "flag_" + _deterministic_id(
            f"flag:{credit_card_account_id}:{flag_type}:{expiration_date}", 12
        )
        self._database.add_record(
            "credit_card_account_flags",
            flag_id,
            {
                "flag_id": flag_id,
                "credit_card_account_id": credit_card_account_id,
                "user_id": user_id,
                "flag_type": flag_type,
                "expiration_date": expiration_date,
                "reason": reason,
                "status": "ACTIVE",
            },
        )
        return "Account flag applied successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def close_credit_card_account_7834(
        self, credit_card_account_id: str, user_id: str
    ) -> str:
        """关闭余额为零的信用卡账户。"""
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        account = self._database.get_record(
            "credit_card_accounts", credit_card_account_id
        )
        if account is None:
            return f"Error: Credit card account '{credit_card_account_id}' not found."
        balance = _parse_balance(account.get("current_balance"))
        if balance != 0:
            return (
                f"Error: Account balance must be $0.00 before closing: ${balance:.2f}."
            )
        self._database.update_record(
            "credit_card_accounts",
            credit_card_account_id,
            {"status": "CLOSED", "date_closed": _TODAY},
        )
        return "Credit card account closed successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def pay_credit_card_from_checking_9182(
        self,
        user_id: str,
        checking_account_id: str,
        credit_card_account_id: str,
        amount: float,
    ) -> str:
        """从同一客户的 checking 账户偿还信用卡。"""
        if amount <= 0:
            return "Error: Payment amount must be positive."
        with self._database.transaction() as tables:
            checking = tables.get("accounts", {}).get(checking_account_id)
            credit = tables.get("credit_card_accounts", {}).get(credit_card_account_id)
            if checking is None:
                return f"Error: Checking account '{checking_account_id}' not found."
            if credit is None:
                return (
                    f"Error: Credit card account '{credit_card_account_id}' not found."
                )
            if checking.get("user_id") != user_id or credit.get("user_id") != user_id:
                return "Error: Both accounts must belong to the specified user."
            checking_balance = _parse_balance(checking.get("current_holdings"))
            if checking_balance < amount:
                return "Error: Insufficient funds in checking account."
            credit_balance = _parse_balance(credit.get("current_balance"))
            checking["current_holdings"] = f"${checking_balance - amount:.2f}"
            credit["current_balance"] = f"${max(0.0, credit_balance - amount):.2f}"
            payment_id = "pay_" + _deterministic_id(
                f"payment:{credit_card_account_id}:{amount}:{_TODAY}", 12
            )
            tables.setdefault("payment_history", {})[payment_id] = {
                "payment_id": payment_id,
                "credit_card_account_id": credit_card_account_id,
                "user_id": user_id,
                "checking_account_id": checking_account_id,
                "payment_date": _TODAY,
                "amount": f"${amount:.2f}",
                "status": "ON_TIME",
            }
        return "Credit card payment completed successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def submit_credit_limit_increase_request_7392(
        self,
        credit_card_account_id: str,
        user_id: str,
        requested_increase_amount: int,
    ) -> str:
        """提交信用额度提升申请。"""
        if requested_increase_amount <= 0:
            return "Error: Requested increase amount must be positive."
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        request_id = "cli_" + _deterministic_id(
            f"cli:{credit_card_account_id}:{requested_increase_amount}:{_TODAY}", 12
        )
        self._database.add_record(
            "credit_limit_increase_requests",
            request_id,
            {
                "request_id": request_id,
                "credit_card_account_id": credit_card_account_id,
                "user_id": user_id,
                "requested_increase_amount": requested_increase_amount,
                "request_date": _TODAY,
                "status": "PENDING",
            },
        )
        return "Credit limit increase request submitted successfully."

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_credit_limit_increase_history_4829(
        self, credit_card_account_id: str
    ) -> str:
        """查询信用额度调整历史。"""
        records = self._database.query_records(
            "credit_limit_increase_requests",
            credit_card_account_id=credit_card_account_id,
        )
        return (
            "Credit limit increase history retrieved successfully.\n\n"
            + _format_records("credit_limit_increase_requests", records)
        )

    @discoverable_tool(DiscoverableToolKind.READ)
    def get_payment_history_6183(self, credit_card_account_id: str, months: int) -> str:
        """查询最近若干月的信用卡还款记录。"""
        if months <= 0:
            return "Error: months must be positive."
        records = self._database.query_records(
            "payment_history", credit_card_account_id=credit_card_account_id
        )
        return "Payment history retrieved successfully.\n\n" + _format_records(
            "payment_history", records[:months]
        )

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def approve_credit_limit_increase_5847(
        self,
        credit_card_account_id: str,
        user_id: str,
        new_credit_limit: int,
    ) -> str:
        """批准信用额度提升并更新账户。"""
        if new_credit_limit <= 0:
            return "Error: New credit limit must be positive."
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        self._database.update_record(
            "credit_card_accounts",
            credit_card_account_id,
            {"credit_limit": new_credit_limit},
        )
        self._resolve_latest_limit_request(credit_card_account_id, "APPROVED")
        return "Credit limit increase approved and applied successfully."

    @discoverable_tool(DiscoverableToolKind.WRITE)
    def deny_credit_limit_increase_5848(
        self,
        credit_card_account_id: str,
        user_id: str,
        denial_reason: str,
    ) -> str:
        """拒绝信用额度提升申请。"""
        error = self._validate_cc_owner(credit_card_account_id, user_id)
        if error:
            return error
        self._resolve_latest_limit_request(
            credit_card_account_id, "DENIED", denial_reason=denial_reason
        )
        return "Credit limit increase request denied."

    def _validate_cc_owner(self, account_id: str, user_id: str) -> str | None:
        """校验信用卡账户存在且属于指定客户。"""
        account = self._database.get_record("credit_card_accounts", account_id)
        if account is None:
            return f"Error: Credit card account '{account_id}' not found."
        if account.get("user_id") != user_id:
            return "Error: Credit card account does not belong to the specified user."
        return None

    def _resolve_latest_limit_request(
        self, account_id: str, status: str, **updates: str
    ) -> None:
        """更新同一账户最后一条未处理额度申请。"""
        requests = self._database.query_records(
            "credit_limit_increase_requests",
            credit_card_account_id=account_id,
        )
        pending = [item for item in requests if item[1].get("status") == "PENDING"]
        if pending:
            self._database.update_record(
                "credit_limit_increase_requests",
                pending[-1][0],
                {"status": status, "decision_date": _TODAY, **updates},
            )

    def _transfer(self, incident: str) -> str:
        """记录专项事件人工升级。"""
        return self._database.transfer_to_human_agents(
            summary=incident,
            reason="specialized_department_required",
        )
