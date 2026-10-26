"""多 Agent Service 的全局装配与异步生命周期。"""

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis, RedisCluster
from sqlalchemy.ext.asyncio import AsyncEngine

from agentsystem.asset.factory import ManagedAssetStore, create_asset_store
from agentsystem.asset.fetcher import SafeAddressResolver, SafeMediaFetcher
from agentsystem.asset.service import AssetService
from agentsystem.cancellation.redis_bus import RedisCancellationBus
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import JsonValue
from agentsystem.config.models import (
    AgentConfig,
    ClusterRedisConfig,
    PublicStreamConfig,
    RuntimeConfig,
    SqlManagerAdapterConfig,
    SqlScheduleManagerConfig,
)
from agentsystem.context.builder import ContextBuilder
from agentsystem.context.compactor import Compactor
from agentsystem.context.system_prompt import SystemPromptBuilder
from agentsystem.database.client import close_database_engine, create_database_engine
from agentsystem.database.schema import (
    DATABASE_SCHEMA_REVISION,
    assert_database_schema_current,
)
from agentsystem.inbox.redis_inbox import RedisInbox
from agentsystem.knowledge.registry import (
    KNOWLEDGE_PLUGIN_ID,
    build_knowledge_plugin_options,
)
from agentsystem.llm.client import LLMClient, LLMClientConfig
from agentsystem.llm.litellm_provider import LiteLLMProvider
from agentsystem.llm.provider import LLMProvider
from agentsystem.lock.redis_session_lock import RedisSessionLock
from agentsystem.memory.service import MemoryService
from agentsystem.memory.store import MemoryStore
from agentsystem.message.manager import MessageManager
from agentsystem.message.projector import MessageProjector
from agentsystem.middleware.manager import MiddlewareManager
from agentsystem.outbound.event_bus import SessionEventBus
from agentsystem.outbound.projector import LoggingProjectorFactory, PublicProjector
from agentsystem.outbound.projector_registry import (
    PublicProjectorFactory,
    PublicProjectorRegistry,
)
from agentsystem.outbound.stream_projector import StreamProjectorFactory
from agentsystem.plugin.assembler import assemble_agent_capabilities
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.plugin.models import PluginSource
from agentsystem.redis.client import (
    RedisClient,
    close_redis_client,
    create_cluster_node_client,
    create_redis_client,
)
from agentsystem.runtime.loop import AgentLoop
from agentsystem.runtime.session import AgentSession
from agentsystem.runtime.state import AgentState
from agentsystem.schedule.scheduler import ScheduleCoordinator
from agentsystem.service.agents import (
    AgentRegistry,
    AgentRuntimeBundle,
    ApplicationRoot,
    KnowledgeRoot,
)
from agentsystem.service.application import ApplicationService
from agentsystem.service.dispatcher import TaskDispatcher
from agentsystem.skill.index import SkillIndex
from agentsystem.skill.models import SkillRoots
from agentsystem.state.manager import StateManager
from agentsystem.state.projector import StateProjector
from agentsystem.todo.plugins.reminder_middleware import TodoReminder
from agentsystem.todo.service import TodoService
from agentsystem.tool.authorization import ToolAuthorization
from agentsystem.tool.executor import ToolExecutor
from agentsystem.tool.models import ToolDefinition
from agentsystem.trace.otel import OTelTraceRecorder, create_otel_provider
from agentsystem.trace.projector import TraceProjector

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentRuntimeSpec:
    """保存单个 Agent 的装配输入，不创建或关闭运行资源。

    Attributes:
        agent_config: 已校验的 Agent 配置。
        base_prompt: 构造首次 SYSTEM 消息的基础提示词。
        skill_roots: 当前 Agent 可加载的 Skill 根目录。
        external_tools: 代码显式注入的外部工具定义。
        plugin_sources: 该 Agent 专属的 Plugin 来源。
        llm_provider: 可选 Provider 覆盖，未提供时装配 LiteLLMProvider。
    """

    agent_config: AgentConfig
    base_prompt: str
    skill_roots: tuple[Path, ...] = ()
    external_tools: tuple[ToolDefinition, ...] = ()
    plugin_sources: tuple[PluginSource, ...] = ()
    llm_provider: LLMProvider | None = None
    application_root: Path | None = None
    knowledge_root: Path | None = None


