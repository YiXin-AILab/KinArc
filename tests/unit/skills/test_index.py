"""SkillIndex 资产加载与安全读取测试。"""

from pathlib import Path

import pytest
from agentsystem.skill.index import SkillIndex
from tests.unit.skills.skill_view_helpers import invoke_tool, skill_view_tools


def write_skill(
    root: Path,
    name: str,
    *,
    declared_name: str | None = None,
    allowed_tools: str = "web_search",
) -> Path:
    """创建最小测试 Skill 目录。"""
    directory = root / name
    directory.mkdir(parents=True)
    markdown = "\n".join(
        (
            "---",
            f"name: {declared_name or name}",
            f"description: {name} 的测试说明",
            f"allowed-tools: {allowed_tools}",
            "metadata:",
            '  version: "1.0.0"',
            "---",
            "",
            f"# {name}",
        )
    )
    (directory / "SKILL.md").write_text(markdown, encoding="utf-8")
    references = directory / "references"
    references.mkdir()
    (references / "detail.md").write_text("参考内容", encoding="utf-8")
    return directory


def test_loads_only_enabled_skills_and_exposes_compact_catalog(tmp_path: Path) -> None:
    """索引只激活配置项，catalog 不提前展开正文。"""
    write_skill(tmp_path, "stock-analysis")
    write_skill(tmp_path, "car-analysis", allowed_tools="")

    index = SkillIndex.load(
        roots=(tmp_path,),
        enabled_skills=("stock-analysis",),
    )

    assert index.names() == ("stock-analysis",)
    entry = index.catalog()[0]
    assert entry.name == "stock-analysis"
    assert entry.description == "stock-analysis 的测试说明"
    assert entry.metadata == {"version": "1.0.0"}
    assert entry.linked_files == ("references/detail.md",)
    assert "# stock-analysis" not in entry.description
    assert index.get("car-analysis") is None


async def test_reads_main_file_and_related_resource_on_demand(tmp_path: Path) -> None:
    """skill_view 可按需读取主文件和 Skill 内部资源。"""
    write_skill(tmp_path, "stock-analysis")
    index = SkillIndex.load(
        roots=(tmp_path,),
        enabled_skills=("stock-analysis",),
    )

    tool = skill_view_tools((tmp_path,))[0]
    main = await invoke_tool(tool, {"name": "stock-analysis"})
    detail = await invoke_tool(
        tool,
        {
            "name": "stock-analysis",
            "file_path": index.catalog()[0].linked_files[0],
        },
    )
    assert "# stock-analysis" in main["content"]
    assert detail["content"] == "参考内容"


def test_rejects_declared_name_that_differs_from_directory(tmp_path: Path) -> None:
    """Skill 声明名称仍必须和安全目录身份一致。"""
    write_skill(
        tmp_path,
        "stock-analysis",
        declared_name="wrong-name",
    )

    with pytest.raises(ValueError, match="目录名"):
        SkillIndex.load(
            roots=(tmp_path,),
            enabled_skills=("stock-analysis",),
        )


async def test_allowed_tools_is_only_preserved_in_source(tmp_path: Path) -> None:
    """工具声明只保留在原文，不进入 Catalog。"""
    write_skill(tmp_path, "stock-analysis", allowed_tools="missing_tool")

    index = SkillIndex.load(
        roots=(tmp_path,),
        enabled_skills=("stock-analysis",),
    )

    page = await invoke_tool(
        skill_view_tools((tmp_path,))[0], {"name": "stock-analysis"}
    )
    assert "allowed-tools: missing_tool" in page["content"]
    assert "allowed_tools" not in type(index.catalog()[0]).__dataclass_fields__


def test_rejects_unknown_active_skill_and_missing_main_file(tmp_path: Path) -> None:
    """未知 active skill 和缺少 SKILL.md 都属于启动错误。"""
    with pytest.raises(KeyError):
        SkillIndex.load(
            roots=(tmp_path,),
            enabled_skills=("missing",),
        )

    (tmp_path / "missing").mkdir()
    with pytest.raises(ValueError, match="SKILL.md"):
        SkillIndex.load(
            roots=(tmp_path,),
            enabled_skills=("missing",),
        )


@pytest.mark.parametrize("kind", ("relative", "absolute", "symlink"))
async def test_reads_paths_without_resource_index(tmp_path: Path, kind: str) -> None:
    """运行期路径不要求在初始化清单或 Skill 根目录内。"""
    write_skill(tmp_path, "stock-analysis")

    outside = tmp_path / "outside.md"
    outside.write_text("外部资料", encoding="utf-8")
    resource = "../outside.md"
    if kind == "absolute":
        resource = str(outside)
    elif kind == "symlink":
        (tmp_path / "stock-analysis" / "linked.md").symlink_to(outside)
        resource = "linked.md"
    page = await invoke_tool(
        skill_view_tools((tmp_path,))[0],
        {
            "name": "stock-analysis",
            "file_path": resource,
        },
    )
    assert page["content"] == "外部资料"


def test_rejects_missing_and_symlinked_resources(tmp_path: Path) -> None:
    """指向 Skill 外部的符号链接必须在启动索引阶段被拒绝。"""
    skill_dir = write_skill(tmp_path, "stock-analysis")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (skill_dir / "references" / "outside.md").symlink_to(outside)
    with pytest.raises(ValueError, match="路径"):
        SkillIndex.load(
            roots=(tmp_path,),
            enabled_skills=("stock-analysis",),
        )


async def test_application_root_overrides_global_skill(tmp_path: Path) -> None:
    """根目录按应用到全局排序，同名 Skill 必须使用应用版本。"""
    global_root = tmp_path / "global"
    application_root = tmp_path / "application"
    write_skill(global_root, "stock-analysis")
    application = write_skill(application_root, "stock-analysis")
    (application / "SKILL.md").write_text(
        "---\nname: stock-analysis\ndescription: 应用专用\n"
        "allowed-tools: web_search\n---\n\n# application",
        encoding="utf-8",
    )

    index = SkillIndex.load(
        roots=(application_root, global_root),
        enabled_skills=("stock-analysis",),
    )

    assert index.catalog()[0].description == "应用专用"
    page = await invoke_tool(
        skill_view_tools((application_root, global_root))[0], {"name": "stock-analysis"}
    )
    assert "# application" in page["content"]
