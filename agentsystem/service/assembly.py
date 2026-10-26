"""Service 生产对象图的唯一集中装配入口。"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from agentsystem.cancellation.redis_bus import RedisCancellationBus
from agentsystem.cancellation.registry import RunTaskRegistry
from agentsystem.config.models import (
    LocalScheduleManagerConfig,
    ManagerAdapterConfig,
    RedisManagerAdapterConfig,
    RedisScheduleManagerConfig,
    RuntimeConfig,
    ScheduleManagerConfig,
    SqlManagerAdapterConfig,
    SqlScheduleManagerConfig,
)
from agentsystem.inbox.redis_inbox import RedisInbox
from agentsystem.inbox.redis_keys import InboxRedisKeys
from agentsystem.lock.redis_session_lock import RedisSessionLock
from agentsystem.memory.local.store import LocalMemoryStore
from agentsystem.memory.redis.store import RedisMemoryStore
from agentsystem.memory.sql.store import SqlMemoryStore
from agentsystem.memory.store import MemoryStore
from agentsystem.message.manager import MessageManager
from agentsystem.message.redis.manager import RedisMessageManager
from agentsystem.message.sql.manager import SqlMessageManager
from agentsystem.outbound.projector_registry import (
    PublicProjectorFactory,
)
from agentsystem.outbound.redis_event_bus import RedisSessionEventBus
from agentsystem.plugin.models import PluginSource
from agentsystem.redis.client import RedisClient
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.manager import ScheduleManager
from agentsystem.schedule.redis.manager import RedisScheduleManager
from agentsystem.schedule.scheduler import CronSchedule, ScheduleCoordinator
from agentsystem.schedule.service import ScheduleService
from agentsystem.schedule.sql.manager import SqlScheduleManager
from agentsystem.service.agents import AgentRegistry
from agentsystem.service.application import ApplicationService
from agentsystem.service.dispatcher import TaskDispatcher
from agentsystem.state.manager import StateManager
from agentsystem.state.redis.manager import RedisStateManager
from agentsystem.state.sql.manager import SqlStateManager
from agentsystem.todo.local.store import LocalTodoStore
from agentsystem.todo.redis.store import RedisTodoStore
from agentsystem.todo.service import TodoService
from agentsystem.todo.sql.store import SqlTodoStore
from agentsystem.todo.store import TodoStore
from agentsystem.tool.authorization import ToolAuthorization
from agentsystem.trace.otel import OTelTraceRecorder


def assemble_service(
    *,
    runtime_config: RuntimeConfig,
    agents: Sequence["AgentRuntimeSpec"],
    consumer_id: str,
    redis_client: RedisClient,
    owns_redis_client: bool,
    event_stream_redis_client: RedisClient,
    owns_event_stream_redis_client: bool,
    cancellation_redis_client: Redis,
    owns_cancellation_redis_client: bool,
    database_engine: AsyncEngine | None,
    owns_database_engine: bool,
    trace_recorder: OTelTraceRecorder,
    asset_infrastructure: "_AssetInfrastructure",
    tool_authorization: ToolAuthorization | None,
    plugin_sources: Sequence[PluginSource],
    outbound_projectors: Mapping[str, PublicProjectorFactory] | None,
) -> "ServiceHost":
    """从现有基础设施装配共享端口、Agent 和宿主，不启动后台任务。

    Args:
        runtime_config: 控制 adapter 选择、超时与共享能力的严格配置。
        agents: 各 Agent 的静态装配输入。
        consumer_id: Dispatcher 的 Ready 消费身份。
        redis_client: Runtime 普通操作使用的共享 Redis 客户端。
        owns_redis_client: 宿主是否负责关闭该客户端。
        event_stream_redis_client: 公开事件阻塞读取使用的客户端。
        owns_event_stream_redis_client: 宿主是否关闭阻塞读取客户端。
        cancellation_redis_client: 取消订阅使用的单节点客户端。
        owns_cancellation_redis_client: 宿主是否关闭取消客户端。
        database_engine: SQL adapter 共用的可选 Engine。
        owns_database_engine: 宿主是否负责关闭 Engine。
        trace_recorder: 所有 Agent 共享的 Trace 记录器。
        asset_infrastructure: 已创建的媒体存储、下载及归档服务。
        tool_authorization: 可选工具授权策略。
        plugin_sources: Agent 共享的 Plugin 来源。
        outbound_projectors: 附加公开投影器 Factory 注册表。

    Returns:
        包含应用服务及后台任务依赖、尚未启动的 ServiceHost。

    Raises:
        RuntimeError: 启用 SQL adapter 却缺少 Engine 或数据库超时配置。
        ExceptionGroup: 一个或多个 Agent 装配失败。

    资源创建失败的清理由调用方 ServiceHost.create 负责。
    """
    from agentsystem.service.host import (
        ServiceHost,
        _assemble_bundles,
        _create_projector_registry,
        _SharedAgentPorts,
    )

    prefix = runtime_config.redis.key_prefix
    todo_service = TodoService(
        _create_todo_store(
            runtime_config, redis_client=redis_client, database_engine=database_engine
        )
    )
    inbox = RedisInbox(redis_client, prefix=prefix)
    database = runtime_config.database
    messages = _create_message_manager(
        runtime_config.managers.message,
        redis_client=redis_client,
        redis_prefix=prefix,
        database_engine=database_engine,
        operation_timeout_seconds=(
            database.operation_timeout_seconds if database is not None else None
        ),
    )
    states = _create_state_manager(
        runtime_config.managers.state,
        redis_client=redis_client,
        redis_prefix=prefix,
        database_engine=database_engine,
        operation_timeout_seconds=(
            database.operation_timeout_seconds if database is not None else None
        ),
    )
    coordinator: ScheduleCoordinator | None = None
    schedule_service: ScheduleService | None = None
    scheduler = runtime_config.scheduler
    if scheduler.enabled:
        cron = CronSchedule(scheduler.timezone)
        schedule_manager = _create_schedule_manager(
            scheduler.store,
            cron=cron,
            redis_client=redis_client,
            redis_prefix=prefix,
            redis_operation_timeout_seconds=(
                runtime_config.redis.socket_timeout_seconds
            ),
            database_engine=database_engine,
            database_operation_timeout_seconds=(
                database.operation_timeout_seconds if database is not None else None
            ),
        )
        schedule_service = ScheduleService(
            manager=schedule_manager,
            cron=cron,
            clock=_utc_now,
            id_factory=_new_id,
            queue=inbox,
            operation_timeout_seconds=runtime_config.redis.socket_timeout_seconds,
        )
        coordinator = ScheduleCoordinator(
            manager=schedule_manager,
            cron=cron,
            service=schedule_service,
            clock=_utc_now,
            id_factory=_new_id,
            scan_batch_size=scheduler.scan_batch_size,
            scan_interval_seconds=scheduler.scan_interval_seconds,
        )
    plugin_capabilities: tuple[tuple[type[object], object], ...] = (
        (MessageManager, messages),
        (ScheduleService, schedule_service),
    )
    lock = RedisSessionLock(
        redis_client,
        prefix=prefix,
        inbox_order_key=InboxRedisKeys(prefix).order,
        ttl_ms=max(1, int(runtime_config.dispatcher.lock_ttl_seconds * 1_000)),
    )
    event_bus = RedisSessionEventBus(
        redis_client,
        event_stream_redis_client,
        prefix=prefix,
        config=runtime_config.public_stream,
    )
    projectors = _create_projector_registry(
        event_bus=event_bus,
        stream_config=runtime_config.public_stream,
        custom=outbound_projectors,
    )
    cancellations = RunTaskRegistry()
    cancellation_bus = RedisCancellationBus(
        cancellation_redis_client,
        prefix=prefix,
        registry=cancellations,
    )
    shared = _SharedAgentPorts(
        inbox=inbox,
        messages=messages,
        states=states,
        lock=lock,
        projectors=projectors,
        trace_recorder=trace_recorder,
        event_bus=event_bus,
        cancellations=cancellations,
        asset_store=asset_infrastructure.managed_store,
        media_fetcher=asset_infrastructure.fetcher,
        plugin_capabilities=plugin_capabilities,
        memory_store=_create_memory_store(
            runtime_config, redis_client=redis_client, database_engine=database_engine
        ),
        todo_service=todo_service,
    )
    registry = AgentRegistry(
        _assemble_bundles(
            agents,
            shared=shared,
            runtime_config=runtime_config,
            tool_authorization=tool_authorization,
            plugin_sources=plugin_sources,
        )
    )
    dispatcher = TaskDispatcher(
        inbox=inbox,
        agents=registry,
        agent_ids=registry.agent_ids(),
        consumer_id=consumer_id,
        ready_block_ms=runtime_config.dispatcher.ready_block_ms,
        resume_requeue_delay_ms=runtime_config.dispatcher.resume_requeue_delay_ms,
    )
    application = ApplicationService(
        todo_service=todo_service,
        inbox=inbox,
        states=states,
        agents=registry,
        asset_service=asset_infrastructure.service,
        messages=messages,
        event_bus=event_bus,
        cancellation_bus=cancellation_bus,
        stream_read_batch_size=runtime_config.public_stream.read_batch_size,
        stream_block_ms=runtime_config.public_stream.block_ms,
        public_stream_config=runtime_config.public_stream,
    )
    return ServiceHost(
        redis_client=redis_client,
        owns_redis_client=owns_redis_client,
        inbox=inbox,
        dispatcher=dispatcher,
        cancellation_bus=cancellation_bus,
        application=application,
        registry=registry,
        trace_recorder=trace_recorder,
        asset_infrastructure=asset_infrastructure,
        shutdown_timeout_seconds=runtime_config.shutdown_timeout_seconds,
        public_stream_config=runtime_config.public_stream,
        event_stream_redis_client=event_stream_redis_client,
        owns_event_stream_redis_client=owns_event_stream_redis_client,
        cancellation_redis_client=cancellation_redis_client,
        owns_cancellation_redis_client=owns_cancellation_redis_client,
        database_engine=database_engine,
        owns_database_engine=owns_database_engine,
        schedule_coordinator=coordinator,
    )


def _create_todo_store(
    config: RuntimeConfig,
    *,
    redis_client: RedisClient,
    database_engine: AsyncEngine | None,
) -> TodoStore:
    """创建应用级唯一 TodoStore，不新增连接生命周期。"""
    if config.todo.store.type == "local":
        return LocalTodoStore()
    if config.todo.store.type == "redis":
        return RedisTodoStore(
            redis_client,
            prefix=config.redis.key_prefix,
            timeout_seconds=config.redis.socket_timeout_seconds,
        )
    if database_engine is None or config.database is None:
        raise RuntimeError("MySQL TodoStore 需要应用级 AsyncEngine")
    return SqlTodoStore(
        database_engine, timeout_seconds=config.database.operation_timeout_seconds
    )


def _create_memory_store(
    config: RuntimeConfig,
    *,
    redis_client: RedisClient,
    database_engine: AsyncEngine | None,
) -> MemoryStore | None:
    """为 Application 创建唯一 MemoryStore，复用连接生命周期。"""
    if not config.memory.enabled:
        return None
    if config.memory.store.type == "local":
        return LocalMemoryStore()
    if config.memory.store.type == "redis":
        return RedisMemoryStore(
            redis_client,
            prefix=config.redis.key_prefix,
            timeout_seconds=config.redis.socket_timeout_seconds,
        )
    if database_engine is None or config.database is None:
        raise RuntimeError("MySQL MemoryStore 需要应用级 AsyncEngine")
    return SqlMemoryStore(
        database_engine, timeout_seconds=config.database.operation_timeout_seconds
    )


def _create_message_manager(
    config: ManagerAdapterConfig,
    *,
    redis_client: RedisClient,
    redis_prefix: str,
    database_engine: AsyncEngine | None,
    operation_timeout_seconds: float | None,
) -> MessageManager:
    """从严格 selector 创建唯一 Message 权威 adapter。"""
    if isinstance(config, RedisManagerAdapterConfig):
        return RedisMessageManager(redis_client, prefix=redis_prefix)
    if not isinstance(config, SqlManagerAdapterConfig):
        raise TypeError("未知 MessageManager selector")
    if database_engine is None:
        raise RuntimeError("SQL MessageManager 需要应用级 AsyncEngine")
    if operation_timeout_seconds is None:
        raise RuntimeError("SQL MessageManager 需要数据库 operation timeout")
    return SqlMessageManager(
        database_engine,
        operation_timeout_seconds=operation_timeout_seconds,
    )


def _create_state_manager(
    config: ManagerAdapterConfig,
    *,
    redis_client: RedisClient,
    redis_prefix: str,
    database_engine: AsyncEngine | None,
    operation_timeout_seconds: float | None,
) -> StateManager:
    """从严格 selector 创建唯一 State 权威 adapter。"""
    if isinstance(config, RedisManagerAdapterConfig):
        return RedisStateManager(redis_client, prefix=redis_prefix)
    if not isinstance(config, SqlManagerAdapterConfig):
        raise TypeError("未知 StateManager selector")
    if database_engine is None:
        raise RuntimeError("SQL StateManager 需要应用级 AsyncEngine")
    if operation_timeout_seconds is None:
        raise RuntimeError("SQL StateManager 需要数据库 operation timeout")
    return SqlStateManager(
        database_engine,
        operation_timeout_seconds=operation_timeout_seconds,
    )


def _create_schedule_manager(
    config: ScheduleManagerConfig,
    *,
    cron: CronSchedule,
    redis_client: RedisClient,
    redis_prefix: str,
    redis_operation_timeout_seconds: float,
    database_engine: AsyncEngine | None,
    database_operation_timeout_seconds: float | None,
) -> ScheduleManager:
    """从严格 selector 创建唯一 Schedule 权威 adapter。"""
    if isinstance(config, LocalScheduleManagerConfig):
        return LocalScheduleManager(cron=cron)
    if isinstance(config, RedisScheduleManagerConfig):
        return RedisScheduleManager(
            redis_client,
            prefix=redis_prefix,
            cron=cron,
            operation_timeout_seconds=redis_operation_timeout_seconds,
        )
    if not isinstance(config, SqlScheduleManagerConfig):
        raise TypeError("未知 ScheduleManager selector")
    if database_engine is None or database_operation_timeout_seconds is None:
        raise RuntimeError("MySQL ScheduleManager 需要应用级 AsyncEngine")
    return SqlScheduleManager(
        database_engine,
        cron=cron,
        operation_timeout_seconds=database_operation_timeout_seconds,
    )


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(UTC)


def _new_id() -> str:
    """生成无分隔符的随机领域 ID。"""
    return uuid4().hex


from agentsystem.service.host import (  # noqa: E402  # Break the composition cycle.
    AgentRuntimeSpec,
    ServiceHost,
    _AssetInfrastructure,
)

__all__ = ["assemble_service"]