@dataclass(frozen=True, slots=True)
class _SharedAgentPorts:
    """汇集各 Agent 共享的装配依赖，避免为每个 Agent 创建连接池。

    此对象仅在装配层流转；适配器具体类型不传入 Runtime 领域模型。
    资源创建和关闭由 ServiceHost 负责，Bundle 仅引用这些共享端口。
    """

    inbox: RedisInbox
    messages: MessageManager
    states: StateManager
    lock: RedisSessionLock
    projectors: PublicProjectorRegistry
    trace_recorder: OTelTraceRecorder
    event_bus: SessionEventBus
    cancellations: RunTaskRegistry
    asset_store: ManagedAssetStore
    media_fetcher: SafeMediaFetcher
    plugin_capabilities: tuple[tuple[type[object], object], ...] = ()
    memory_store: MemoryStore | None = None
    todo_service: TodoService | None = None


@dataclass(frozen=True, slots=True)
class _AssetInfrastructure:
    """保存 Runtime 唯一一组媒体基础设施，由 ServiceHost 成对关闭。

    managed_store 管理存储资源，http_session 承担下载连接；fetcher 与 service
    复用前两者，不创建另一套连接生命周期。
    """

    managed_store: ManagedAssetStore
    http_session: aiohttp.ClientSession
    fetcher: SafeMediaFetcher
    service: AssetService


