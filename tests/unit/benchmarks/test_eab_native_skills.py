"""原生 EAB Skill 应能加载、保持隔离，并在四组冻结后保留。"""

from pathlib import Path

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
)
from agentsystem.message.models import ToolCallBlock
from agentsystem.tool.authorization import ToolAuthorizationDecisionType
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.models import ToolOutput, ToolSource
from benchmarks.evoagentbench.experiments.arms import freeze_arms, verify_arms
from benchmarks.evoagentbench.runners.skills import (
    CandidateSkillAuthorization,
    configure_skills,
    skill_roots,
)
from evolution.candidates import CandidateRepository


def config() -> AgentConfig:
    return AgentConfig(
        agent_id="eab-test",
        agent_name="test",
        prompt="prompt/system.md",
        enabled_tools=("eab_read", "eab_write_memory"),
        outbound=AgentOutboundConfig(type="log"),
        llm_model=ModelParamsConfig(model="openai/test"),
    )


def add_skill(target: Path) -> None:
    root = target / "skills/delivery-check"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: delivery-check\ndescription: Check office deliverables\n---\n"
        "Read references/checks.md before delivery.\n"
    )
    (root / "references/checks.md").write_text("Verify the required file formats.")
    (target / "skills.yaml").write_text(
        "skill_roots: [skills]\nenabled_skills: [delivery-check]\n"
    )


def test_candidate_publishes_skill_without_unfreezing_model(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    (seed / "config").mkdir(parents=True)
    (seed / "prompt").mkdir()
    (seed / "config/agent.yaml").write_text("prompt: prompt/system.md\n")
    (seed / "prompt/system.md").write_text("Do the task")
    repository = CandidateRepository(
        tmp_path / "experiment",
        initial_target=seed,
        read_only=("config/",),
        workspace_root=tmp_path / "workspaces",
        objective="Improve office tasks",
    )
    initial = repository.initialize_seed()
    workspace = repository.create_workspace(initial.id, changelog_source_id=initial.id)
    add_skill(workspace / "target")
    candidate = repository.publish_workspace(
        initial.id, workspace, candidate_id=repository.allocate_candidate_id()
    )
    assert candidate is not None
    arms = tmp_path / "arms"
    freeze_arms(initial.target_dir, candidate.target_dir, arms)
    assert not (arms / "prompt/skills.yaml").exists()
    assert not (arms / "yx/skills.yaml").exists()
    evolved = configure_skills(arms / "evolved", config())
    assert evolved.enabled_skills == ("delivery-check",)
    assert evolved.enabled_tools == ("eab_read", "eab_write_memory", "skill_view")
    assert evolved.llm_model == config().llm_model
    CandidateSkillAuthorization(
        skill_roots(arms / "evolved", evolved), evolved.enabled_skills
    )
    verify_arms(arms)
    (arms / "evolved/skills.yaml").write_text("enabled_skills: []\n")
    with pytest.raises(ValueError, match="changed"):
        verify_arms(arms)


def test_old_candidate_is_unchanged(tmp_path: Path) -> None:
    assert configure_skills(tmp_path, config()) == config()


@pytest.mark.parametrize("extra", ["llm_model: {}", "enabled_tools: [run_command]"])
def test_manifest_cannot_override_frozen_configuration(
    tmp_path: Path, extra: str
) -> None:
    (tmp_path / "skills.yaml").write_text(extra)
    with pytest.raises(ValueError):
        configure_skills(tmp_path, config())


@pytest.mark.parametrize("root", ["../outside", "/tmp", "config"])
def test_roots_cannot_escape_skill_assets(tmp_path: Path, root: str) -> None:
    with pytest.raises(ValueError, match="under skills"):
        skill_roots(tmp_path, config().model_copy(update={"skill_roots": (root,)}))


def test_missing_skill_and_symlink_rejected(tmp_path: Path) -> None:
    add_skill(tmp_path)
    cfg = configure_skills(tmp_path, config())
    roots = skill_roots(tmp_path, cfg)
    with pytest.raises(KeyError):
        CandidateSkillAuthorization(roots, ("missing",))
    outside = tmp_path / "outside.md"
    outside.write_text("not a skill asset")
    (tmp_path / "skills/delivery-check/references/leak.md").symlink_to(outside)
    with pytest.raises(ValueError):
        CandidateSkillAuthorization(roots, cfg.enabled_skills)
    with pytest.raises(ValueError, match="symlinks"):
        skill_roots(tmp_path, cfg)


def test_skill_root_symlink_rejected(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / "skills").symlink_to(tmp_path / "other", target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        skill_roots(tmp_path, config().model_copy(update={"skill_roots": ("skills",)}))


@pytest.mark.parametrize(
    "name,path,allowed",
    [
        ("delivery-check", None, True),
        ("delivery-check", "references/checks.md", True),
        ("delivery-check", "../../config/agent.yaml", False),
        ("delivery-check", "/etc/passwd", False),
        ("delivery-check", "unknown.md", False),
        ("../delivery-check", None, False),
        ("disabled", None, False),
    ],
)
async def test_native_skill_authorization(
    tmp_path: Path, name: str, path: str | None, allowed: bool
) -> None:
    add_skill(tmp_path)
    cfg = configure_skills(tmp_path, config())
    authorization = CandidateSkillAuthorization(
        skill_roots(tmp_path, cfg), cfg.enabled_skills
    )

    async def unused() -> ToolOutput:
        """用于授权校验的工具定义桩。"""
        return ToolOutput(content="unused")

    definition = build_function_tool(
        unused, name="skill_view", source=ToolSource.CUSTOMIZED
    )
    decision = await authorization.authorize(
        SessionContext(app_id="app", agent_id="a", user_id="u", session_id="s"),
        ToolCallBlock(
            tool_call_id="t",
            tool_name="skill_view",
            raw_arguments="{}",
            arguments={"name": name, "file_path": path},
        ),
        definition,
    )
    assert (decision.type == ToolAuthorizationDecisionType.ALLOW) == allowed
