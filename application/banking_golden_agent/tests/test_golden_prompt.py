"""Golden documents are supplied per case and only injected at system bootstrap."""

from dataclasses import replace
from pathlib import Path

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentOutboundConfig,
    ModelParamsConfig,
)
from agentsystem.context.system_prompt import SystemPromptBuilder
from agentsystem.message.models import TextBlock
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecisionType,
    MiddlewarePoint,
    SystemPromptBuildPayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.plugin.discovery import discover_application_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.models import ToolSource
from agentsystem.tool.registry import ToolRegistry

from application.banking_golden_agent.plugins.golden_prompt import (
    extract_required_documents,
)


def test_extract_only_documents() -> None:
    policy = (
        "shared policy\n<required_documents>\n"
        "## Case A\nA only\n</required_documents>\n"
    )
    assert extract_required_documents(policy) == "## Case A\nA only"


@pytest.mark.parametrize(
    "policy", ["plain", "<required_documents>missing", "</required_documents>"]
)
def test_missing_document_block_fails(policy: str) -> None:
    with pytest.raises(ValueError):
        extract_required_documents(policy)


def test_empty_golden_knowledge_and_nested_markers_are_preserved() -> None:
    for text in (
        "(No documents provided)",
        "example <required_documents>x</required_documents>",
        "\n  document whitespace and {{braces}}\n\n",
    ):
        assert (
            extract_required_documents(
                f"<required_documents>\n{text}\n</required_documents>"
            )
            == text
        )


@pytest.fixture
def golden_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> MiddlewareManager:
    monkeypatch.delenv("TAU2_DOMAIN_POLICY_FILE", raising=False)
    services = PluginSetupServices()
    services.freeze()
    sources = discover_application_plugins(
        Path(__file__).parents[1], "banking-golden-agent"
    )
    contributions = load_plugin_contributions(
        sources,
        agent_id="banking-golden-agent",
        options={},
        services=services,
    )
    registry = MiddlewareRegistry()
    registry.register_many(
        tuple(
            item for contribution in contributions for item in contribution.middlewares
        )
    )
    registry.freeze()
    return MiddlewareManager(registry)


async def test_registered_after_hook_is_lazy_and_session_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_manager: MiddlewareManager,
) -> None:
    manager = golden_manager
    context = MiddlewareContext(
        invocation_id="",
        point=MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
        ctx=SessionContext(
            app_id="app",
            user_id="user",
            agent_id="banking-golden-agent",
            session_id="s",
        ),
        run_id=None,
        trace_id=None,
        state_snapshot=None,
        payload=SystemPromptBuildPayload(
            prompt="fixed instructions\n<required_documents>\n"
            "{{required_documents}}\n</required_documents>"
        ),
    )
    before = await manager.dispatch(
        replace(context, point=MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD)
    )
    assert before.type is MiddlewareDecisionType.CONTINUE
    with pytest.raises(KeyError):
        await manager.dispatch(context)
    for marker in ("case-a", "case-b", "(No documents provided)"):
        path = tmp_path / f"{marker}.md"
        path.write_text(
            f"shared policy\n<required_documents>\n{marker}\n</required_documents>\n"
        )
        monkeypatch.setenv("TAU2_DOMAIN_POLICY_FILE", str(path))
        result = await manager.dispatch(context)
        assert result.type is MiddlewareDecisionType.UPDATE
        assert isinstance(result.payload, SystemPromptBuildPayload)
        assert result.payload.prompt == (
            f"fixed instructions\n<required_documents>\n{marker}\n</required_documents>"
        )
    path.write_text("not a golden policy")
    with pytest.raises(ValueError):
        await manager.dispatch(context)
    path.write_text("<required_documents>\ndocument\n</required_documents>")
    with pytest.raises(ValueError, match="placeholder"):
        await manager.dispatch(
            replace(context, payload=SystemPromptBuildPayload(prompt="missing slot"))
        )


async def test_golden_documents_preserve_default_skill_and_tool_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_manager: MiddlewareManager,
) -> None:
    policy = tmp_path / "policy.md"
    policy.write_text(
        "shared policy\n<required_documents>\n"
        "<document>A & B</document>\n</required_documents>"
    )
    monkeypatch.setenv("TAU2_DOMAIN_POLICY_FILE", str(policy))
    skill = tmp_path / "account-help"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: account-help\ndescription: Help with accounts\n---\nRead policy."
    )

    def account_tool() -> str:
        """Read an account."""
        return "account"

    tools = ToolRegistry()
    tools.register(
        build_function_tool(
            account_tool, source=ToolSource.CUSTOMIZED, rules="Verify identity first"
        )
    )
    tools.freeze()
    builder = SystemPromptBuilder(
        base_prompt="Bank policy\n{{required_documents}}",
        tool_registry=tools,
        skill_index=SkillIndex.load(
            roots=(tmp_path,), enabled_skills=("account-help",)
        ),
        middleware_manager=golden_manager,
    )
    result = await builder.build(
        SessionContext(app_id="app", agent_id="agent", user_id="user", session_id="s"),
        agent_config=AgentConfig(
            agent_id="agent",
            agent_name="agent",
            prompt="unused",
            outbound=AgentOutboundConfig(type="log"),
            llm_model=ModelParamsConfig(model="test"),
        ),
    )
    assert isinstance(result.content[0], TextBlock)
    prompt = result.content[0].text
    assert (
        "<agent_prompt>\nBank policy\n<document>A & B</document>\n</agent_prompt>"
        in prompt
    )
    assert "<skill_catalog>" in prompt and 'name="account-help"' in prompt
    assert "<tool_rules>" in prompt and "Verify identity first" in prompt
    assert "{{required_documents}}" not in prompt and "shared policy" not in prompt