class ServiceHost:
    """托管多 Agent 对象图、共享基础设施及关键后台任务的进程级宿主。

    create 完成装配，start 启动取消订阅、Dispatcher 和可选调度器；close 在
    统一期限内停止任务并释放自有连接。外部注入的 Redis 与数据库连接不归此处
    关闭。并发 start/close 调用各自共享一次生命周期任务，调用方取消不直接
    取消这些共享任务。

    Attributes:
        application: 供传输层调用的应用服务入口。
    """

    def __init__(
        self,
        *,
        redis_client: RedisClient,
        owns_redis_client: bool,
        inbox: RedisInbox,
        dispatcher: TaskDispatcher,
        cancellation_bus: RedisCancellationBus,
        application: ApplicationService,
        registry: AgentRegistry,
        trace_recorder: OTelTraceRecorder,
        asset_infrastructure: _AssetInfrastructure,
        shutdown_timeout_seconds: float,
        public_stream_config: PublicStreamConfig | None = None,
        event_stream_redis_client: RedisClient | None = None,
        owns_event_stream_redis_client: bool = False,
        cancellation_redis_client: Redis | None = None,
        owns_cancellation_redis_client: bool = False,
        database_engine: AsyncEngine | None = None,
        owns_database_engine: bool = False,
        schedule_coordinator: ScheduleCoordinator | None = None,
    ) -> None:
        """接收已完成装配的对象图并记录资源所有权，尚不启动后台任务。

        Args:
            redis_client: 普通 Runtime Redis 操作使用的共享客户端。
            owns_redis_client: 是否由宿主负责关闭普通 Redis 客户端。
            inbox: Ready Stream 初始化端口。
            dispatcher: 唯一 Ready 消费器。
            cancellation_bus: 用户取消订阅和广播总线。
            application: 对外应用服务入口。
            registry: 已装配 Agent 目录及其专属资源关闭入口。
            trace_recorder: 关闭时刷新并停止的 Trace 记录器。
            asset_infrastructure: 共享媒体资源及其关闭入口。
            shutdown_timeout_seconds: 整个关闭阶段的时间预算。
            public_stream_config: 传输层使用的公开 Stream 配置。
            event_stream_redis_client: 阻塞事件读取使用的独立客户端。
            owns_event_stream_redis_client: 是否关闭事件读取客户端。
            cancellation_redis_client: 可选独立取消订阅客户端。
            owns_cancellation_redis_client: 是否关闭取消订阅客户端。
            database_engine: SQL adapter 共享的可选 Engine。
            owns_database_engine: 是否由宿主关闭 Engine。
            schedule_coordinator: 可选周期任务扫描器。
        """
        self._redis = redis_client
        self._owns_redis = owns_redis_client
        self._inbox = inbox
        self._dispatcher = dispatcher
        self._cancellation_bus = cancellation_bus
        self.application = application
        self._event_stream_redis = event_stream_redis_client
        self._owns_event_stream_redis = owns_event_stream_redis_client
        self._cancellation_redis = cancellation_redis_client
        self._owns_cancellation_redis = owns_cancellation_redis_client
        self._database_engine = database_engine
        self._owns_database_engine = owns_database_engine
        self._schedule_coordinator = schedule_coordinator
        self._registry = registry
        self._trace = trace_recorder
        self._assets = asset_infrastructure
        self._shutdown_timeout = shutdown_timeout_seconds
        self._public_stream_config = public_stream_config or PublicStreamConfig()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._schedule_task: asyncio.Task[None] | None = None
        self._cancellation_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._failure: asyncio.Future[BaseException] | None = None
        self._closed = False

    @property
    def agent_ids(self) -> tuple[str, ...]:
        """返回当前 Service 成功装配的全部 Agent。"""
        return self._registry.agent_ids()

    @property
    def public_stream_config(self) -> PublicStreamConfig:
        """返回 Server transport 使用的 SSE 配置。"""
        return self._public_stream_config

    @classmethod
    async def create(
        cls,
        *,
        runtime_config: RuntimeConfig,
        project_root: Path,
        agents: Sequence[AgentRuntimeSpec],
        consumer_id: str,
        redis_client: RedisClient | None = None,
        event_stream_redis_client: RedisClient | None = None,
        database_engine: AsyncEngine | None = None,
        tracer_provider: TracerProvider | None = None,
        tool_authorization: ToolAuthorization | None = None,
        plugin_sources: Sequence[PluginSource] = (),
        outbound_projectors: Mapping[str, PublicProjectorFactory] | None = None,
    ) -> "ServiceHost":
        """创建共享基础设施并装配所有 Agent，失败时清理本次自有资源。

        Args:
            runtime_config: 已校验的 Runtime 配置。
            project_root: 解析资产存储等相对路径的项目根目录。
            agents: 各 Agent 的静态资产与可选 Provider 覆盖。
            consumer_id: 本节点的非空 Ready 消费身份。
            redis_client: 可选外部普通 Redis 客户端，仍由调用方关闭。
            event_stream_redis_client: 可选外部阻塞读取客户端。
            database_engine: 可选外部 Engine，启用 SQL 时仍检查 schema 版本。
            tracer_provider: 可选 Trace Provider，宿主通过记录器负责其关闭。
            tool_authorization: 工具授权策略，未提供时由执行器使用默认策略。
            plugin_sources: 除内置来源外的共享 Plugin 来源。
            outbound_projectors: 代码注册的额外公开投影器 Factory。

        Returns:
            尚未 start 的宿主实例。

        Raises:
            ValueError: 消费身份为空、Agent 身份重复或 Redis 类型配置不匹配。
            ExceptionGroup: 一个或多个 Agent 装配失败。

        基础设施初始化、schema 校验及失败清理产生的异常向调用方传播。
        """
        if not consumer_id.strip():
            raise ValueError("consumer_id 不能为空")
        _reject_duplicate_specs(agents)
        assets = await _create_asset_infrastructure(runtime_config, project_root)
        try:
            provider = tracer_provider or create_otel_provider(runtime_config.trace)
            trace_recorder = OTelTraceRecorder(provider)
        except BaseException:
            await _close_assets(assets)
            raise
        owns_redis = redis_client is None
        client = redis_client
        owns_event_stream_redis = event_stream_redis_client is None
        event_client = event_stream_redis_client
        cancellation_client: Redis | None = None
        owns_cancellation_redis = False
        engine = database_engine
        owns_database_engine = False
        try:
            if client is None:
                client = await create_redis_client(
                    runtime_config.redis,
                    max_block_ms=runtime_config.dispatcher.ready_block_ms,
                )
            if event_client is None:
                event_client = await create_redis_client(
                    runtime_config.redis,
                    max_connections=(
                        runtime_config.public_stream.max_connections_total
                    ),
                    max_block_ms=runtime_config.public_stream.block_ms,
                )
            if isinstance(client, RedisCluster):
                redis_config = runtime_config.redis
                if not isinstance(redis_config, ClusterRedisConfig):
                    raise ValueError("RedisCluster client 要求 ClusterRedisConfig")
                cancellation_client = await create_cluster_node_client(redis_config)
                owns_cancellation_redis = True
            else:
                cancellation_client = client
            engine, owns_database_engine = await _prepare_database_engine(
                runtime_config,
                engine,
            )
            from agentsystem.service.assembly import assemble_service

            return assemble_service(
                runtime_config=runtime_config,
                agents=agents,
                consumer_id=consumer_id,
                redis_client=client,
                owns_redis_client=owns_redis,
                event_stream_redis_client=event_client,
                owns_event_stream_redis_client=owns_event_stream_redis,
                cancellation_redis_client=cancellation_client,
                owns_cancellation_redis_client=owns_cancellation_redis,
                database_engine=engine,
                owns_database_engine=owns_database_engine,
                trace_recorder=trace_recorder,
                asset_infrastructure=assets,
                tool_authorization=tool_authorization,
                plugin_sources=(
                    *discover_builtin_plugins(Path(__file__).parents[1]),
                    *plugin_sources,
                ),
                outbound_projectors=outbound_projectors,
            )
        except BaseException:
            await _close_failed_create(
                trace_recorder,
                client if owns_redis else None,
                event_client if owns_event_stream_redis else None,
                cancellation_client if owns_cancellation_redis else None,
                engine if owns_database_engine else None,
                assets,
                runtime_config.shutdown_timeout_seconds,
            )
            raise

    async def start(self) -> None:
        """初始化 Agent Stream 并启动关键后台任务，共享同一次启动结果。

        先等待取消订阅就绪，再启动 Dispatcher 和可选调度扫描器。

        Raises:
            RuntimeError: 宿主已关闭，或启动期间进入关闭状态。

        基础设施启动异常继续传播；调用方取消不会取消共享启动任务。
        """
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("ServiceHost 已关闭")
            if self._start_task is None:
                self._start_task = asyncio.create_task(
                    self._start_once(),
                    name="agentsystem-host-start",
                )
            task = self._start_task
        await asyncio.shield(task)

    async def _start_once(self) -> None:
        """先确认取消订阅可用，再初始化并监督 Dispatcher。"""
        try:
            await self._start_cancellation_subscriber()
            await self._inbox.initialize(self.agent_ids)
            async with self._lifecycle_lock:
                if self._closed:
                    raise RuntimeError("ServiceHost 启动期间已关闭")
            self._dispatcher_task = asyncio.create_task(
                self._dispatcher.run(),
                name="agentsystem-dispatcher",
            )
            self._dispatcher_task.add_done_callback(
                lambda task: self._background_done(task, "Dispatcher")
            )
            if self._schedule_coordinator is not None:
                self._schedule_task = asyncio.create_task(
                    self._schedule_coordinator.run(),
                    name="agentsystem-schedule-coordinator",
                )
                self._schedule_task.add_done_callback(
                    lambda task: self._background_done(task, "ScheduleCoordinator")
                )
        except BaseException:
            await self._rollback_cancellation_start()
            raise

    async def _start_cancellation_subscriber(self) -> None:
        """启动 Subscriber，并在 ready 前暴露任何永久退出。"""
        task = asyncio.create_task(
            self._cancellation_bus.run(),
            name="agentsystem-cancellation-subscriber",
        )
        self._cancellation_task = task
        task.add_done_callback(
            lambda done: self._background_done(done, "CancellationSubscriber")
        )
        ready = asyncio.create_task(self._cancellation_bus.wait_ready())
        done, _ = await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
            if task.cancelled():
                raise RuntimeError("CancellationSubscriber 启动时被取消")
            raise task.exception() or RuntimeError("CancellationSubscriber 意外退出")
        await ready
        if task.done():
            raise task.exception() or RuntimeError("CancellationSubscriber 意外退出")

    async def wait_failed(self) -> None:
        """供宿主监督方等待关键后台任务失败，并抛出该失败。

        此方法不会正常返回；正常运行时持续等待，调用方可取消等待。
        后台任务无异常返回或意外被取消时，以 RuntimeError 表示意外退出。
        """
        async with self._lifecycle_lock:
            if self._failure is None:
                self._failure = asyncio.get_running_loop().create_future()
            failure = self._failure
        raise await asyncio.shield(failure)

    def _background_done(self, task: asyncio.Task[None], name: str) -> None:
        """消费关键后台任务结果并向宿主监督方发布非关闭退出。"""
        if task.cancelled():
            error: BaseException = RuntimeError(f"{name} 被意外取消")
        else:
            error = task.exception() or RuntimeError(f"{name} 意外退出")
        if self._closed:
            return
        log_unexpected_exception(
            LOGGER,
            "background_task_failed",
            error,
            operation="service.background",
            task_name=name,
        )
        if self._failure is None:
            self._failure = asyncio.get_running_loop().create_future()
        if not self._failure.done():
            self._failure.set_result(error)

    async def close(self) -> None:
        """共享一次关闭任务，在统一期限内尽力释放全部自有资源。

        先停止调度器、Dispatcher 和取消订阅，再关闭 Agent、媒体、Trace 及
        自有数据库和 Redis。调用方取消不取消正在执行的共享关闭任务。

        Raises:
            BaseExceptionGroup: 收集到资源关闭失败或超时。
        """
        async with self._lifecycle_lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(
                    self._close_once(),
                    name="agentsystem-host-close",
                )
            task = self._close_task
        await asyncio.shield(task)

    async def _close_once(self) -> None:
        """尽力释放全部 Host 自有资源并聚合关闭错误。"""
        errors: list[BaseException] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout
        await self._finish_start_for_close(errors, deadline)
        await _collect_close_error(errors, self._stop_schedule_coordinator, deadline)
        await _collect_close_error(errors, self._stop_dispatcher, deadline)
        await _collect_close_error(errors, self._stop_cancellation, deadline)
        if self._owns_cancellation_redis and self._cancellation_redis is not None:
            cancellation_redis = self._cancellation_redis
            await _collect_close_error(
                errors,
                lambda: close_redis_client(cancellation_redis),
                deadline,
            )
        if self._owns_database_engine and self._database_engine is not None:
            database_engine = self._database_engine
            await _collect_close_error(
                errors,
                lambda: close_database_engine(database_engine),
                deadline,
            )
        await _collect_close_error(errors, self._registry.close, deadline)
        await _collect_close_error(
            errors, lambda: _close_assets(self._assets), deadline
        )
        await _collect_close_error(errors, self._close_trace, deadline)
        if self._owns_redis:
            await _collect_close_error(
                errors,
                lambda: close_redis_client(self._redis),
                deadline,
            )
        if self._owns_event_stream_redis and self._event_stream_redis is not None:
            event_stream_redis = self._event_stream_redis
            await _collect_close_error(
                errors,
                lambda: close_redis_client(event_stream_redis),
                deadline,
            )
        if errors:
            raise BaseExceptionGroup("关闭 ServiceHost 失败", errors)

    async def _close_trace(self) -> None:
        await self._trace.force_flush(self._shutdown_timeout)
        await self._trace.shutdown(self._shutdown_timeout)

    async def _finish_start_for_close(
        self,
        errors: list[BaseException],
        deadline: float,
    ) -> None:
        """等待启动观察关闭状态，超时时取消启动任务。"""
        task = self._start_task
        if task is None or task.done():
            return
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            task.cancel()
            errors.append(TimeoutError("ServiceHost 启动未在关闭期限内收敛"))
            return
        try:
            task.result()
        except RuntimeError as error:
            if "启动期间已关闭" not in str(error):
                errors.append(error)
        except BaseException as error:
            errors.append(error)

    async def _stop_dispatcher(self) -> None:
        task = self._dispatcher_task
        if task is None:
            return
        self._dispatcher.close()
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await task
        finally:
            self._dispatcher_task = None

    async def _stop_schedule_coordinator(self) -> None:
        """停止扫描器并消费其最终结果。"""
        task = self._schedule_task
        if task is None or self._schedule_coordinator is None:
            return
        self._schedule_coordinator.close()
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await task
        finally:
            self._schedule_task = None

    async def _stop_cancellation(self) -> None:
        """停止 Subscriber 并消费其最终结果。"""
        task = self._cancellation_task
        if task is None:
            return
        await self._cancellation_bus.close()
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await task
        finally:
            self._cancellation_task = None

    async def _rollback_cancellation_start(self) -> None:
        """启动后续步骤失败时尽力关闭已经创建的 Subscriber。"""
        task = self._cancellation_task
        if task is None:
            return
        try:
            await self._cancellation_bus.close()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._cancellation_task = None


