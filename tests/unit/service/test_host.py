"""ServiceHost 资源所有权、后台监督和 Agent 装配测试。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agentsystem.common.models import JsonValue, SessionContext
from agentsystem.config.models import (
    AgentConfig,
    AgentMediaConfig,
    AgentOutboundConfig,
    CompactionConfig,
    ModelParamsConfig,
    RuntimeConfig,
)
from agentsystem.context.plugins.provider_middleware import (
    register as register_provider_middleware,
)
from agentsystem.context.providers import ContextProvider
from agentsystem.knowledge.models import KnowledgeBaseRef
from agentsystem.llm.client import LLMClient
from agentsystem.llm.models import LLMEvent, LLMRequest
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
    MiddlewarePoint,
    RunResultMiddlewarePayload,
)
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.runtime.state import AgentState, SessionStatus, complete_run
from agentsystem.service import host as service_module
from agentsystem.service.agents import AgentRegistry, ApplicationRoot, KnowledgeRoot
from agentsystem.service.host import (
    AgentRuntimeSpec,
    ServiceHost,
    _AssetInfrastructure,
)


class _Queue:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def initialize(self, agent_ids: tuple[str, ...]) -> None:
        assert agent_ids == ()
        self.calls += 1
        await self.release.wait()


class _Dispatcher:
    def __init__(self) -> None:
        self.calls = 0
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        self.calls += 1
        await self.stopping.wait()

    def close(self) -> None:
        self.stopping.set()


class _Coordinator:
    def __init__(self) -> None:
        self.calls = 0
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        self.calls += 1
        await self.stopping.wait()

    def close(self) -> None:
        self.stopping.set()


class _NoCallProvider:
    """装配测试不允许实际调用模型。"""

    provider_name = "no-call"

    async def generate(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """任何生成调用都表示装配产生了运行副作用。"""
        del request
        raise AssertionError("装配不得调用 Provider")
        yield


class _CancellationBus:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        self.ready.set()
        await self.stopping.wait()

    async def wait_ready(self) -> None:
        await self.ready.wait()

    async def close(self) -> None:
        self.stopping.set()


def _host(
    inbox: _Queue,
    dispatcher: _Dispatcher,
    *,
    shutdown_timeout_seconds: float = 1,
    cancellation_bus: _CancellationBus | None = None,
    coordinator: _Coordinator | None = None,
) -> ServiceHost:
    class _Trace:
        async def force_flush(self, timeout: float) -> None:
            del timeout

        async def shutdown(self, timeout: float) -> None:
            del timeout

    class _Closeable:
        async def close(self) -> None:
            return None

    infrastructure = _AssetInfrastructure(
        managed_store=cast(Any, _Closeable()),
        http_session=cast(Any, _Closeable()),
        fetcher=cast(Any, object()),
        service=cast(Any, object()),
    )
    return ServiceHost(
        redis_client=cast(Any, object()),
        owns_redis_client=False,
        inbox=cast(Any, inbox),
        dispatcher=cast(Any, dispatcher),
        cancellation_bus=cast(Any, cancellation_bus or _CancellationBus()),
        application=cast(Any, object()),
        registry=AgentRegistry(()),
        trace_recorder=cast(Any, _Trace()),
        asset_infrastructure=infrastructure,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
        schedule_coordinator=cast(Any, coordinator),
    )


def _agent_config() -> AgentConfig:
    """构造带明确 hard input limit 的最小 Agent 配置。"""
    return AgentConfig(
        agent_id="agent",
        agent_name="Agent",
        prompt="unused.md",
        outbound=AgentOutboundConfig(type="log"),
        llm_model=ModelParamsConfig(
            model="openai/test",
            max_output_tokens=1_000,
            context_window=5_000,
        ),
        compaction=CompactionConfig(safety_margin_tokens=500),
    )


def test_host_passes_agent_capability_selection_to_assembler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host 只负责把严格 AgentConfig 选择项接到装配边界。"""
    captured: dict[str, object] = {}

    class CapturedAssembly(RuntimeError):
        """在 capability 装配边界停止测试。"""

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        raise CapturedAssembly

    config = _agent_config().model_copy(
        update={
            "enabled_tools": ("workspace_*",),
            "enabled_middlewares": ("audit_*",),
            "enabled_context_providers": ("current-datetime",),
        }
    )
    runtime = SimpleNamespace(
        context_provider_timeout_seconds=1.0,
        context_provider_max_content_chars=1_000,
    )
    monkeypatch.setattr(service_module, "assemble_agent_capabilities", capture)

    with pytest.raises(CapturedAssembly):
        service_module._assemble_agent(
            AgentRuntimeSpec(agent_config=config, base_prompt="base"),
            shared=cast(service_module._SharedAgentPorts, object()),
            runtime_config=cast(RuntimeConfig, runtime),
            tool_authorization=None,
        )

    assert captured["context_provider_types"] == ("current-datetime",)
    assert captured["enabled_tool_patterns"] == ("workspace_*",)
    assert captured["enabled_middleware_patterns"] == ("audit_*",)


