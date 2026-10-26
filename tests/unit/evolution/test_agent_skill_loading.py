"""Evolution 静态资产的 Skill 根目录装配测试。"""

from pathlib import Path

import pytest
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.skill.index import SkillIndex
from evolution import agentsystem_improver


@pytest.mark.parametrize("with_skills", [False, True])
def test_agent_spec_resolves_skill_assets_independently_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_skills: bool
) -> None:
    """自定义宿主传递包内 Skill 路径，未启用 Skill 时保持空索引。"""
    assets = tmp_path / "agent"
    (assets / "config").mkdir(parents=True)
    (assets / "prompt.md").write_text("Optimize the agent.", encoding="utf-8")
    config = """agent_id: test-optimizer
agent_name: Test Optimizer
prompt: prompt.md
outbound:
  type: log
llm_model:
  type: litellm
  model: openai/test
"""
    if with_skills:
        config += "skill_roots: [skills]\nenabled_skills: [test-guide]\n"
        directory = assets / "skills" / "test-guide"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\nname: test-guide\ndescription: Test guide.\n---\n"
            "Read [details](details.md).\n",
            encoding="utf-8",
        )
        (directory / "details.md").write_text("Component details.", encoding="utf-8")
    (assets / "config" / "agent.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(agentsystem_improver, "__file__", str(tmp_path / "host.py"))
    monkeypatch.chdir(tmp_path.parent)
    plugin = PluginSource(
        plugin_id="test-plugin",
        origin=PluginOrigin.APPLICATION,
        path=tmp_path / "plugin.py",
        register=lambda context: None,
    )

    spec = agentsystem_improver.load_evolution_agent_spec(
        plugin_source=plugin, environ={}
    )
    index = SkillIndex.load(
        roots=spec.skill_roots, enabled_skills=spec.agent_config.enabled_skills
    )

    assert spec.skill_roots == ((assets / "skills",) if with_skills else ())
    assert index.names() == (("test-guide",) if with_skills else ())
    if with_skills:
        assert "details.md" in index.catalog()[0].linked_files
