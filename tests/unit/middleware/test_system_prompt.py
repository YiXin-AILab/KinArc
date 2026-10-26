"""System prompt hooks run before a run exists and preserve session boundaries."""

import asyncio
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
from agentsystem.message.models import Role, TextBlock
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewareHandler,
    MiddlewarePoint,
    MiddlewareRegistration,
    MiddlewareScope,
    SystemPromptBuildPayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.registry import ToolRegistry


def context(point: MiddlewarePoint) -> MiddlewareContext:
    """Construct initialization context without inventing a run."""
    return MiddlewareContext(
        invocation_id="",
        point=point,
        ctx=SessionContext(
            app_id="app", agent_id="agent", user_id="user", session_id="s"
        ),
        run_id=None,
        trace_id=None,
        state_snapshot=None,
        payload=SystemPromptBuildPayload(prompt="base"),
    )


def registration(
    point: MiddlewarePoint,
    handler: MiddlewareHandler,
    name: str = "hook",
    failure_mode: MiddlewareFailureMode = MiddlewareFailureMode.FAIL_CLOSED,
) -> MiddlewareRegistration:
    """Register real handlers with finite budgets."""
    return MiddlewareRegistration(
        name=name,
        middleware_id=name,
        point=point,
        scope=MiddlewareScope.GLOBAL,
        handler=handler,
        timeout_ms=50,
        failure_mode=failure_mode,
    )


def manager(*items: MiddlewareRegistration) -> MiddlewareManager:
    """Create a frozen middleware chain."""
    registry = MiddlewareRegistry()
    registry.register_many(items)
    registry.freeze()
    return MiddlewareManager(registry)


def test_only_bootstrap_hooks_allow_missing_run_state() -> None:
    base = context(MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD)
    with pytest.raises(ValueError, match="run identity"):
        replace(base, point=MiddlewarePoint.BEFORE_RUN)
    with pytest.raises(ValueError, match="must not carry"):
        replace(base, run_id="invented")


@pytest.mark.parametrize(
    "point",
    [
        MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD,
        MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
    ],
)
async def test_continue_keeps_input_and_non_prompt_decisions_fail(
    point: MiddlewarePoint,
) -> None:
    async def keep(ctx: MiddlewareContext) -> MiddlewareDecision:
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    assert (await manager(registration(point, keep)).dispatch(context(point))).type is (
        MiddlewareDecisionType.CONTINUE
    )
    for kind in (MiddlewareDecisionType.MODIFY, MiddlewareDecisionType.BLOCK):

        async def invalid(
            ctx: MiddlewareContext, decision_type: MiddlewareDecisionType = kind
        ) -> MiddlewareDecision:
            return MiddlewareDecision(type=decision_type)

        with pytest.raises(ValueError, match="不允许"):
            await manager(registration(point, invalid)).dispatch(context(point))


@pytest.mark.parametrize(
    "point",
    [
        MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD,
        MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD,
    ],
)
async def test_update_short_circuits_and_ids_are_session_scoped(
    point: MiddlewarePoint,
) -> None:
    seen: list[str] = []

    async def update(ctx: MiddlewareContext) -> MiddlewareDecision:
        assert (
            ctx.run_id is None and ctx.trace_id is None and ctx.state_snapshot is None
        )
        seen.append(ctx.invocation_id)
        return MiddlewareDecision(
            type=MiddlewareDecisionType.UPDATE,
            payload=SystemPromptBuildPayload(prompt="<final>"),
        )

    async def unreachable(ctx: MiddlewareContext) -> MiddlewareDecision:
        raise AssertionError("UPDATE must stop this hook chain")

    middleware = manager(
        registration(point, update, "a"), registration(point, unreachable, "b")
    )
    first = context(point)
    second = replace(first, ctx=first.ctx.model_copy(update={"session_id": "another"}))
    assert (await middleware.dispatch(first)).type is MiddlewareDecisionType.UPDATE
    await middleware.dispatch(second)
    assert seen[0] != seen[1]


@pytest.mark.parametrize("value", ["", "  ", 12])
async def test_invalid_update_is_not_ignored(value: object) -> None:
    async def invalid(ctx: MiddlewareContext) -> MiddlewareDecision:
        # Validate actual plugin output even when it bypasses static typing.
        payload = SystemPromptBuildPayload(prompt="valid")
        object.__setattr__(payload, "prompt", value)
        return MiddlewareDecision(type=MiddlewareDecisionType.UPDATE, payload=payload)

    point = MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD
    with pytest.raises((ValueError, TypeError)):
        await manager(registration(point, invalid)).dispatch(context(point))


@pytest.mark.parametrize("mode", list(MiddlewareFailureMode))
@pytest.mark.parametrize("timeout", [False, True])
async def test_hook_failure_policy(mode: MiddlewareFailureMode, timeout: bool) -> None:
    async def fail(ctx: MiddlewareContext) -> MiddlewareDecision:
        if timeout:
            await asyncio.sleep(10)
        raise ValueError("test failure")

    point = MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD
    middleware = manager(registration(point, fail, failure_mode=mode))
    if mode is MiddlewareFailureMode.FAIL_CLOSED:
        with pytest.raises((TimeoutError, ValueError)):
            await middleware.dispatch(context(point))
    else:
        assert (
            await middleware.dispatch(context(point))
        ).type is MiddlewareDecisionType.CONTINUE


async def test_cancellation_propagates() -> None:
    async def cancel(ctx: MiddlewareContext) -> MiddlewareDecision:
        raise asyncio.CancelledError()

    point = MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD
    with pytest.raises(asyncio.CancelledError):
        await manager(registration(point, cancel)).dispatch(context(point))


async def test_before_skips_default_and_after_replaces_whole_text(
    tmp_path: Path,
) -> None:
    async def before(ctx: MiddlewareContext) -> MiddlewareDecision:
        assert ctx.payload == SystemPromptBuildPayload(prompt="base")
        return MiddlewareDecision(
            type=MiddlewareDecisionType.UPDATE,
            payload=SystemPromptBuildPayload(prompt="before"),
        )

    async def after(ctx: MiddlewareContext) -> MiddlewareDecision:
        assert ctx.payload == SystemPromptBuildPayload(prompt="before")
        return MiddlewareDecision(
            type=MiddlewareDecisionType.UPDATE,
            payload=SystemPromptBuildPayload(prompt="<complete>"),
        )

    class NoDefaultBuilder(SystemPromptBuilder):
        async def base_prompt(
            self, ctx: SessionContext, agent_config: AgentConfig
        ) -> str:
            raise AssertionError("default construction was not skipped")

    tools = ToolRegistry()
    tools.freeze()
    builder = NoDefaultBuilder(
        base_prompt="base",
        tool_registry=tools,
        skill_index=SkillIndex.load(roots=(tmp_path,), enabled_skills=()),
        middleware_manager=manager(
            registration(MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD, before, "before"),
            registration(MiddlewarePoint.AFTER_SYSTEM_PROMPT_BUILD, after, "after"),
        ),
    )
    config = AgentConfig(
        agent_id="agent",
        agent_name="agent",
        prompt="unused",
        outbound=AgentOutboundConfig(type="log"),
        llm_model=ModelParamsConfig(model="test"),
    )
    result = await builder.build(
        context(MiddlewarePoint.BEFORE_SYSTEM_PROMPT_BUILD).ctx, agent_config=config
    )
    assert result.content == (TextBlock(text="<complete>"),)
    assert result.role is Role.SYSTEM and result.run_id is None
