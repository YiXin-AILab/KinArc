"""Application Agent 自动发现与资产加载测试。"""

import logging
from pathlib import Path

import pytest
from agentsystem.service.agents import discover_agent_assets


def _write_agent(
    root: Path,
    *,
    agent_id: str,
    prompt: str = "prompt/system.md",
) -> None:
    """创建一个最小合法 Application Agent。"""
    (root / "config").mkdir(parents=True)
    (root / "prompt").mkdir()
    (root / "skills").mkdir()
    (root / "prompt" / "system.md").write_text(
        f"你是 {agent_id}。\n",
        encoding="utf-8",
    )
    (root / "config" / "agent.yaml").write_text(
        (
            f"agent_id: {agent_id}\n"
            f"agent_name: {agent_id} name\n"
            f"prompt: {prompt}\n"
            "skill_roots: [skills]\n"
            "outbound:\n"
            "  type: log\n"
            "llm_model:\n"
            "  type: litellm\n"
            "  model: openai/test-model\n"
        ),
        encoding="utf-8",
    )


def test_discovery_rejects_any_declared_invalid_agent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """任一已声明 Agent 配置无效都阻止节点以缺失目录启动。"""
    applications = tmp_path / "application"
    _write_agent(applications / "valid", agent_id="valid-agent")
    broken = applications / "broken" / "config"
    broken.mkdir(parents=True)
    (broken / "agent.yaml").write_text("agent_id: [\n", encoding="utf-8")
    (applications / "ignored").mkdir()

    with caplog.at_level(logging.ERROR), pytest.raises(ExceptionGroup):
        discover_agent_assets(applications, environ={})

    assert "broken" in caplog.text
    assert "agent_config" in caplog.text


def test_discovery_rejects_duplicate_agent_ids(tmp_path: Path) -> None:
    """重复 agent_id 不允许由扫描顺序决定胜者。"""
    applications = tmp_path / "application"
    _write_agent(applications / "first", agent_id="same")
    _write_agent(applications / "second", agent_id="same")

    with pytest.raises(ValueError, match="same"):
        discover_agent_assets(applications, environ={})


def test_discovery_rejects_paths_escaping_application_root(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prompt 与 Skill 文件逃逸必须在启动阶段 fail-close。"""
    applications = tmp_path / "application"
    application = applications / "escaping"
    _write_agent(application, agent_id="escaping", prompt="../secret.md")
    (applications / "secret.md").write_text("secret", encoding="utf-8")

    with caplog.at_level(logging.ERROR), pytest.raises(ExceptionGroup):
        discover_agent_assets(applications, environ={})

    assert "application_assets" in caplog.text
    assert "ValueError" in caplog.text
    assert "必须位于 Application 根目录内" not in caplog.text


def test_discovery_allows_zero_valid_agents(tmp_path: Path) -> None:
    """空 Application 根目录返回空 Registry 输入而不是阻止 Service。"""
    applications = tmp_path / "application"
    applications.mkdir()

    assert discover_agent_assets(applications, environ={}) == ()