def _assemble_bundles(
    agents: Sequence[AgentRuntimeSpec],
    *,
    shared: _SharedAgentPorts,
    runtime_config: RuntimeConfig,
    tool_authorization: ToolAuthorization | None,
    plugin_sources: Sequence[PluginSource] = (),
) -> tuple[AgentRuntimeBundle, ...]:
    """逐个装配 Agent 并收集普通异常，任一失败拒绝返回部分可用目录。"""
    bundles: list[AgentRuntimeBundle] = []
    failures: list[Exception] = []
    for spec in agents:
        try:
            bundles.append(
                _assemble_agent(
                    spec,
                    shared=shared,
                    runtime_config=runtime_config,
                    tool_authorization=tool_authorization,
                    plugin_sources=plugin_sources,
                )
            )
        except Exception as error:
            failure = RuntimeError(
                f"Agent 装配失败：{spec.agent_config.agent_id} ({type(error).__name__})"
            )
            failure.__cause__ = error
            failures.append(failure)
    if failures:
        raise ExceptionGroup("Agent Runtime 装配失败", failures)
    return tuple(bundles)


def _assemble_agent(
    spec: AgentRuntimeSpec,
    *,
    shared: _SharedAgentPorts,
    runtime_config: RuntimeConfig,
    tool_authorization: ToolAuthorization | None,
    plugin_sources: Sequence[PluginSource] = (),
) -> AgentRuntimeBundle:
    """创建 Agent 专属工具、中间件、上下文及 Loop 工厂，复用共享存储端口。"""
    config = spec.agent_config
    todo_service = getattr(shared, "todo_service", None)
    todo_reminder = TodoReminder(todo_service) if todo_service is not None else None
    memory_store = getattr(shared, "memory_store", None)
    memory_service = (
        MemoryService(memory_store, config=config.memory)
        if memory_store is not None and config.memory.scopes
        else None
    )
    skills = SkillIndex.load(
        roots=spec.skill_roots,
        enabled_skills=config.enabled_skills,
    )
    capabilities = assemble_agent_capabilities(
        agent_id=config.agent_id,
        sources=(*plugin_sources, *spec.plugin_sources),
        plugin_options=_merge_knowledge_plugin_options(
            config,
            application_root=spec.application_root,
            knowledge_root=spec.knowledge_root,
        ),
        context_provider_types=config.enabled_context_providers,
        skill_index=skills,
        timeout_seconds=runtime_config.context_provider_timeout_seconds,
        max_content_chars=runtime_config.context_provider_max_content_chars,
        external_tools=spec.external_tools,
        plugin_capabilities=(
            *getattr(shared, "plugin_capabilities", ()),
            (SkillRoots, SkillRoots(spec.skill_roots)),
            (MemoryService, memory_service),
            *_application_root_capability(spec.application_root),
            *_knowledge_root_capability(spec.knowledge_root),
            (TodoService, todo_service),
            (TodoReminder, todo_reminder),
        ),
        enabled_tool_patterns=config.enabled_tools,
        enabled_middleware_patterns=config.enabled_middlewares,
    )
    tools = capabilities.tool_registry
    if todo_reminder is not None:
        todo_reminder.bind_tools(tools)
    context_provider_runner = capabilities.context_provider_runner
    middleware = MiddlewareManager(capabilities.middleware_registry)
    provider = spec.llm_provider or LiteLLMProvider(
        api_key=config.llm_model.api_key,
        api_base=config.llm_model.base_url,
        timeout_seconds=config.llm_model.timeout_seconds,
    )
    generation = config.llm_model.to_generation_config()
    client = LLMClient(
        LLMClientConfig(
            provider=provider,
            media_transport=config.media.llm_transport,
            asset_store=shared.asset_store.store,
            media_fetcher=shared.media_fetcher,
            max_file_bytes=runtime_config.asset.max_file_bytes,
        )
    )
    tool_executor = ToolExecutor(
        tools,
        authorization=tool_authorization,
        timeout_seconds=runtime_config.tool_timeout_seconds,
    )
    compaction_config = config.compaction
    compactor = Compactor(
        client=client,
        generation_config=generation,
        context_window=config.llm_model.context_window,
        config=compaction_config,
        include_asset_id_in_context=config.media.include_asset_id_in_context,
        middleware_manager=middleware,
    )
    context_builder = ContextBuilder(
        compaction_config,
        include_asset_id_in_context=config.media.include_asset_id_in_context,
    )

    def build_loop(initial_state: AgentState) -> AgentLoop:
        """为每次执行段创建独立状态容器，复用只读配置与共享执行依赖。"""
        return AgentLoop(
            initial_state=initial_state,
            message_manager=shared.messages,
            context_builder=context_builder,
            context_provider_runner=context_provider_runner,
            compactor=compactor,
            llm_client=client,
            tool_registry=tools,
            tool_executor=tool_executor,
            inbox=shared.inbox,
            middleware_manager=middleware,
            generation_config=generation,
            middleware_llm_retry_limit=(runtime_config.middleware_llm_retry_limit),
        )

    system_prompt_builder = SystemPromptBuilder(
        base_prompt=spec.base_prompt,
        tool_registry=tools,
        skill_index=skills,
        middleware_manager=middleware,
        memory_service=memory_service,
    )
    trace_config = runtime_config.trace
    public_projector = _create_public_projector(
        config,
        shared=shared,
    )
    session = AgentSession(
        message_projector=MessageProjector(shared.messages),
        state_projector=StateProjector(shared.states),
        trace_projector=TraceProjector(
            shared.trace_recorder,
            capture_content=trace_config.capture_content,
        ),
        public_projector=public_projector,
        projector_timeout_seconds=runtime_config.projector_timeout_seconds,
        lock=shared.lock,
        state_manager=shared.states,
        inbox=shared.inbox,
        loop_builder=build_loop,
        messages=shared.messages,
        system_prompt_builder=system_prompt_builder,
        agent_config=config,
        output_mode=config.output_mode,
        cancellation_registry=shared.cancellations,
        middleware_manager=middleware,
        heartbeat_interval_seconds=(
            runtime_config.dispatcher.heartbeat_interval_seconds
        ),
    )
    return AgentRuntimeBundle(agent_config=config, session=session, llm_client=client)


