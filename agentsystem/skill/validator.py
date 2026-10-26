"""Skill frontmatter 和递归资源的启动期安全校验。"""

import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml

from agentsystem.common.models import JsonValue
from agentsystem.skill.models import SkillSource as SkillSource
from agentsystem.skill.models import SkillValidationResult
from agentsystem.skill.safe_open import open_file_beneath_root

NAME_RE = re.compile(r"^(?!-)(?!.*--)[a-z0-9-]{1,64}(?<!-)$")
_FRONTMATTER_RE = re.compile(
    r"\A---(?:\r\n|\r|\n)(.*?)(?:\r\n|\r|\n)---(?:(?:\r\n|\r|\n)|\Z)",
    re.DOTALL,
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML mapping 重复键的 SafeLoader。"""


def _unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,
)


class SkillValidator:
    """在初始化阶段同步验证单个 Skill 并生成资源路径清单。

    校验器不持有文件句柄或缓存，不解析 allowed-tools，也不实施运行期授权。
    """

    def validate(
        self,
        root: Path,
        source: SkillSource,
    ) -> SkillValidationResult:
        """校验目录、frontmatter 及所有递归资源。

        Args:
            root: 包含 SKILL.md 的 Skill 目录，不允许目录本身是符号链接。
            source: 由目录搜索优先级确定的来源标签。

        Returns:
            元数据和去重后的相对路径快照，不包含正文。

        Raises:
            ValueError: 目录、frontmatter 或资源路径不符合约束。
            UnicodeError: SKILL.md 无法按 UTF-8 解码。
            OSError: 正文读取期间发生文件系统错误。
        """
        normalized = _validate_root(root)
        main = normalized / "SKILL.md"
        try:
            main_lstat = main.lstat()
            if stat.S_ISLNK(main_lstat.st_mode) or not stat.S_ISREG(main_lstat.st_mode):
                raise ValueError("SKILL.md 必须是普通文件")
            descriptor = open_file_beneath_root(normalized, main)
        except FileNotFoundError as error:
            raise ValueError("Skill 缺少 SKILL.md") from error
        except (OSError, RuntimeError) as error:
            raise ValueError("Skill 缺少或无法安全读取 SKILL.md") from error
        with os.fdopen(descriptor, encoding="utf-8", newline="") as stream:
            text = stream.read()
        frontmatter = _parse_frontmatter(text)
        values = _validate_fields(frontmatter, normalized.name)
        resources = _list_resources(normalized, main)
        return SkillValidationResult(
            name=str(values["name"]),
            description=str(values["description"]),
            source=source,
            root=normalized,
            license=values.get("license"),
            compatibility=values.get("compatibility"),
            metadata=_metadata(values.get("metadata")),
            resources=resources,
        )


def _validate_root(root: Path) -> Path:
    candidate = Path(root)
    try:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Skill 路径不是安全目录")
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("Skill 路径不安全") from error


def _parse_frontmatter(text: str) -> dict[str, object]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError("SKILL.md frontmatter 缺失或未闭合")
    try:
        loaded = yaml.load(match.group(1), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError("SKILL.md frontmatter 无效") from error
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise ValueError("SKILL.md frontmatter 必须为字符串 mapping")
    return loaded


def _validate_fields(
    values: dict[str, object],
    directory_name: str,
) -> dict[str, Any]:
    name = values.get("name")
    description = values.get("description")
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise ValueError("Skill name 不符合规范")
    if name != directory_name:
        raise ValueError(f"Skill 目录名 {directory_name!r} 与声明 {name!r} 不一致")
    if not isinstance(description, str) or not description:
        raise ValueError("Skill description 必须为非空字符串")
    if "license" in values and not isinstance(values["license"], str):
        raise ValueError("Skill license 必须为字符串")
    compatibility = values.get("compatibility")
    if compatibility is not None and (
        not isinstance(compatibility, str) or not compatibility
    ):
        raise ValueError("Skill compatibility 必须为非空字符串")
    _metadata(values.get("metadata"))
    return values


def _metadata(value: object) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("Skill metadata 必须是字符串 mapping")
    return {str(key): item for key, item in value.items()}


def _list_resources(root: Path, main: Path) -> tuple[str, ...]:
    indexed: dict[Path, str] = {main: "SKILL.md"}
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if candidate == main:
            continue
        _register_candidate(root, candidate, indexed)
    return tuple(sorted(indexed.values()))


def _register_candidate(
    root: Path,
    candidate: Path,
    indexed: dict[Path, str],
) -> None:
    try:
        info = candidate.lstat()
        if stat.S_ISDIR(info.st_mode):
            return
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ValueError("Skill 资源不是普通文件")
        target = candidate.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError("Skill 资源路径越界")
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Skill 资源不是普通文件")
    except (OSError, RuntimeError) as error:
        raise ValueError("Skill 资源路径不安全") from error
    logical = candidate.relative_to(root).as_posix()
    existing = indexed.get(target)
    if existing != "SKILL.md" and (existing is None or logical < existing):
        indexed[target] = logical
