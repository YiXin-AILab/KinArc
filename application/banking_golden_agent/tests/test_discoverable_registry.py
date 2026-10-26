"""隐藏工具注册表与 Banking 数据原语测试。"""

import pytest

from application.banking_golden_agent.services.banking_db_service import (
    BankingDBService,
)
from application.banking_golden_agent.services.discoverable_registry import (
    DiscoverableToolKind,
    build_discoverable_registry,
    discoverable_tool,
    invoke_discoverable_tool,
    parse_discoverable_arguments,
)


def test_registry_invokes_valid_bound_method() -> None:
    """注册表应校验参数并调用真实绑定方法。"""

    class DemoTools:
        @discoverable_tool(DiscoverableToolKind.WRITE)
        def add(self, left: int, right: int = 1) -> str:
            return str(left + right)

    registry = build_discoverable_registry(DemoTools)

    result = invoke_discoverable_tool(
        registry["add"],
        DemoTools(),
        {"left": 2},
    )

    assert result == "3"


def test_registry_rejects_duplicate_tool_names() -> None:
    """两个方法不得声明相同的隐藏工具名。"""

    class DuplicateTools:
        @discoverable_tool(DiscoverableToolKind.READ, name="duplicate")
        def first(self) -> str:
            return "first"

        @discoverable_tool(DiscoverableToolKind.READ, name="duplicate")
        def second(self) -> str:
            return "second"

    with pytest.raises(ValueError, match="重复"):
        build_discoverable_registry(DuplicateTools)


def test_registry_rejects_unsupported_parameter_annotation() -> None:
    """隐藏工具参数必须使用可从 JSON 校验的明确类型。"""

    class InvalidTools:
        @discoverable_tool(DiscoverableToolKind.READ)
        def invalid(self, value: complex) -> str:
            return str(value)

    with pytest.raises(TypeError, match="不支持"):
        build_discoverable_registry(InvalidTools)


@pytest.mark.parametrize("raw", ["[]", "null", '"value"', "1"])
def test_parse_discoverable_arguments_requires_json_object(raw: str) -> None:
    """通用调用参数必须是 JSON object。"""
    with pytest.raises(ValueError, match="JSON object"):
        parse_discoverable_arguments(raw)


def test_invoke_rejects_missing_and_extra_arguments() -> None:
    """签名绑定应拒绝缺失参数和未知参数。"""

    class DemoTools:
        @discoverable_tool(DiscoverableToolKind.READ)
        def read(self, record_id: str) -> str:
            return record_id

    definition = build_discoverable_registry(DemoTools)["read"]

    with pytest.raises(ValueError, match="参数"):
        invoke_discoverable_tool(definition, DemoTools(), {})
    with pytest.raises(ValueError, match="参数"):
        invoke_discoverable_tool(
            definition,
            DemoTools(),
            {"record_id": "1", "unexpected": True},
        )


def test_database_reset_restores_snapshot_and_capabilities(
    banking_service: BankingDBService,
) -> None:
    """重置应恢复业务数据并清空临时工具状态。"""
    original = banking_service.get_record("users", "125")
    assert original is not None
    banking_service.update_record("users", "125", {"email": "changed@example.com"})
    banking_service._unlocked_agent_tools["demo"] = {}  # noqa: SLF001
    banking_service._given_user_tools["demo"] = {}  # noqa: SLF001
    banking_service._called_agent_tools.append({"tool_name": "demo"})  # noqa: SLF001

    banking_service.reset()

    assert banking_service.get_record("users", "125") == original
    assert banking_service._unlocked_agent_tools == {}  # noqa: SLF001
    assert banking_service._given_user_tools == {}  # noqa: SLF001
    assert banking_service._called_agent_tools == []  # noqa: SLF001


def test_database_primitives_do_not_modify_source_file(
    banking_service: BankingDBService,
) -> None:
    """运行时数据变更不得回写初始 JSON。"""
    source_before = banking_service._db_path.read_bytes()  # noqa: SLF001

    assert banking_service.add_record("test_records", "one", {"value": 1})
    assert banking_service.get_record("test_records", "one") == {"value": 1}
    assert banking_service.update_record("test_records", "one", {"value": 2}) == {
        "value": 2
    }
    assert banking_service.delete_record("test_records", "one")

    assert banking_service._db_path.read_bytes() == source_before  # noqa: SLF001


def test_database_query_returns_copies(
    banking_service: BankingDBService,
) -> None:
    """查询结果不得允许调用方绕过服务锁直接修改数据。"""
    records = banking_service.query_records("users", user_id="125")
    assert len(records) == 1

    records[0][1]["email"] = "mutated@example.com"

    stored = banking_service.get_record("users", "125")
    assert stored is not None
    assert stored["email"] != "mutated@example.com"
