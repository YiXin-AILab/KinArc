"""平台磁盘存储层（server.platform.store）单元测试。"""

from pathlib import Path

import pytest
import yaml
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import JsonValue
from server.platform.store import (
    PlatformStore,
    create_agent,
    create_skill,
    delete_agent,
    delete_resource,
    delete_skill,
    describe_skill,
    list_disk_agents,
    list_resources,
    list_skills,
    read_agent,
    read_resource,
    read_skill,
    skill_references,
    update_agent,
    update_skill,
    write_resource,
)


def _store(root: Path) -> PlatformStore:
    return PlatformStore(
        project_root=root,
        applications_root=root / "application",
    )


def _write_agent(
    applications_root: Path,
    directory: str,
    agent_id: str,
    *,
    skill_roots: list[str] | None = None,
    enabled_skills: list[str] | None = None,
    enabled_tools: list[str] | None = None,
) -> Path:
    """在磁盘上创建一个最小合法 Agent，目录名可与 agent_id 不同。"""
    agent_dir = applications_root / directory
    (agent_dir / "config").mkdir(parents=True)
    (agent_dir / "prompt").mkdir()
    (agent_dir / "prompt" / "system.md").write_text(
        f"你是 {agent_id}。\n",
        encoding="utf-8",
    )
    lines = [
        f"agent_id: {agent_id}",
        f"agent_name: {agent_id} name",
        "prompt: prompt/system.md",
        "outbound:",
        "  type: log",
        "llm_model:",
        "  type: litellm",
        "  model: openai/test-model",
    ]
    if skill_roots is not None:
        lines.append(f"skill_roots: [{', '.join(skill_roots)}]")
    if enabled_skills is not None:
        lines.append(f"enabled_skills: [{', '.join(enabled_skills)}]")
    if enabled_tools is not None:
        lines.append(f"enabled_tools: [{', '.join(enabled_tools)}]")
    (agent_dir / "config" / "agent.yaml").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return agent_dir


def _config(agent_id: str, **overrides: JsonValue) -> dict[str, JsonValue]:
    config: dict[str, JsonValue] = {
        "agent_id": agent_id,
        "agent_name": f"{agent_id} name",
        "prompt": "prompt/system.md",
        "outbound": {"type": "log"},
        "llm_model": {"type": "litellm", "model": "openai/test-model"},
        "metadata": {"description": "演示 Agent"},
    }
    config.update(overrides)
    return config


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "演示 Skill",
    body: str = "步骤说明。",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_list_disk_agents_marks_active_pending_and_invalid(tmp_path: Path) -> None:
    """磁盘列表同时包含生效中、待重启和配置无效的 Agent。"""
    applications = tmp_path / "application"
    _write_agent(applications, "active_dir", "active-agent")
    _write_agent(applications, "pending_dir", "pending-agent")
    broken = applications / "broken_dir" / "config"
    broken.mkdir(parents=True)
    (broken / "agent.yaml").write_text("agent_id: [\n", encoding="utf-8")

    summaries = list_disk_agents(applications, {"active-agent"})

    by_id = {item.agent_id: item for item in summaries}
    assert by_id["active-agent"].status == "active"
    assert by_id["active-agent"].model == "openai/test-model"
    assert by_id["active-agent"].description is None
    assert by_id["pending-agent"].status == "pending"
    broken_summary = by_id["broken_dir"]
    assert broken_summary.status == "invalid"
    assert broken_summary.error is not None


def test_list_disk_agents_summarizes_capabilities(tmp_path: Path) -> None:
    """摘要包含 description、tool 与 skill 计数。"""
    applications = tmp_path / "application"
    _write_agent(
        applications,
        "demo",
        "demo-agent",
        skill_roots=["skills"],
        enabled_skills=["a", "b"],
        enabled_tools=["KB_search", "get_*"],
    )
    config_path = applications / "demo" / "config" / "agent.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["metadata"] = {"description": "演示"}
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    (summary,) = list_disk_agents(applications, ())

    assert summary.description == "演示"
    assert summary.tool_count == 2
    assert summary.skill_count == 2


