"""ContextProvider 相关 builtin Plugin 测试。"""

from datetime import UTC, datetime
from pathlib import Path

import agentsystem
import pytest
from agentsystem.common.errors import RunError
from agentsystem.common.models import SessionContext
from agentsystem.context.plugins.datetime_provider import DatetimeProvider
from agentsystem.context.providers import ContextProvider, ContextProviderRunner
from agentsystem.message.models import (
    InjectedItem,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecisionType,
    MiddlewarePoint,
    RunResultMiddlewarePayload,
)
from agentsystem.middleware.registry import MiddlewareRegistry
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.models import PluginSource
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.runtime.state import AgentState, SessionStatus

_NOW = datetime(2026, 8, 21, tzinfo=UTC)
_SESSION = SessionContext(
    app_id="app",
    user_id="user",
    agent_id="agent",
    session_id="session",
)


def _builtin_source(plugin_id: str) -> PluginSource:
    package_root = Path(agentsystem.__file__).parent
    return next(
        source
        for source in discover_builtin_plugins(package_root)
        if source.plugin_id == plugin_id
    )


def _services(runner: ContextProviderRunner) -> PluginSetupServices:
    services = PluginSetupServices()
    services.register(ContextProviderRunner, runner)
    services.freeze()
    return services


def test_current_datetime_plugin_registers_lazy_definition() -> None:
    """Provider options 只在选中 Definition 后校验和构造。"""
    runner = ContextProviderRunner(timeout_seconds=0.1)
    source = _builtin_source("builtin.context.datetime-provider")

    contribution = load_plugin_contributions(
        (source,),
        agent_id="agent",
        options={"current-datetime": {"timezone": "UTC"}},
        services=_services(runner),
    )[0]

    assert contribution.tools == ()
    assert contribution.middlewares == ()
    assert len(contribution.context_providers) == 1
    definition = contribution.context_providers[0]
    assert definition.provider_type == "current-datetime"
    with pytest.raises(ValueError, match="timezone"):
        definition.create()


def test_current_datetime_plugin_builds_provider_with_empty_options() -> None:
    """无参数配置构造稳定名称的 current-datetime Provider。"""
    runner = ContextProviderRunner(timeout_seconds=0.1)
    source = _builtin_source("builtin.context.datetime-provider")
    contribution = load_plugin_contributions(
        (source,),
        agent_id="agent",
        options={},
        services=_services(runner),
    )[0]

    provider = contribution.context_providers[0].create()

    assert isinstance(provider, DatetimeProvider)
    assert provider.provider_name == "current-datetime"


class _RecordingProvider(ContextProvider):
    """记录 Plugin Middleware 回写消息。"""

    provider_name = "memory"

    def __init__(self) -> None:
        self.received: list[Message] = []

    async def get(
        self,
        ctx: SessionContext,
        *,
        message: Message,
    ) -> InjectedItem | None:
        del ctx, message
        return None

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        assert ctx == _SESSION
        self.received.append(message)


def _middleware_context(payload: RunResultMiddlewarePayload) -> MiddlewareContext:
    return MiddlewareContext(
        invocation_id="after-run",
        point=MiddlewarePoint.AFTER_RUN,
        ctx=_SESSION,
        run_id="run",
        trace_id="trace",
        state_snapshot=AgentState(session_context=_SESSION),
        payload=payload,
    )


async def test_provider_put_plugin_only_writes_succeeded_final_message() -> None:
    """FAILED/CANCELED 不回写，SUCCEEDED 只转发最终 Assistant Message。"""
    provider = _RecordingProvider()
    runner = ContextProviderRunner(timeout_seconds=0.1)
    runner.register_many((provider,))
    runner.freeze()
    source = _builtin_source("builtin.context.provider-middleware")
    contribution = load_plugin_contributions(
        (source,),
        agent_id="agent",
        options={},
        services=_services(runner),
    )[0]
    registry = MiddlewareRegistry()
    registry.register_many(contribution.middlewares)
    registry.freeze()
    manager = MiddlewareManager(registry)
    final = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="done"),),
        origin=MessageOrigin.MODEL,
        run_id="run",
        created_at=_NOW,
        visibility=MessageVisibility.ALL,
    )
    error = RunError(code="run_failed", message="failed")

    for payload in (
        RunResultMiddlewarePayload(status=SessionStatus.FAILED, error=error),
        RunResultMiddlewarePayload(status=SessionStatus.CANCELED),
    ):
        decision = await manager.dispatch(_middleware_context(payload))
        assert decision.type is MiddlewareDecisionType.CONTINUE
    decision = await manager.dispatch(
        _middleware_context(
            RunResultMiddlewarePayload(
                status=SessionStatus.SUCCEEDED,
                message=final,
            )
        )
    )

    assert decision.type is MiddlewareDecisionType.CONTINUE
    assert provider.received == [final]
    registration = contribution.middlewares[0]
    assert registration.middleware_id == (
        "builtin.context.provider-middleware.after-run"
    )
    assert registration.timeout_ms == 1_100


async def test_provider_put_plugin_is_fail_open_for_missing_success_message() -> None:
    """缺失最终消息的异常不得改变已保存的成功终态。"""
    runner = ContextProviderRunner(timeout_seconds=0.1)
    runner.freeze()
    contribution = load_plugin_contributions(
        (_builtin_source("builtin.context.provider-middleware"),),
        agent_id="agent",
        options={},
        services=_services(runner),
    )[0]
    registry = MiddlewareRegistry()
    registry.register_many(contribution.middlewares)
    registry.freeze()

    decision = await MiddlewareManager(registry).dispatch(
        _middleware_context(RunResultMiddlewarePayload(status=SessionStatus.SUCCEEDED))
    )

    assert decision.type is MiddlewareDecisionType.CONTINUE