def test_host_injects_application_root_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 携带 application_root 时注入 ApplicationRoot，缺省时不注入。"""
    captured: list[object] = []

    class CapturedAssembly(RuntimeError):
        """在 capability 装配边界停止测试。"""

    def capture(**kwargs: object) -> object:
        captured.append(kwargs.get("plugin_capabilities"))
        raise CapturedAssembly

    monkeypatch.setattr(service_module, "assemble_agent_capabilities", capture)
    runtime = SimpleNamespace(
        context_provider_timeout_seconds=1.0,
        context_provider_max_content_chars=1_000,
    )

    with pytest.raises(CapturedAssembly):
        service_module._assemble_agent(
            AgentRuntimeSpec(
                agent_config=_agent_config(),
                base_prompt="base",
                application_root=Path("/app"),
            ),
            shared=cast(service_module._SharedAgentPorts, object()),
            runtime_config=cast(RuntimeConfig, runtime),
            tool_authorization=None,
        )
    with pytest.raises(CapturedAssembly):
        service_module._assemble_agent(
            AgentRuntimeSpec(agent_config=_agent_config(), base_prompt="base"),
            shared=cast(service_module._SharedAgentPorts, object()),
            runtime_config=cast(RuntimeConfig, runtime),
            tool_authorization=None,
        )

    first = cast(tuple[tuple[type[object], object], ...], captured[0])
    second = cast(tuple[tuple[type[object], object], ...], captured[1])
    roots = [value for key, value in first if key is ApplicationRoot]
    assert roots == [ApplicationRoot(Path("/app"))]
    assert all(key is not ApplicationRoot for key, _ in second)


def _write_knowledge_base(root: Path, name: str) -> Path:
    """写入一个最小 bm25 知识库（kb.yaml + documents.json）。"""
    base = root / name
    base.mkdir(parents=True)
    base.joinpath("kb.yaml").write_text(
        f"name: {name}\nretrieval: {{strategy: bm25, top_k: 7}}\n",
        encoding="utf-8",
    )
    base.joinpath("documents.json").write_text("[]", encoding="utf-8")
    return base


def test_host_merges_knowledge_bases_into_plugin_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """knowledge_bases 解析后注入 knowledge Plugin options 与 KnowledgeRoot。"""
    captured: dict[str, object] = {}

    class CapturedAssembly(RuntimeError):
        """在 capability 装配边界停止测试。"""

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        raise CapturedAssembly

    monkeypatch.setattr(service_module, "assemble_agent_capabilities", capture)
    knowledge_root = tmp_path / "knowledge"
    _write_knowledge_base(knowledge_root, "pricing")
    config = _agent_config().model_copy(
        update={"knowledge_bases": (KnowledgeBaseRef(name="pricing"),)}
    )
    runtime = SimpleNamespace(
        context_provider_timeout_seconds=1.0,
        context_provider_max_content_chars=1_000,
    )

    with pytest.raises(CapturedAssembly):
        service_module._assemble_agent(
            AgentRuntimeSpec(
                agent_config=config,
                base_prompt="base",
                application_root=tmp_path,
                knowledge_root=knowledge_root,
            ),
            shared=cast(service_module._SharedAgentPorts, object()),
            runtime_config=cast(RuntimeConfig, runtime),
            tool_authorization=None,
        )

    options = cast(
        Mapping[str, Mapping[str, JsonValue]],
        captured["plugin_options"],
    )
    bases = cast(
        list[dict[str, JsonValue]],
        options["builtin.knowledge.knowledge-plugin"]["bases"],
    )
    assert len(bases) == 1
    assert bases[0]["name"] == "pricing"
    assert cast(dict[str, JsonValue], bases[0]["retrieval"])["top_k"] == 7
    docs_path = cast(str, bases[0]["docs_path"])
    assert docs_path == str(knowledge_root / "pricing" / "documents.json")
    capabilities = cast(
        tuple[tuple[type[object], object], ...],
        captured["plugin_capabilities"],
    )
    roots = [value for key, value in capabilities if key is KnowledgeRoot]
    assert roots == [KnowledgeRoot(knowledge_root)]


def test_host_rejects_knowledge_bases_without_knowledge_root(tmp_path: Path) -> None:
    """声明 knowledge_bases 但缺少全局知识库根时装配期失败。"""
    config = _agent_config().model_copy(
        update={"knowledge_bases": (KnowledgeBaseRef(name="pricing"),)}
    )
    runtime = SimpleNamespace(
        context_provider_timeout_seconds=1.0,
        context_provider_max_content_chars=1_000,
    )

    with pytest.raises(ValueError, match="knowledge_root"):
        service_module._assemble_agent(
            AgentRuntimeSpec(agent_config=config, base_prompt="base"),
            shared=cast(service_module._SharedAgentPorts, object()),
            runtime_config=cast(RuntimeConfig, runtime),
            tool_authorization=None,
        )


async def test_concurrent_start_only_initializes_and_runs_once() -> None:
    """并发 start 必须共享一次启动，而不是创建两个 Dispatcher。"""
    queue = _Queue()
    dispatcher = _Dispatcher()
    host = _host(queue, dispatcher)

    first = asyncio.create_task(host.start())
    second = asyncio.create_task(host.start())
    await asyncio.sleep(0)
    queue.release.set()
    await asyncio.gather(first, second)

    assert queue.calls == 1
    assert dispatcher.calls == 1
    await host.close()


async def test_schedule_coordinator_is_started_once_and_stopped_by_host() -> None:
    """启用 Scheduler 时 Host 必须监督唯一扫描任务。"""
    queue = _Queue()
    queue.release.set()
    coordinator = _Coordinator()
    host = _host(queue, _Dispatcher(), coordinator=coordinator)

    await asyncio.gather(host.start(), host.start())
    while coordinator.calls == 0:
        await asyncio.sleep(0)

    assert coordinator.calls == 1
    await host.close()
    assert coordinator.stopping.is_set()


async def test_wait_failed_reports_schedule_coordinator_failure() -> None:
    """扫描器永久退出必须传播到 Host 监督边界。"""

    class FailingCoordinator(_Coordinator):
        async def run(self) -> None:
            raise RuntimeError("schedule coordinator failed")

    queue = _Queue()
    queue.release.set()
    host = _host(queue, _Dispatcher(), coordinator=FailingCoordinator())
    await host.start()

    with pytest.raises(RuntimeError, match="schedule coordinator failed"):
        await asyncio.wait_for(host.wait_failed(), timeout=1)

    with pytest.raises(BaseExceptionGroup):
        await host.close()


async def test_cancellation_subscriber_is_ready_before_queue_initialization() -> None:
    """Server 启动不能在本节点具备取消接收能力之前初始化 Dispatcher。"""
    queue = _Queue()
    dispatcher = _Dispatcher()
    cancellation_bus = _CancellationBus()
    host = _host(queue, dispatcher, cancellation_bus=cancellation_bus)

    start = asyncio.create_task(host.start())
    await cancellation_bus.started.wait()
    while queue.calls == 0:
        await asyncio.sleep(0)

    assert queue.calls == 1
    assert dispatcher.calls == 0
    queue.release.set()
    await start
    await host.close()


async def test_wait_failed_reports_cancellation_subscriber_failure() -> None:
    """取消控制面永久退出必须使 Host 失败，不能继续返回虚假 accepted。"""

    class FailingCancellationBus(_CancellationBus):
        def __init__(self) -> None:
            super().__init__()
            self.fail = asyncio.Event()

        async def run(self) -> None:
            self.ready.set()
            await self.fail.wait()
            raise RuntimeError("cancellation subscriber failed")

    queue = _Queue()
    queue.release.set()
    cancellation_bus = FailingCancellationBus()
    host = _host(
        queue,
        _Dispatcher(),
        cancellation_bus=cancellation_bus,
    )
    await host.start()
    cancellation_bus.fail.set()

    with pytest.raises(RuntimeError, match="cancellation subscriber failed"):
        await asyncio.wait_for(host.wait_failed(), timeout=1)

    with pytest.raises(BaseExceptionGroup):
        await host.close()


async def test_wait_failed_reports_dispatcher_failure() -> None:
    """Dispatcher 顶层退出必须变成 Host 可监督失败。"""

    class FailingDispatcher(_Dispatcher):
        async def run(self) -> None:
            raise RuntimeError("dispatcher failed")

    queue = _Queue()
    queue.release.set()
    host = _host(queue, FailingDispatcher())
    await host.start()

    with pytest.raises(RuntimeError, match="dispatcher failed"):
        await asyncio.wait_for(host.wait_failed(), timeout=1)

    with pytest.raises(BaseExceptionGroup):
        await host.close()


async def test_close_during_start_never_launches_dispatcher_after_cleanup() -> None:
    """关闭必须先收敛进行中的启动，再释放资源。"""
    queue = _Queue()
    dispatcher = _Dispatcher()
    host = _host(queue, dispatcher)

    start_task = asyncio.create_task(host.start())
    await asyncio.sleep(0)
    close_task = asyncio.create_task(host.close())
    await asyncio.sleep(0)
    queue.release.set()
    await close_task

    with pytest.raises(RuntimeError, match="关闭"):
        await start_task
    assert dispatcher.calls == 0


async def test_close_deadline_survives_cancellation_resistant_startup() -> None:
    """启动协程暂不响应取消时，关闭仍必须在全局期限内返回。"""

    class ResistantQueue(_Queue):
        async def initialize(self, agent_ids: tuple[str, ...]) -> None:
            assert agent_ids == ()
            self.calls += 1
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()

    queue = ResistantQueue()
    dispatcher = _Dispatcher()
    host = _host(queue, dispatcher, shutdown_timeout_seconds=0.01)
    start_task = asyncio.create_task(host.start())
    await asyncio.sleep(0)

    with pytest.raises(BaseExceptionGroup):
        await asyncio.wait_for(host.close(), timeout=0.2)

    queue.release.set()
    with pytest.raises(RuntimeError, match="关闭"):
        await start_task
    assert dispatcher.calls == 0


class _ConfiguredProvider(ContextProvider):
    """保存独立 options 的装配测试 Provider。"""

    provider_name = "configured"

    def __init__(self, options: Mapping[str, JsonValue]) -> None:
        self.options = dict(options)
        self.received: list[Message] = []

    async def get(
        self,
        ctx: SessionContext,
        *,
        message: Message,
    ) -> InjectedItem | None:
        """装配测试不生成动态上下文。"""
        del ctx, message
        return None

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        """记录由 AFTER_RUN Middleware 转发的最终结果。"""
        del ctx
        self.received.append(message)


async def test_agent_loop_and_middleware_share_one_agent_provider_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop get 与 AFTER_RUN Middleware put 复用同一 Agent Runner。"""
    captured: dict[str, Any] = {}
    instances: list[_ConfiguredProvider] = []

    def capture_loop(**kwargs: Any) -> object:
        captured["loop_runner"] = kwargs["context_provider_runner"]
        return object()

    def capture_session(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    def register_provider(context: PluginSetupContext) -> None:
        options = context.options("configured")

        def create() -> ContextProvider:
            provider = _ConfiguredProvider(options)
            instances.append(provider)
            return provider

        context.register_context_provider("configured", create)

    provider_source = PluginSource(
        plugin_id="application.agent.configured-provider",
        origin=PluginOrigin.APPLICATION,
        path=Path("configured_provider.py"),
        register=register_provider,
    )
    put_source = PluginSource(
        plugin_id="builtin.context.provider-middleware",
        origin=PluginOrigin.BUILTIN,
        path=Path("provider_middleware.py"),
        register=register_provider_middleware,
    )
    config = _agent_config().model_copy(
        update={
            "enabled_context_providers": ("configured",),
            "plugin_options": {"configured": {"namespace": "customer"}},
        }
    )
    runtime = SimpleNamespace(
        tool_timeout_seconds=30.0,
        context_provider_timeout_seconds=1.0,
        context_provider_max_content_chars=1_000,
        middleware_llm_retry_limit=0,
        projector_timeout_seconds=1.0,
        asset=SimpleNamespace(max_file_bytes=1_000),
        trace=SimpleNamespace(capture_content=False),
        dispatcher=SimpleNamespace(heartbeat_interval_seconds=1.0),
    )
    shared = SimpleNamespace(
        messages=object(),
        states=object(),
        lock=object(),
        inbox=object(),
        projectors=SimpleNamespace(create=lambda provider_type, options: object()),
        trace_recorder=object(),
        event_bus=object(),
        cancellations=object(),
        asset_store=SimpleNamespace(store=object()),
        media_fetcher=object(),
    )
    monkeypatch.setattr(service_module, "AgentLoop", capture_loop)
    monkeypatch.setattr(service_module, "AgentSession", capture_session)

    service_module._assemble_agent(
        AgentRuntimeSpec(
            agent_config=config,
            base_prompt="base",
            plugin_sources=(provider_source, put_source),
            llm_provider=_NoCallProvider(),
        ),
        shared=cast(service_module._SharedAgentPorts, shared),
        runtime_config=cast(RuntimeConfig, runtime),
        tool_authorization=None,
    )
    captured["loop_builder"](object())

    now = datetime(2026, 8, 18, tzinfo=UTC)
    context = SessionContext(
        app_id="app",
        user_id="user",
        agent_id="agent",
        session_id="session",
    )
    running = AgentState(
        session_context=context,
        status=SessionStatus.RUNNING,
        run_id="run",
        output_mode=config.output_mode,
        started_at=now,
        active_started_at=now,
    )
    final = Message(
        message_id="assistant",
        session_id="session",
        role=Role.ASSISTANT,
        content=(TextBlock(text="done"),),
        origin=MessageOrigin.MODEL,
        run_id="run",
        created_at=now,
        visibility=MessageVisibility.ALL,
    )
    succeeded = complete_run(running, final_message_id=final.message_id, now=now)
    manager = cast(MiddlewareManager, captured["middleware_manager"])
    await manager.dispatch(
        MiddlewareContext(
            invocation_id="after-run",
            point=MiddlewarePoint.AFTER_RUN,
            ctx=context,
            run_id="run",
            trace_id="trace",
            state_snapshot=succeeded,
            payload=RunResultMiddlewarePayload(
                status=SessionStatus.SUCCEEDED,
                message=final,
            ),
        )
    )

    assert "context_provider_runner" not in captured
    assert len(instances) == 1
    assert instances[0].received == [final]


def test_host_injects_agent_budget_and_agent_middleware_into_compactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host 构造 Compactor 时使用 Agent 预算与专属 MiddlewareManager。"""
    captured: dict[str, object] = {}

    class CapturedCompactor(RuntimeError):
        """在 Compactor 构造边界停止剩余装配。"""

    def capture_compactor(**kwargs: Any) -> object:
        captured.update(kwargs)
        raise CapturedCompactor

    config = _agent_config().model_copy(
        update={"media": AgentMediaConfig(include_asset_id_in_context=True)}
    )
    provider = _NoCallProvider()
    monkeypatch.setattr(service_module, "Compactor", capture_compactor)

    with pytest.raises(CapturedCompactor):
        service_module._assemble_agent(
            AgentRuntimeSpec(
                agent_config=config,
                base_prompt="base",
                llm_provider=provider,
            ),
            shared=cast(
                service_module._SharedAgentPorts,
                SimpleNamespace(
                    asset_store=SimpleNamespace(store=object()),
                    media_fetcher=object(),
                ),
            ),
            runtime_config=cast(
                RuntimeConfig,
                SimpleNamespace(
                    tool_timeout_seconds=30.0,
                    asset=SimpleNamespace(max_file_bytes=10),
                    context_provider_timeout_seconds=1.0,
                    context_provider_max_content_chars=1_000,
                ),
            ),
            tool_authorization=None,
        )

    assert isinstance(captured.pop("middleware_manager"), MiddlewareManager)
    assert captured == {
        "client": captured["client"],
        "generation_config": config.llm_model.to_generation_config(),
        "context_window": config.llm_model.context_window,
        "config": config.compaction,
        "include_asset_id_in_context": True,
    }
    assert cast(LLMClient, captured["client"]).provider_name == provider.provider_name


def test_host_injects_asset_id_setting_into_context_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主上下文构建器必须使用同一个 Agent 媒体句柄开关。"""
    captured: dict[str, object] = {}

    class CapturedContextBuilder(RuntimeError):
        """在 ContextBuilder 构造边界停止剩余装配。"""

    def capture_context_builder(
        config: object,
        *,
        include_asset_id_in_context: bool,
    ) -> object:
        captured.update(
            {
                "config": config,
                "include_asset_id_in_context": include_asset_id_in_context,
            }
        )
        raise CapturedContextBuilder

    config = _agent_config().model_copy(
        update={"media": AgentMediaConfig(include_asset_id_in_context=True)}
    )
    monkeypatch.setattr(service_module, "Compactor", lambda **kwargs: object())
    monkeypatch.setattr(service_module, "ContextBuilder", capture_context_builder)

    with pytest.raises(CapturedContextBuilder):
        service_module._assemble_agent(
            AgentRuntimeSpec(
                agent_config=config,
                base_prompt="base",
                llm_provider=_NoCallProvider(),
            ),
            shared=cast(
                service_module._SharedAgentPorts,
                SimpleNamespace(
                    asset_store=SimpleNamespace(store=object()),
                    media_fetcher=object(),
                ),
            ),
            runtime_config=cast(
                RuntimeConfig,
                SimpleNamespace(
                    tool_timeout_seconds=30.0,
                    asset=SimpleNamespace(max_file_bytes=10),
                    context_provider_timeout_seconds=1.0,
                    context_provider_max_content_chars=1_000,
                ),
            ),
            tool_authorization=None,
        )

    assert captured == {
        "config": config.compaction,
        "include_asset_id_in_context": True,
    }
