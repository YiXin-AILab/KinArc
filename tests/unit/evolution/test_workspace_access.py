"""工作区权限匹配合约。"""

import pytest
from evolution.workspace import WorkspaceAccess
from pydantic import ValidationError


@pytest.mark.parametrize(
    "path", [".env", "a/.git/config", "__pycache__/x", "a/__pycache__/x"]
)
def test_inaccessible_matches_root_and_nested_paths(path: str) -> None:
    """默认声明在任何层级生效并向后代继承。"""
    policy = WorkspaceAccess(inaccessible=("**/.*", "**/__pycache__"))
    assert policy.is_inaccessible(path)
    assert not policy.is_inaccessible("a/source.py")


def test_rules_are_explicit_and_read_only_inherits() -> None:
    """空策略不隐式隐藏文件，目录规则向后代继承。"""
    assert not WorkspaceAccess().is_inaccessible(".env")
    policy = WorkspaceAccess(read_only=("data",))
    assert policy.is_read_only("data/sub/file")
    assert not policy.is_read_only("database/file")


@pytest.mark.parametrize("rule", ["", "/tmp", "../x", "a/../x", "a//b", "a\\b"])
def test_invalid_rule_is_rejected(rule: str) -> None:
    """拒绝逃逸及歧义规则。"""
    with pytest.raises(ValidationError):
        WorkspaceAccess(inaccessible=(rule,))
