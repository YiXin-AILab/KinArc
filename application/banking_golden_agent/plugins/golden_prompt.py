"""Build the official golden system prompt without importing Tau2."""

import asyncio
import os
from pathlib import Path

from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
    SystemPromptBuildPayload,
)
from agentsystem.plugin.context import PluginSetupContext


def extract_required_documents(policy: str) -> str:
    """Extract the outer official document block without repeating shared policy."""
    _, found_start, remaining = policy.partition("<required_documents>")
    documents, found_end, suffix = remaining.rpartition("</required_documents>")
    if not found_start or not found_end or suffix.strip():
        raise ValueError("invalid golden retrieval document block")
    # The official template contributes one newline on either side of the slot.
    return documents.removeprefix("\n").removesuffix("\n")


async def inject_golden_documents(context: MiddlewareContext) -> MiddlewareDecision:
    """Replace the prompt placeholder with raw documents for the current case."""
    payload = context.payload
    if not isinstance(payload, SystemPromptBuildPayload):
        raise TypeError("golden prompt requires SystemPromptBuildPayload")
    path = Path(os.environ["TAU2_DOMAIN_POLICY_FILE"])
    policy = await asyncio.to_thread(path.read_text, encoding="utf-8")
    documents = extract_required_documents(policy)
    if "{{required_documents}}" not in payload.prompt:
        raise ValueError("golden prompt requires {{required_documents}} placeholder")
    return MiddlewareDecision(
        type=MiddlewareDecisionType.UPDATE,
        payload=SystemPromptBuildPayload(
            prompt=payload.prompt.replace("{{required_documents}}", documents)
        ),
    )


def register(context: PluginSetupContext) -> None:
    """Register lazily so service preflight does not require a case policy."""
    context.register_middleware(
        inject_golden_documents,
        name="golden-system-prompt",
        handler_id="inject-golden-documents",
        # AFTER preserves default sections such as skill_catalog and tool_rules.
        # To skip default building, replace the active point with BEFORE below.
        # point=MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD,
        point=MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
        timeout_ms=5000,
        failure_mode=MiddlewareFailureMode.FAIL_CLOSED,
    )
