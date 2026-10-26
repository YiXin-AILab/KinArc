"""Keep only the configured banking prompt in the final SYSTEM message."""

from html import unescape

from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    # MiddlewareFailureMode,
    # MiddlewarePoint,
    SystemPromptBuildPayload,
)

# from agentsystem.plugin.context import PluginSetupContext


async def after_system_prompt_build(context: MiddlewareContext) -> MiddlewareDecision:
    """Remove Runtime sections and undo its single HTML escaping pass."""
    payload = context.payload
    if not isinstance(payload, SystemPromptBuildPayload):
        raise TypeError("banking prompt requires SystemPromptBuildPayload")
    _, start, remaining = payload.prompt.partition("<agent_prompt>\n")
    prompt, end, _ = remaining.partition("\n</agent_prompt>")
    if not start or not end:
        raise ValueError("missing agent_prompt section")
    return MiddlewareDecision(
        type=MiddlewareDecisionType.UPDATE,
        payload=SystemPromptBuildPayload(prompt=unescape(prompt)),
    )


# def register(context: PluginSetupContext) -> None:
#     """Register the application-only final prompt replacement."""
#     context.register_middleware(
#         after_system_prompt_build,
#         name="banking-system-prompt",
#         handler_id="after-system-prompt-build",
#         point=MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
#         timeout_ms=5000,
#         failure_mode=MiddlewareFailureMode.FAIL_CLOSED,
#     )
