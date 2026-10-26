"""用户侧 discoverable 工具的授权与执行测试。"""

from application.banking_agent.services.banking_db_service import BankingDBService


def test_user_tool_must_be_given_before_call(
    banking_service: BankingDBService,
) -> None:
    result = banking_service.call_discoverable_user_tool(
        "deposit_check_3847",
        '{"account_id":"03","check_amount":25.0}',
    )
    assert result.startswith("Error: Tool 'deposit_check_3847' has not been given")


def test_given_deposit_check_updates_balance(banking_service: BankingDBService) -> None:
    given = banking_service.give_discoverable_user_tool(
        "deposit_check_3847",
        '{"account_id":"03","check_amount":25.0}',
    )
    assert given.startswith("Tool given to user:")
    result = banking_service.call_discoverable_user_tool(
        "deposit_check_3847",
        '{"account_id":"03","check_amount":25.0}',
    )
    assert "deposited successfully" in result
    account = banking_service.get_record("accounts", "03")
    assert account is not None
    assert account["current_holdings"] == "$3425.00"
    calls = banking_service.query_records(
        "user_discoverable_tool_calls", tool_name="deposit_check_3847"
    )
    assert len(calls) == 1


def test_get_card_last_four_is_stable(banking_service: BankingDBService) -> None:
    banking_service.give_discoverable_user_tool("get_card_last_4_digits")
    first = banking_service.call_discoverable_user_tool(
        "get_card_last_4_digits",
        '{"credit_card_account_id":"cc_lj82d4f1a9_silver"}',
    )
    second = banking_service.call_discoverable_user_tool(
        "get_card_last_4_digits",
        '{"credit_card_account_id":"cc_lj82d4f1a9_silver"}',
    )
    assert "Last 4 digits of card:" in first
    assert first == second


def test_unknown_user_tool_cannot_be_given(banking_service: BankingDBService) -> None:
    result = banking_service.give_discoverable_user_tool("not_a_real_tool")
    assert result == "Error: Unknown user tool 'not_a_real_tool'."


def test_user_tool_arguments_are_validated(banking_service: BankingDBService) -> None:
    banking_service.give_discoverable_user_tool("deposit_check_3847")
    result = banking_service.call_discoverable_user_tool(
        "deposit_check_3847",
        '{"account_id":"03","check_amount":"25"}',
    )
    assert result.startswith("Error: Invalid arguments:")
