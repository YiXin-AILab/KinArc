"""借记卡 discoverable 工具的业务行为测试。"""

from application.banking_agent.services.banking_db_service import BankingDBService


def _call(service: BankingDBService, tool_name: str, arguments: str) -> str:
    assert service.unlock_discoverable_agent_tool(tool_name).startswith(
        "Tool unlocked:"
    )
    return service.call_discoverable_agent_tool(tool_name, arguments)


def test_activate_freeze_and_unfreeze_debit_card(
    banking_service: BankingDBService,
) -> None:
    activated = _call(
        banking_service,
        "activate_debit_card_8291",
        '{"card_id":"dbc_7a3f9c2b1e","last_4_digits":"2847",'
        '"expiration_date":"01/31/2030","cvv":"516","pin":"2468"}',
    )
    assert "Activation Successful" in activated
    card = banking_service.get_record("debit_cards", "dbc_7a3f9c2b1e")
    assert card is not None
    assert "pin" not in card
    assert card["status"] == "ACTIVE"

    frozen = _call(
        banking_service,
        "freeze_debit_card_3892",
        '{"card_id":"dbc_7a3f9c2b1e"}',
    )
    assert "Frozen Successfully" in frozen
    unfrozen = _call(
        banking_service,
        "unfreeze_debit_card_3893",
        '{"card_id":"dbc_7a3f9c2b1e"}',
    )
    assert "Unfrozen Successfully" in unfrozen


def test_activation_rejects_wrong_card_kind_without_mutation(
    banking_service: BankingDBService,
) -> None:
    before = banking_service.get_record("debit_cards", "dbc_4e8d2a6f9c")
    result = _call(
        banking_service,
        "activate_debit_card_8291",
        '{"card_id":"dbc_4e8d2a6f9c","last_4_digits":"8765",'
        '"expiration_date":"01/31/2030","cvv":"432","pin":"2468"}',
    )
    assert result.startswith("Error:")
    assert banking_service.get_record("debit_cards", "dbc_4e8d2a6f9c") == before


def test_pin_change_and_temporary_limit(banking_service: BankingDBService) -> None:
    banking_service.update_record(
        "debit_cards",
        "dbc_lj82d4f1a9_bluest",
        {"pin": "2468", "daily_purchase_limit": 2000},
    )
    changed = _call(
        banking_service,
        "change_debit_card_pin_6285",
        '{"card_id":"dbc_lj82d4f1a9_bluest","current_pin":"2468","new_pin":"1357"}',
    )
    assert "PIN Changed Successfully" in changed
    limit = _call(
        banking_service,
        "request_temporary_debit_card_limit_increase_8374",
        '{"card_id":"dbc_lj82d4f1a9_bluest","limit_type":"purchase","new_limit":2500}',
    )
    assert "Increase Granted Successfully" in limit
    card = banking_service.get_record("debit_cards", "dbc_lj82d4f1a9_bluest")
    assert card is not None
    assert card["daily_purchase_limit"] == 2500
    assert card["original_purchase_limit"] == 2000


def test_get_cards_by_account(banking_service: BankingDBService) -> None:
    result = _call(
        banking_service,
        "get_debit_cards_by_account_id_7823",
        '{"account_id":"chk_538bfb9cba"}',
    )
    assert "dbc_538bfb9cba" in result
    assert '"card_id": "dbc_538bfb9cba"' in result


def test_order_debit_card_creates_pending_card_and_deducts_fees(
    banking_service: BankingDBService,
) -> None:
    """下单必须复现 Tau 的订单、卡片、手续费三项状态转换。"""
    result = _call(
        banking_service,
        "order_debit_card_5739",
        '{"account_id":"03","user_id":"125","delivery_option":"EXPEDITED",'
        '"delivery_fee":10,"card_design":"PREMIUM","design_fee":5,'
        '"shipping_address":"1 Main St"}',
    )

    assert result.startswith("Debit Card Order Confirmed")
    cards = banking_service.query_records("debit_cards", account_id="03")
    assert any(card_id == "dbc_3d75961c9e6e" for card_id, _ in cards)
    account = banking_service.get_record("accounts", "03")
    assert account is not None
    assert account["current_holdings"] == "3385.00"
    fees = banking_service.query_records(
        "bank_account_transaction_history",
        account_id="03",
        type="debit_card_fee",
    )
    assert len(fees) == 1