def _application_root_capability(
    application_root: Path | None,
) -> tuple[tuple[type[object], object], ...]:
    """仅在 Spec 携带 Application 根目录时注入对应 capability。"""
    if application_root is None:
        return ()
    return ((ApplicationRoot, ApplicationRoot(application_root)),)


def _knowledge_root_capability(
    knowledge_root: Path | None,
) -> tuple[tuple[type[object], object], ...]:
    """仅在 Spec 携带全局知识库根目录时注入对应 capability。"""
    if knowledge_root is None:
        return ()
    return ((KnowledgeRoot, KnowledgeRoot(knowledge_root)),)


def _merge_knowledge_plugin_options(
    config: AgentConfig,
    *,
    application_root: Path | None,
    knowledge_root: Path | None,
) -> Mapping[str, Mapping[str, JsonValue]]:
    """把 knowledge_bases 引用解析合并为 knowledge Plugin 的 plugin_options。

    knowledge_bases 与 plugin_options 内嵌 knowledge 配置互斥；无引用时
    原样返回 plugin_options。
    """
    if not config.knowledge_bases:
        return config.plugin_options
    if knowledge_root is None:
        raise ValueError(
            f"Agent {config.agent_id} 声明 knowledge_bases 但缺少 knowledge_root"
        )
    if KNOWLEDGE_PLUGIN_ID in config.plugin_options:
        raise ValueError(
            f"Agent {config.agent_id} 的 knowledge_bases 与 plugin_options "
            f"内嵌 {KNOWLEDGE_PLUGIN_ID} 配置互斥"
        )
    options = build_knowledge_plugin_options(
        config.knowledge_bases,
        application_root=application_root,
        knowledge_root=knowledge_root,
    )
    if options is None:
        return config.plugin_options
    return {**config.plugin_options, KNOWLEDGE_PLUGIN_ID: options}


