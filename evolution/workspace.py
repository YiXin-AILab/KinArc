"""工作区路径权限的共享配置与匹配规则。"""

from pathlib import PurePosixPath

from agentsystem.common.models import FrozenModel
from pydantic import field_validator


class WorkspaceAccess(FrozenModel):
    """显式声明只读及不可访问路径，未匹配路径允许读写。"""

    read_only: tuple[str, ...] = ()
    inaccessible: tuple[str, ...] = ()

    @field_validator("read_only", "inaccessible")
    @classmethod
    def validate_rules(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """验证相对 POSIX 规则并拒绝重复项。"""
        for value in values:
            body = value.removesuffix("/")
            if (
                not body.strip()
                or "\\" in body
                or any(part in {"", ".", ".."} for part in body.split("/"))
            ):
                raise ValueError("workspace 包含非法路径规则")
        if len(set(values)) != len(values):
            raise ValueError("workspace 包含重复规则")
        return values

    def is_inaccessible(self, path: str) -> bool:
        """返回路径或其祖先是否匹配不可访问规则。"""
        return _matches(path, self.inaccessible)

    def is_read_only(self, path: str) -> bool:
        """返回路径或其祖先是否匹配只读规则。"""
        return _matches(path, self.read_only)

    def is_restricted(self, path: str) -> bool:
        """返回是否禁止修改路径。"""
        return self.is_inaccessible(path) or self.is_read_only(path)


def _matches(path: str, rules: tuple[str, ...]) -> bool:
    """按路径前缀或 glob 规则匹配工作区保护项。"""
    parts = path.rstrip("/").split("/")
    return any(
        PurePosixPath("/".join(parts[:end])).full_match(rule.rstrip("/"))
        for end in range(1, len(parts) + 1)
        for rule in rules
    )
