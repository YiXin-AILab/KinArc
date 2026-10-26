"""Service Agent Registry 与静态发现边界测试。"""

from pathlib import Path

from agentsystem.service.agents import discover_agent_assets


def test_agent_assets_discover_only_current_application_plugins(tmp_path: Path) -> None:
    """静态资产携带当前 Agent 的稳定 Application PluginSource。"""
    application = tmp_path / "example"
    (application / "config").mkdir(parents=True)
    (application / "prompt").mkdir()
    (application / "plugins").mkdir()
    (application / "prompt" / "system.md").write_text(
        "You are an example agent.",
        encoding="utf-8",
    )
    (application / "plugins" / "z_tool.py").write_text(
        "def register(context):\n    pass\n",
        encoding="utf-8",
    )
    (application / "plugins" / "helper.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (application / "config" / "agent.yaml").write_text(
        """
agent_id: example
agent_name: Example
prompt: prompt/system.md
outbound:
  type: log
llm_model:
  model: openai/test
""".strip(),
        encoding="utf-8",
    )

    assets = discover_agent_assets(tmp_path)

    assert len(assets) == 1
    assert [source.plugin_id for source in assets[0].plugin_sources] == [
        "application.example.z-tool"
    ]