def _create_public_projector(
    config: AgentConfig,
    *,
    shared: _SharedAgentPorts,
) -> PublicProjector:
    """按 Agent outbound type 创建唯一公开投影器。"""
    return shared.projectors.create(
        config.outbound.type,
        config.outbound.options,
    )


def _create_projector_registry(
    *,
    event_bus: SessionEventBus,
    stream_config: PublicStreamConfig,
    custom: Mapping[str, PublicProjectorFactory] | None,
) -> PublicProjectorRegistry:
    """注册内置和应用注入的 PublicProjector factory。"""
    registry = PublicProjectorRegistry()
    registry.register("log", LoggingProjectorFactory())
    registry.register("stream", StreamProjectorFactory(event_bus, stream_config))
    for projector_type, factory in (custom or {}).items():
        registry.register(projector_type, factory)
    registry.freeze()
    return registry


def _reject_duplicate_specs(agents: Sequence[AgentRuntimeSpec]) -> None:
    counts = Counter(spec.agent_config.agent_id for spec in agents)
    duplicates = sorted(agent_id for agent_id, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"重复 agent_id：{', '.join(duplicates)}")


async def _prepare_database_engine(
    runtime_config: RuntimeConfig,
    engine: AsyncEngine | None,
) -> tuple[AsyncEngine | None, bool]:
    """任一 SQL Manager 启用时创建并校验共享应用级 Engine。"""
    uses_database = (
        any(
            isinstance(manager, SqlManagerAdapterConfig)
            for manager in (
                runtime_config.managers.message,
                runtime_config.managers.state,
            )
        )
        or (
            runtime_config.scheduler.enabled
            and isinstance(runtime_config.scheduler.store, SqlScheduleManagerConfig)
        )
        or (runtime_config.memory.enabled and runtime_config.memory.store.type == "db")
        or runtime_config.todo.store.type == "db"
    )
    if not uses_database:
        return engine, False
    database = runtime_config.database
    if database is None:
        raise RuntimeError("SQL Manager 缺少 database 配置")
    owns_engine = engine is None
    if engine is None:
        engine = await create_database_engine(database)
    try:
        await assert_database_schema_current(
            engine,
            expected_revision=DATABASE_SCHEMA_REVISION,
            timeout_seconds=database.operation_timeout_seconds,
        )
    except BaseException:
        if owns_engine:
            await close_database_engine(engine)
        raise
    return engine, owns_engine


