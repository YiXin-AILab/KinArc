"""Skill Validator 的开放内容、资源发现与启动路径校验测试。"""

from pathlib import Path
from unittest.mock import patch

import pytest
from agentsystem.skill import validator as skill_validator
from agentsystem.skill.safe_open import open_file_beneath_root
from agentsystem.skill.validator import SkillSource, SkillValidator


def _write(root: Path, frontmatter: str, body: str = "# body") -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n{body}",
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize(
    "frontmatter",
    (
        "name: valid-skill\nname: duplicate\ndescription: test",
        "name: Invalid_Name\ndescription: test",
        "name: valid-skill\ndescription: test\nmetadata: []",
    ),
)
def test_rejects_invalid_frontmatter(tmp_path: Path, frontmatter: str) -> None:
    """重复键、非法名称和错误字段类型必须在启动期失败。"""
    root = _write(tmp_path / "valid-skill", frontmatter)

    with pytest.raises(ValueError):
        SkillValidator().validate(root, SkillSource.APPLICATION)


def test_accepts_long_content_and_metadata(tmp_path: Path) -> None:
    """长度是编写建议，不是内容加载门槛。"""
    description = "说" * 1_025
    compatibility = "环" * 501
    root = _write(
        tmp_path / "long-skill",
        f"name: long-skill\ndescription: {description}\ncompatibility: {compatibility}",
        "中" * 8_001,
    )
    result = SkillValidator().validate(root, SkillSource.APPLICATION)
    assert result.description == description
    assert result.compatibility == compatibility


def test_resource_listing_has_no_count_or_size_quota(tmp_path: Path) -> None:
    """大附件与大量文件不影响初始化，清单只包含路径。"""
    root = _write(tmp_path / "many", "name: many\ndescription: test")
    for number in range(2_001):
        (root / f"{number:04}.txt").touch()
    with (root / "large.bin").open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    result = SkillValidator().validate(root, SkillSource.APPLICATION)
    assert result.resources == tuple(
        sorted(["SKILL.md", "large.bin", *(f"{n:04}.txt" for n in range(2_001))])
    )


@pytest.mark.parametrize(
    "value", ("lookup lookup missing", "[lookup, lookup]", "{x: 1}", "null", "42")
)
def test_ignores_allowed_tools_and_unknown_fields(tmp_path: Path, value: str) -> None:
    """扩展字段不校验或产生额外的结构化元数据。"""
    root = _write(
        tmp_path / "valid-skill",
        f"name: valid-skill\ndescription: test\nallowed-tools: {value}\ncustom: true",
    )
    result = SkillValidator().validate(root, SkillSource.APPLICATION)
    assert result.name == "valid-skill"
    assert "allowed_tools" not in type(result).__dataclass_fields__
    assert "warnings" not in type(result).__dataclass_fields__


def test_rejects_symlinked_main_file(tmp_path: Path) -> None:
    """SKILL.md 本身不得通过符号链接绕过主文件校验。"""
    root = tmp_path / "linked"
    root.mkdir()
    target = tmp_path / "real.md"
    target.write_text("---\nname: linked\ndescription: test\n---\n", encoding="utf-8")
    (root / "SKILL.md").symlink_to(target)

    with pytest.raises(ValueError, match="SKILL.md"):
        SkillValidator().validate(root, SkillSource.APPLICATION)


def test_lists_resources_without_opening_them(tmp_path: Path) -> None:
    """启动时只打开主文件，附件只参与路径枚举。"""
    root = _write(tmp_path / "valid-skill", "name: valid-skill\ndescription: test")
    (root / "detail.txt").write_text("参考", encoding="utf-8")

    def open_main(directory: Path, target: Path) -> int:
        assert target.name == "SKILL.md"
        return open_file_beneath_root(directory, target)

    with patch.object(skill_validator, "open_file_beneath_root", open_main):
        result = SkillValidator().validate(root, SkillSource.APPLICATION)
    assert result.resources == ("SKILL.md", "detail.txt")


def test_reports_missing_main_file_distinctly(tmp_path: Path) -> None:
    """缺少主文件应与存在但无法安全打开的情况使用不同诊断。"""
    root = tmp_path / "missing"
    root.mkdir()

    with pytest.raises(ValueError, match="^Skill 缺少 SKILL.md$"):
        SkillValidator().validate(root, SkillSource.APPLICATION)


def test_rejects_outside_symlink_and_indexes_nested_files(tmp_path: Path) -> None:
    """目录外符号链接被拒绝，普通嵌套文件进入稳定索引。"""
    root = _write(
        tmp_path / "valid-skill",
        "name: valid-skill\ndescription: test",
    )
    references = root / "references"
    references.mkdir()
    (references / "detail.md").write_text("详情", encoding="utf-8")
    result = SkillValidator().validate(root, SkillSource.APPLICATION)
    assert result.resources == (
        "SKILL.md",
        "references/detail.md",
    )

    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (references / "outside.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="路径"):
        SkillValidator().validate(root, SkillSource.APPLICATION)
