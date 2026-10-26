"""信用卡 discoverable 工具的业务行为测试。"""

from application.banking_agent.services.banking_db_service import BankingDBService


def _call(service: BankingDBService, tool_name: str, arguments: str) -> str:
    assert service.unlock_discoverable_agent_tool(tool_name).startswith(
        "Tool unlocked:"
    )
    return service.call_discoverable_agent_tool(tool_name, arguments)


def test_statement_credit_changes_balance_and_adds_transaction(
    banking_service: BankingDBService,
) -> None:
    result = _call(
        banking_service,
        "apply_statement_credit_8472",
        '{"user_id":"lj82d4f1a9",'
        '"credit_card_account_id":"cc_lj82d4f1a9_silver",'
        '"amount":42.15,"reason":"goodwill_adjustment"}',
    )
    assert "successfully" in result
    account = banking_service.get_record("credit_card_accounts", "cc_lj82d4f1a9_silver")
    assert account is not None
    assert account["current_balance"] == "$842.15"
    assert banking_service.query_records(
        "credit_card_transaction_history",
        user_id="lj82d4f1a9",
        category="Statement Credit",
    )


def test_credit_card_payment_moves_money_atomically(
    banking_service: BankingDBService,
) -> None:
    before_checking = banking_service.get_record("accounts", "03")
    before_credit = banking_service.get_record(
        "credit_card_accounts", "cc_lj82d4f1a9_silver"
    )
    # Account 03 and this card have different owners; ownership check must reject.
    result = _call(
        banking_service,
        "pay_credit_card_from_checking_9182",
        '{"user_id":"lj82d4f1a9","checking_account_id":"03",'
        '"credit_card_account_id":"cc_lj82d4f1a9_silver","amount":50.0}',
    )
    assert result.startswith("Error:")
    assert banking_service.get_record("accounts", "03") == before_checking
    assert (
        banking_service.get_record("credit_card_accounts", "cc_lj82d4f1a9_silver")
        == before_credit
    )


def test_credit_limit_request_can_be_approved(
    banking_service: BankingDBService,
) -> None:
    request = _call(
        banking_service,
        "submit_credit_limit_increase_request_7392",
        '{"credit_card_account_id":"cc_lj82d4f1a9_silver",'
        '"user_id":"lj82d4f1a9","requested_increase_amount":1000}',
    )
    assert "submitted successfully" in request
    approved = _call(
        banking_service,
        "approve_credit_limit_increase_5847",
        '{"credit_card_account_id":"cc_lj82d4f1a9_silver",'
        '"user_id":"lj82d4f1a9","new_credit_limit":5000}',
    )
    assert "approved" in approved.lower()
    account = banking_service.get_record("credit_card_accounts", "cc_lj82d4f1a9_silver")
    assert account is not None
    assert account["credit_limit"] == "$5000.00"
    history = banking_service.query_records(
        "credit_limit_increase_requests",
        credit_card_account_id="cc_lj82d4f1a9_silver",
    )
    assert history[-1][1]["status"] == "APPROVED"


def test_file_credit_card_dispute_records_the_supplied_transaction_reference(
    banking_service: BankingDBService,
) -> None:
    result = _call(
        banking_service,
        "file_credit_card_transaction_dispute_4829",
        '{"transaction_id":"missing","card_action":"keep_active",'
        '"card_last_4_digits":"1234","full_name":"Mia Smith",'
        '"user_id":"123","phone":"1","email":"a@example.com",'
        '"address":"x","contacted_merchant":true,'
        '"purchase_date":"11/01/2025","issue_noticed_date":"11/02/2025",'
        '"dispute_reason":"duplicate_charge",'
        '"resolution_requested":"full_refund",'
        '"eligible_for_provisional_credit":false}',
    )
    assert result.startswith("Credit card transaction dispute filed successfully")
    disputes = banking_service.query_records(
        "transaction_disputes",
        transaction_id="missing",
    )
    assert len(disputes) == 1


def test_credit_card_dispute_uses_tau_submitted_state(
    banking_service: BankingDBService,
) -> None:
    """争议记录字段必须与 Tau 的正式状态转换一致。"""
    result = _call(
        banking_service,
        "file_credit_card_transaction_dispute_4829",
        '{"transaction_id":"txn_lj82d4_001","card_action":"keep_active",'
        '"card_last_4_digits":"1234","full_name":"Mia Smith",'
        '"user_id":"lj82d4f1a9","phone":"1","email":"a@example.com",'
        '"address":"x","contacted_merchant":true,'
        '"purchase_date":"11/01/2025","issue_noticed_date":"11/02/2025",'
        '"dispute_reason":"duplicate_charge","resolution_requested":"full_refund",'
        '"eligible_for_provisional_credit":false}',
    )

    assert result.startswith("Credit card transaction dispute filed successfully")
    disputes = banking_service.query_records(
        "transaction_disputes",
        transaction_id="txn_lj82d4_001",
    )
    assert disputes[0][1]["status"] == "SUBMITTED"
    assert disputes[0][1]["provisional_credit_given"] is False


def test_credit_limit_approval_rejects_pending_dispute(
    banking_service: BankingDBService,
) -> None:
    """Tau 不允许存在 pending dispute 的客户获批提额。"""
    banking_service.add_record(
        "transaction_disputes",
        "pending",
        {"user_id": "lj82d4f1a9", "status": "SUBMITTED"},
    )

    result = _call(
        banking_service,
        "approve_credit_limit_increase_5847",
        '{"credit_card_account_id":"cc_lj82d4f1a9_silver",'
        '"user_id":"lj82d4f1a9","new_credit_limit":5000}',
    )

    assert result == (
        "Error: Credit limit increase request cannot be approved at this time."
    )