async def _close_failed_create(
    trace_recorder: OTelTraceRecorder,
    redis_client: RedisClient | None,
    event_stream_redis_client: RedisClient | None,
    cancellation_redis_client: Redis | None,
    database_engine: AsyncEngine | None,
    assets: _AssetInfrastructure,
    timeout_seconds: float,
) -> None:
    try:
        await trace_recorder.shutdown(timeout_seconds)
    finally:
        try:
            if redis_client is not None:
                await close_redis_client(redis_client)
        finally:
            try:
                if event_stream_redis_client is not None:
                    await close_redis_client(event_stream_redis_client)
            finally:
                try:
                    if cancellation_redis_client is not None:
                        await close_redis_client(cancellation_redis_client)
                finally:
                    try:
                        if database_engine is not None:
                            await close_database_engine(database_engine)
                    finally:
                        await _close_assets(assets)


async def _close_assets(assets: _AssetInfrastructure) -> None:
    """关闭共享下载 Session，并保证 Store 总能收到关闭通知。"""
    try:
        await assets.http_session.close()
    finally:
        await assets.managed_store.close()


async def _collect_close_error(
    errors: list[BaseException],
    operation: Callable[[], Awaitable[None]],
    deadline: float,
) -> None:
    """执行一个关闭操作并保留错误，让后续资源仍可释放。"""
    task = asyncio.ensure_future(operation())
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            task.cancel()
            task.add_done_callback(_consume_task_result)
            raise TimeoutError("资源未在 ServiceHost 关闭期限内释放")
        task.result()
    except BaseException as error:
        errors.append(error)


