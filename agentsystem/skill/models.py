"""Skill 校验、资源索引和 Prompt 目录的不可变模型。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from agentsystem.common.models import JsonValue


class SkillSource(StrEnum):
    """Skill 的配置优先级来源。"""

    APPLICATION = "application"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillValidationResult:
    """启动时校验得到的 Skill 元数据与静态路径清单。

    这是发现快照，不保存正文，也不是运行期读取权限表。

    Attributes:
        name: 与目录名一致的 Skill 名称。
        description: frontmatter 中的用途说明。
        source: 命中的应用或全局根目录来源。
        root: 已解析的 Skill 根目录。
        license: 可选许可证说明。
        compatibility: 可选运行环境说明。
        metadata: frontmatter 中的附加元数据。
        resources: 去重并排序后的相对资源路径，包含 SKILL.md。
    """

    name: str
    description: str
    source: SkillSource
    root: Path = field(repr=False)
    license: str | None = None
    compatibility: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillCatalogEntry:
    """提供给 SystemPrompt 的紧凑 Skill 描述，不包含正文。

    Attributes:
        name: 已启用的 Skill 名称。
        description: Skill 用途说明。
        linked_files: 除 SKILL.md 外的静态关联资源路径。
        metadata: 从启动校验结果保留的元数据。
    """

    name: str
    description: str
    linked_files: tuple[str, ...]
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillRoots:
    """工具拼接路径使用的根目录能力对象。

    Attributes:
        paths: 按应用到全局优先级排列的目录；不在此对象内访问文件系统。
    """

    paths: tuple[Path, ...]
