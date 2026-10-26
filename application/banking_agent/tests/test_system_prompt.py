"""The banking prompt survives Runtime section building without extra content."""

from html import escape

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecisionType,
    MiddlewarePoint,
    SystemPromptBuildPayload,
)

from application.banking_agent.plugins.system_prompt import after_system_prompt_build


def context(prompt: str) -> MiddlewareContext:
    """Create the initialization context passed to the after hook."""
    return MiddlewareContext(
        invocation_id="test",
        point=MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
        ctx=SessionContext(app_id="app", agent_id="agent", user_id="u", session_id="s"),
        run_id=None,
        trace_id=None,
        state_snapshot=None,
        payload=SystemPromptBuildPayload(prompt=prompt),
    )


async def test_preserves_original_text_and_removes_other_sections() -> None:
    original = "<instructions>\nA & B, &lt;literal&gt;\n</instructions>\n"
    built = (
        f"<agent_prompt>\n{escape(original, quote=False)}\n</agent_prompt>"
        "\n\n<safety>runtime rules</safety>"
    )
    result = await after_system_prompt_build(context(built))
    assert result.type is MiddlewareDecisionType.UPDATE
    assert result.payload == SystemPromptBuildPayload(prompt=original)


@pytest.mark.parametrize("prompt", ["missing", "<agent_prompt>\nmissing end"])
async def test_missing_section_fails(prompt: str) -> None:
    with pytest.raises(ValueError, match="agent_prompt"):
        await after_system_prompt_build(context(prompt))