def _consume_task_result(task: asyncio.Future[None]) -> None:
    """消费超时后台清理的最终结果，避免未检索异常告警。"""
    if not task.cancelled():
        task.exception()


async def _create_asset_infrastructure(
    runtime_config: RuntimeConfig,
    project_root: Path,
) -> _AssetInfrastructure:
    """创建 Runtime 唯一的 Store、SSRF-safe HTTP session 与归档服务。"""
    managed = await create_asset_store(
        runtime_config.asset_store,
        project_root=project_root,
        limits=runtime_config.asset,
    )
    try:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=SafeAddressResolver())
        )
    except BaseException:
        await managed.close()
        raise
    fetcher = SafeMediaFetcher(
        session=session,
        max_file_bytes=runtime_config.asset.max_file_bytes,
        timeout_seconds=runtime_config.asset.download_timeout_seconds,
        allowed_url_schemes=runtime_config.asset.allowed_url_schemes,
    )
    return _AssetInfrastructure(
        managed_store=managed,
        http_session=session,
        fetcher=fetcher,
        service=AssetService(
            store=managed.store,
            fetcher=fetcher,
            max_file_bytes=runtime_config.asset.max_file_bytes,
            upload_timeout_seconds=runtime_config.asset.upload_timeout_seconds,
            allowed_url_schemes=runtime_config.asset.allowed_url_schemes,
        ),
    )


__all__ = ["AgentRuntimeSpec", "ServiceHost"]