def test_create_agent_writes_config_and_prompt_preserving_placeholders(
    tmp_path: Path,
) -> None:
    """创建写回 `${VAR}` 占位符原文，不读取环境变量。"""
    config = _config(
        "new-agent",
        llm_model={
            "type": "litellm",
            "model": "openai/x",
            "api_key": "${UNIT_TEST_MISSING_KEY}",
        },
    )

    parsed = create_agent(tmp_path / "application", config, "你是新 Agent。")

    assert parsed.agent_id == "new-agent"
    config_path = tmp_path / "application" / "new-agent" / "config" / "agent.yaml"
    text = config_path.read_text(encoding="utf-8")
    assert "${UNIT_TEST_MISSING_KEY}" in text
    prompt = tmp_path / "application" / "new-agent" / "prompt" / "system.md"
    assert prompt.read_text(encoding="utf-8") == "你是新 Agent。"


def test_create_agent_rejects_duplicate_and_invalid(tmp_path: Path) -> None:
    """重复 agent_id 报 409 类别；结构错误与非法目录名报 400 类别。"""
    _write_agent(tmp_path / "application", "dup", "dup-agent")

    with pytest.raises(ServiceError) as conflict:
        create_agent(tmp_path / "application", _config("dup-agent"), "prompt")
    assert conflict.value.kind is ServiceErrorKind.CONFLICT

    with pytest.raises(ServiceError) as invalid:
        create_agent(tmp_path / "application", {"agent_id": "x"}, "prompt")
    assert invalid.value.kind is ServiceErrorKind.INVALID

    with pytest.raises(ServiceError) as bad_name:
        create_agent(tmp_path / "application", _config("bad/agent"), "prompt")
    assert bad_name.value.kind is ServiceErrorKind.INVALID

    with pytest.raises(ServiceError) as empty_prompt:
        create_agent(tmp_path / "application", _config("fresh-agent"), "  ")
    assert empty_prompt.value.kind is ServiceErrorKind.INVALID


def test_read_agent_returns_raw_config_and_prompt(tmp_path: Path) -> None:
    """读取返回未插值的配置原文与 Prompt 文本。"""
    _write_agent(tmp_path / "application", "demo_dir", "demo-agent")

    document = read_agent(tmp_path / "application", "demo-agent", ())

    assert document.config["agent_id"] == "demo-agent"
    assert document.prompt_text == "你是 demo-agent。\n"
    assert document.status == "pending"
    assert document.error is None


def test_read_agent_reports_invalid_config(tmp_path: Path) -> None:
    """无法解析的 Agent 以目录名回退并携带错误信息。"""
    broken = tmp_path / "application" / "broken_dir" / "config"
    broken.mkdir(parents=True)
    (broken / "agent.yaml").write_text("agent_id: [\n", encoding="utf-8")

    document = read_agent(tmp_path / "application", "broken_dir", ())

    assert document.status == "invalid"
    assert document.error is not None
    assert document.config == {}


def test_update_agent_rewrites_files_and_protects_agent_id(tmp_path: Path) -> None:
    """更新覆盖配置与 Prompt；agent_id 不允许改名。"""
    _write_agent(tmp_path / "application", "demo", "demo-agent")

    updated = _config("demo-agent", agent_name="新名字")
    update_agent(tmp_path / "application", "demo-agent", updated, "新 Prompt。")

    document = read_agent(tmp_path / "application", "demo-agent", ())
    assert document.config["agent_name"] == "新名字"
    assert document.prompt_text == "新 Prompt。"

    with pytest.raises(ServiceError) as renamed:
        update_agent(tmp_path / "application", "demo-agent", _config("other"), "x")
    assert renamed.value.kind is ServiceErrorKind.INVALID
    assert renamed.value.code == "agent_id_immutable"


def test_delete_agent_removes_directory(tmp_path: Path) -> None:
    """删除移除整个 Application 目录；未知 Agent 报 404 类别。"""
    agent_dir = _write_agent(tmp_path / "application", "demo", "demo-agent")

    delete_agent(tmp_path / "application", "demo-agent")

    assert not agent_dir.exists()
    with pytest.raises(ServiceError) as missing:
        delete_agent(tmp_path / "application", "demo-agent")
    assert missing.value.kind is ServiceErrorKind.NOT_FOUND


