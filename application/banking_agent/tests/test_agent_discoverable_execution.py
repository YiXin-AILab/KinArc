"""Agent discoverable 通用执行与账户场景回归测试。"""

import json

import pytest

from application.banking_agent.services.banking_db_service import BankingDBService


def _unlock_and_call(
    service: BankingDBService,
    tool_name: str,
    arguments: dict[str, object],
) -> str:
    """解锁并调用一个隐藏工具。"""
    unlock_result = service.unlock_discoverable_agent_tool(tool_name)
    assert unlock_result.startswith(f"Tool unlocked: {tool_name}")
    return service.call_discoverable_agent_tool(
        tool_name,
        json.dumps(arguments),
    )


def test_account_query_returns_real_account_records(
    banking_service: BankingDBService,
) -> None:
    """账户查询必须返回 ID、类型和余额，而不是固定成功文案。"""
    result = _unlock_and_call(
        banking_service,
        "get_all_user_accounts_by_user_id_3847",
        {"user_id": "125"},
    )

    assert "account_id: 03" in result
    assert "account_id: 04" in result
    assert "current_holdings: 3400" in result
    assert "current_holdings: 900" in result


def test_transfer_atomically_moves_funds_between_open_accounts(
    banking_service: BankingDBService,
) -> None:
    """内部转账应同时更新来源和目标账户余额。"""
    result = _unlock_and_call(
        banking_service,
        "transfer_funds_between_bank_accounts_7291",
        {
            "source_account_id": "03",
            "destination_account_id": "04",
            "amount": 100.0,
        },
    )

    assert "Transfer completed successfully" in result
    source = banking_service.get_record("accounts", "03")
    destination = banking_service.get_record("accounts", "04")
    assert source is not None
    assert destination is not None
    assert source["current_holdings"] == "$3300.00"
    assert destination["current_holdings"] == "$1000.00"


def test_unlock_rejects_unknown_tool(banking_service: BankingDBService) -> None:
    """不存在的隐藏工具不能通过 fallback 解锁。"""
    result = banking_service.unlock_discoverable_agent_tool("not_a_real_tool")

    assert result == "Error: Unknown agent tool 'not_a_real_tool'."


def test_call_requires_prior_unlock(banking_service: BankingDBService) -> None:
    """真实工具在未解锁时也不得调用。"""
    result = banking_service.call_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847",
        '{"user_id":"125"}',
    )

    assert "has not been unlocked" in result


@pytest.mark.parametrize("arguments", ["{", "[]", "null"])
def test_call_rejects_invalid_json_object(
    banking_service: BankingDBService,
    arguments: str,
) -> None:
    """通用调用必须拒绝损坏或非对象 JSON。"""
    banking_service.unlock_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847"
    )

    result = banking_service.call_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847",
        arguments,
    )

    assert result.startswith("Error: Invalid JSON in arguments:")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"user_id": "125", "unexpected": True},
    ],
)
def test_call_rejects_invalid_signature_arguments(
    banking_service: BankingDBService,
    arguments: dict[str, object],
) -> None:
    """通用调用必须按真实方法签名校验参数。"""
    banking_service.unlock_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847"
    )

    result = banking_service.call_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847",
        json.dumps(arguments),
    )

    assert result.startswith("Error: Invalid arguments:")


def test_call_uses_tau_json_number_coercion(
    banking_service: BankingDBService,
) -> None:
    """Tau 将 JSON 整数转为 float，并交由隐藏工具自身校验。"""
    banking_service.unlock_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847"
    )

    result = banking_service.call_discoverable_agent_tool(
        "get_all_user_accounts_by_user_id_3847",
        '{"user_id":125}',
    )

    assert result.startswith("User accounts retrieved successfully.")


def test_write_call_is_logged_in_tau_agent_tool_table(
    banking_service: BankingDBService,
) -> None:
    """写工具调用必须使用 Tau 的确定性记录写入数据库。"""
    result = _unlock_and_call(
        banking_service,
        "update_transaction_rewards_3847",
        {
            "transaction_id": "txn_8d1aa1219382",
            "new_rewards_earned": "400 points",
        },
    )

    assert result.startswith("Transaction rewards updated successfully")
    calls = banking_service.query_records(
        "agent_discoverable_tools",
        tool_name="update_transaction_rewards_3847",
    )
    assert len(calls) == 1
    assert calls[0][1]["status"] == "CALLED"


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (
            {
                "source_account_id": "03",
                "destination_account_id": "03",
                "amount": 1.0,
            },
            "cannot be the same",
        ),
        (
            {
                "source_account_id": "03",
                "destination_account_id": "04",
                "amount": 0.0,
            },
            "must be positive",
        ),
        (
            {
                "source_account_id": "missing",
                "destination_account_id": "04",
                "amount": 1.0,
            },
            "Source account 'missing' not found",
        ),
        (
            {
                "source_account_id": "03",
                "destination_account_id": "04",
                "amount": 999999.0,
            },
            "Insufficient funds",
        ),
    ],
)
def test_transfer_rejects_invalid_business_preconditions_without_mutation(
    banking_service: BankingDBService,
    arguments: dict[str, object],
    expected_error: str,
) -> None:
    """转账失败不得留下部分余额变更。"""
    before_source = banking_service.get_record("accounts", "03")
    before_destination = banking_service.get_record("accounts", "04")

    result = _unlock_and_call(
        banking_service,
        "transfer_funds_between_bank_accounts_7291",
        arguments,
    )

    assert expected_error in result
    assert banking_service.get_record("accounts", "03") == before_source
    assert banking_service.get_record("accounts", "04") == before_destination


def test_transfer_rejects_closed_account_without_mutation(
    banking_service: BankingDBService,
) -> None:
    """关闭账户不能参与内部转账。"""
    banking_service.update_record("accounts", "04", {"status": "CLOSED"})
    before_source = banking_service.get_record("accounts", "03")

    result = _unlock_and_call(
        banking_service,
        "transfer_funds_between_bank_accounts_7291",
        {
            "source_account_id": "03",
            "destination_account_id": "04",
            "amount": 1.0,
        },
    )

    assert "Destination account '04' is not active" in result
    assert banking_service.get_record("accounts", "03") == before_source
