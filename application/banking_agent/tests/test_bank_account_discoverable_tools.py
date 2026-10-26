"""银行账户 discoverable 工具的业务行为测试。"""

from application.banking_agent.services.banking_db_service import BankingDBService


def _call(service: BankingDBService, tool_name: str, arguments: str) -> str:
    service.unlock_discoverable_agent_tool(tool_name)
    return service.call_discoverable_agent_tool(tool_name, arguments)


def _record(
    service: BankingDBService, table_name: str, record_id: str
) -> dict[str, object]:
    record = service.get_record(table_name, record_id)
    assert record is not None
    return record


def test_open_and_close_bank_account(banking_service: BankingDBService) -> None:
    opened = _call(
        banking_service,
        "open_bank_account_4821",
        '{"user_id":"123","account_type":"checking","account_class":"Blue Account"}',
    )
    assert "opened successfully" in opened
    new_accounts = banking_service.query_records(
        "accounts",
        user_id="123",
        account_type="checking",
        account_class="Blue Account",
    )
    assert len(new_accounts) == 1
    account_id, account = new_accounts[-1]
    assert account["status"] == "OPEN"

    closed = _call(
        banking_service,
        "close_bank_account_7392",
        f'{{"account_id":"{account_id}"}}',
    )
    assert "closed successfully" in closed
    assert _record(banking_service, "accounts", account_id)["status"] == "CLOSED"


def test_apply_checking_and_savings_credit(
    banking_service: BankingDBService,
) -> None:
    checking = _call(
        banking_service,
        "apply_checking_account_credit_5829",
        '{"account_id":"03","amount":25.5,"credit_type":"fee_refund"}',
    )
    savings = _call(
        banking_service,
        "apply_savings_account_credit_6831",
        '{"account_id":"04","amount":10.0,"credit_type":"interest_correction"}',
    )
    assert "successfully" in checking
    assert "successfully" in savings
    assert _record(banking_service, "accounts", "03")["current_holdings"] == "$3425.50"
    assert _record(banking_service, "accounts", "04")["current_holdings"] == "910.00"


def test_account_credit_rejects_wrong_account_class_without_mutation(
    banking_service: BankingDBService,
) -> None:
    before = banking_service.get_record("accounts", "04")
    result = _call(
        banking_service,
        "apply_checking_account_credit_5829",
        '{"account_id":"04","amount":25.0,"credit_type":"fee_refund"}',
    )
    assert result.startswith("Error:")
    assert banking_service.get_record("accounts", "04") == before


def test_submit_interest_discrepancy_and_query_transactions(
    banking_service: BankingDBService,
) -> None:
    report = _call(
        banking_service,
        "submit_interest_discrepancy_report_7294",
        '{"account_id":"04","user_id":"123","expected_apy":4.5,'
        '"actual_apy":4.0,"amount_difference":12.34}',
    )
    assert "Submitted Successfully" in report
    # Tau 1.0.1 does not define this table: the upstream tool still reports
    # success, while add_to_db deliberately performs no mutation.
    assert not banking_service.has_table("interest_discrepancy_reports")

    transactions = _call(
        banking_service,
        "get_bank_account_transactions_9173",
        '{"account_id":"chk_538bfb9cba"}',
    )
    assert "bank_account_transaction_history" in transactions
    assert "btxn_bd30d42f67a2" in transactions


def test_close_account_with_balance_is_rejected(
    banking_service: BankingDBService,
) -> None:
    result = _call(
        banking_service,
        "close_bank_account_7392",
        '{"account_id":"03"}',
    )
    assert result.startswith("Error:")
    assert "balance" in result.lower()
    assert _record(banking_service, "accounts", "03")["status"] == "OPEN"


def test_savings_account_requires_fourteen_day_old_checking_account(
    banking_service: BankingDBService,
) -> None:
    """Tau 拒绝仅持有未满 14 天 checking account 的储蓄开户。"""
    result = _call(
        banking_service,
        "open_bank_account_4821",
        '{"user_id":"123","account_type":"savings","account_class":"Silver Account"}',
    )

    assert result == "Error: Account eligibility requirements not met."
    assert not banking_service.query_records(
        "accounts",
        user_id="123",
        account_type="savings",
        account_class="Silver Account",
    )