def test_create_global_skill_writes_parseable_skill_md(tmp_path: Path) -> None:
    """全局 Skill 落盘后可通过 SkillValidator 校验并完整读回。"""
    store = _store(tmp_path)

    create_skill(
        store,
        scope="global",
        name="demo-skill",
        description="演示",
        content="正文内容",
        metadata={"team": "platform"},
        allowed_tools=("KB_search",),
    )

    document = read_skill(store, "global", "demo-skill")
    assert document.name == "demo-skill"
    assert document.description == "演示"
    assert document.metadata == {"team": "platform"}
    assert document.allowed_tools == ("KB_search",)
    assert document.content == "正文内容"
    summary = describe_skill(store, "global", "demo-skill")
    assert summary.valid is True
    assert summary.error is None


def test_create_private_skill_appends_skill_roots(tmp_path: Path) -> None:
    """首个私有 Skill 创建时给 agent.yaml 补 skill_roots: [skills]。"""
    store = _store(tmp_path)
    agent_dir = _write_agent(
        tmp_path / "application",
        "demo_dir",
        "demo-agent",
        skill_roots=[],
    )

    create_skill(
        store,
        scope="demo-agent",
        name="private-skill",
        description="私有",
        content="内容",
    )

    assert (agent_dir / "skills" / "private-skill" / "SKILL.md").is_file()
    raw = yaml.safe_load(
        (agent_dir / "config" / "agent.yaml").read_text(encoding="utf-8")
    )
    assert raw["skill_roots"] == ["skills"]

    # 重复创建另一个私有 Skill 不重复追加 skill_roots。
    create_skill(
        store,
        scope="demo-agent",
        name="second-skill",
        description="第二个",
        content="内容",
    )
    raw = yaml.safe_load(
        (agent_dir / "config" / "agent.yaml").read_text(encoding="utf-8")
    )
    assert raw["skill_roots"] == ["skills"]


def test_create_skill_rejects_invalid_name_and_duplicates(tmp_path: Path) -> None:
    """名称不符合 NAME_RE 报 400 类别；同名重复报 409 类别。"""
    store = _store(tmp_path)
    _write_skill(tmp_path / "skills", "taken")

    with pytest.raises(ServiceError) as invalid:
        create_skill(
            store,
            scope="global",
            name="Bad_Name",
            description="x",
            content="x",
        )
    assert invalid.value.kind is ServiceErrorKind.INVALID

    with pytest.raises(ServiceError) as conflict:
        create_skill(
            store,
            scope="global",
            name="taken",
            description="x",
            content="x",
        )
    assert conflict.value.kind is ServiceErrorKind.CONFLICT

    with pytest.raises(ServiceError) as missing_agent:
        create_skill(
            store,
            scope="no-such-agent",
            name="ok-name",
            description="x",
            content="x",
        )
    assert missing_agent.value.kind is ServiceErrorKind.NOT_FOUND


def test_update_and_delete_skill(tmp_path: Path) -> None:
    """更新重写 SKILL.md；删除移除整个目录；缺失报 404 类别。"""
    store = _store(tmp_path)
    _write_skill(tmp_path / "skills", "demo-skill")

    update_skill(
        store,
        "global",
        "demo-skill",
        description="新描述",
        content="新正文",
    )
    document = read_skill(store, "global", "demo-skill")
    assert document.description == "新描述"
    assert document.content == "新正文"

    delete_skill(store, "global", "demo-skill")
    assert not (tmp_path / "skills" / "demo-skill").exists()

    with pytest.raises(ServiceError) as missing:
        delete_skill(store, "global", "demo-skill")
    assert missing.value.kind is ServiceErrorKind.NOT_FOUND


