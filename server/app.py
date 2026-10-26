"""创建 FastAPI 应用并管理 Runtime 生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agentsystem.config.models import PublicStreamConfig
from agentsystem.service.application import ApplicationService
from agentsystem.service.host import ServiceHost
from fastapi import FastAPI
from starlette.types import Lifespan

from server.chat.assets import create_asset_router
from server.chat.routes import create_router
from server.chat.sse import SseDiagnosticSink
from server.errors import register_error_handlers
from server.platform.evaluation.routes import EvaluationDeps, create_evaluation_router
from server.platform.evolution.routes import EvolutionDeps, create_evolution_router
from server.platform.routes import PlatformDeps, create_platform_router


def create_api_app(
    service: ApplicationService,
    *,
    lifespan: Lifespan[FastAPI] | None = None,
    sse_config: PublicStreamConfig | None = None,
    sse_diagnostic_sink: SseDiagnosticSink | None = None,
    platform: PlatformDeps | None = None,
    evolution: EvolutionDeps | None = None,
    evaluation: EvaluationDeps | None = None,
) -> FastAPI:
    """创建不携带全局可变依赖的 Agent Runtime V1 API。

    Args:
        service: 提供 Runtime 业务能力的服务实例。
        lifespan: 可选 ASGI 生命周期管理器。
        sse_config: 可选公开事件流配置。
        sse_diagnostic_sink: 可选 SSE 诊断事件接收器。
        platform: 可选配置平台依赖，为 None 时不挂载该模块路由。
        evolution: 可选自进化依赖，为 None 时不挂载该模块路由。
        evaluation: 可选评测依赖，为 None 时不挂载该模块路由。

    Returns:
        已挂载路由与异常处理器的 FastAPI 应用。
    """
    app = FastAPI(
        title="KinArc API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(
        create_router(
            service,
            sse_config=sse_config,
            sse_diagnostic_sink=sse_diagnostic_sink,
        )
    )
    app.include_router(create_asset_router(service))
    if platform is not None:
        app.include_router(
            create_platform_router(
                store=platform.store,
                catalog=platform.catalog,
                active_agent_ids_provider=platform.active_agent_ids_provider,
            )
        )
    if evolution is not None:
        app.include_router(create_evolution_router(evolution))
    if evaluation is not None:
        app.include_router(create_evaluation_router(evaluation))

    register_error_handlers(app)

    return app


def create_runtime_api_app(
    service: ServiceHost,
    *,
    platform: PlatformDeps | None = None,
    evolution: EvolutionDeps | None = None,
    evaluation: EvaluationDeps | None = None,
) -> FastAPI:
    """创建由 Server lifespan 管理 ServiceHost 的 HTTP 应用。

    Args:
        service: 提供 Runtime 业务能力的服务实例。
        platform: 可选配置平台依赖，为 None 时不挂载该模块路由。
        evolution: 可选自进化依赖，为 None 时不挂载该模块路由。
        evaluation: 可选评测依赖，为 None 时不挂载该模块路由。

    Returns:
        在 ASGI 生命周期内启停 ServiceHost 的 FastAPI 应用。
    """
    return create_api_app(
        service.application,
        lifespan=_service_lifespan(service),
        sse_config=service.public_stream_config,
        platform=platform,
        evolution=evolution,
        evaluation=evaluation,
    )


def _service_lifespan(service: ServiceHost) -> Lifespan[FastAPI]:
    """创建在 ASGI 生命周期内启停 ServiceHost 的 callable。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        try:
            await service.start()
            yield
        finally:
            await service.close()

    return lifespan