def test_list_skills_reports_visibility_and_references(tmp_path: Path) -> None:
    """私有视图附带全局库与覆盖关系；全局视图标记被哪些 Agent 覆盖。"""
    store = _store(tmp_path)
    applications = tmp_path / "application"
    _write_agent(
        applications,
        "demo_dir",
        "demo-agent",
        enabled_skills=["shared-skill"],
    )
    _write_agent(applications, "other_dir", "other-agent")
    _write_skill(tmp_path / "skills", "shared-skill")
    _write_skill(tmp_path / "skills", "global-only")
    _write_skill(applications / "demo_dir" / "skills", "shared-skill")

    view = list_skills(store, "demo-agent")

    assert view.global_skills is not None
    private = {item.name: item for item in view.skills}
    assert private["shared-skill"].source == "private"
    assert private["shared-skill"].overrides_global is True
    globals_ = {item.name: item for item in view.global_skills}
    assert globals_["shared-skill"].overridden_by == ("demo-agent",)
    assert globals_["global-only"].overridden_by == ()
    assert globals_["shared-skill"].referenced_by == ("demo-agent",)

    global_view = list_skills(store, "global")
    assert global_view.global_skills is None
    shared = {item.name: item for item in global_view.skills}["shared-skill"]
    assert shared.overridden_by == ("demo-agent",)

    with pytest.raises(ServiceError) as missing:
        list_skills(store, "no-such-agent")
    assert missing.value.kind is ServiceErrorKind.NOT_FOUND


def test_list_skills_marks_invalid_skill(tmp_path: Path) -> None:
    """frontmatter 与目录名不一致的 Skill 以 valid=false 列出。"""
    store = _store(tmp_path)
    skill_dir = tmp_path / "skills" / "dir-name"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: 不一致\n---\n",
        encoding="utf-8",
    )

    view = list_skills(store, "global")

    (summary,) = view.skills
    assert summary.name == "dir-name"
    assert summary.valid is False
    assert summary.error is not None


def test_skill_resources_roundtrip_and_escape_rejected(tmp_path: Path) -> None:
    """资源文件支持写读删；越界与非法后缀一律拒绝。"""
    store = _store(tmp_path)
    _write_skill(tmp_path / "skills", "demo-skill")

    write_resource(store, "global", "demo-skill", "references/a.md", "资源内容")
    write_resource(store, "global", "demo-skill", "data.json", "{}")

    items = list_resources(store, "global", "demo-skill")
    assert [item.path for item in items] == ["data.json", "references/a.md"]
    assert read_resource(store, "global", "demo-skill", "references/a.md") == "资源内容"

    delete_resource(store, "global", "demo-skill", "data.json")
    assert [item.path for item in list_resources(store, "global", "demo-skill")] == [
        "references/a.md"
    ]

    for bad_path in ("../escape.md", "/abs/x.md", "sub/../../escape.md"):
        with pytest.raises(ServiceError) as escape:
            write_resource(store, "global", "demo-skill", bad_path, "x")
        assert escape.value.kind is ServiceErrorKind.INVALID
        assert escape.value.code == "invalid_resource_path"

    with pytest.raises(ServiceError) as bad_suffix:
        write_resource(store, "global", "demo-skill", "image.png", "x")
    assert bad_suffix.value.code == "unsupported_resource_type"

    with pytest.raises(ServiceError) as main_file:
        write_resource(store, "global", "demo-skill", "SKILL.md", "x")
    assert main_file.value.kind is ServiceErrorKind.INVALID

    with pytest.raises(ServiceError) as missing:
        read_resource(store, "global", "demo-skill", "none.md")
    assert missing.value.kind is ServiceErrorKind.NOT_FOUND

    assert not (tmp_path / "skills" / "escape.md").exists()
    assert not (tmp_path / "escape.md").exists()


def test_skill_references_scans_enabled_skills(tmp_path: Path) -> None:
    """引用查询聚合全部 agent.yaml 的 enabled_skills。"""
    applications = tmp_path / "application"
    _write_agent(applications, "a_dir", "agent-a", enabled_skills=["shared"])
    _write_agent(applications, "b_dir", "agent-b", enabled_skills=["shared"])
    _write_agent(applications, "c_dir", "agent-c", enabled_skills=[])

    assert skill_references(applications, "shared") == ("agent-a", "agent-b")
    assert skill_references(applications, "unused") == ()
