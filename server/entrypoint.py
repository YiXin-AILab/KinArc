"""发现全部 Application Agent 并运行 Runtime Service。"""

import argparse
import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import agentsystem
import uvicorn
from agentsystem.common.logging import setup_logging
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import RuntimeConfig
from agentsystem.service.agents import AgentAssets, discover_agent_assets
from agentsystem.service.host import AgentRuntimeSpec, ServiceHost
from dotenv import load_dotenv

from server.app import create_runtime_api_app
from server.platform.catalog import PlatformCatalog
from server.platform.evaluation.routes import EvaluationDeps
from server.platform.evaluation.runner import EvaluationRunRegistry
from server.platform.evaluation.store import EvaluationStore
from server.platform.evolution.routes import EvolutionDeps
from server.platform.evolution.runner import EvolutionRunRegistry
from server.platform.evolution.store import EvolutionStore
from server.platform.routes import PlatformDeps
from server.platform.store import PlatformStore


@dataclass(frozen=True, slots=True)
class ServiceAssets:
    """启动多 Agent Runtime Service 所需的已校验静态资产。"""

    runtime_config: RuntimeConfig
    agents: tuple[AgentAssets, ...]
    project_root: Path
    applications_root: Path


def load_service_assets(
    *,
    runtime_config_path: str | Path,
    applications_root: str | Path,
    project_root: str | Path,
    environ: Mapping[str, str] = os.environ,
) -> ServiceAssets:
    """加载全局 RuntimeConfig，并发现项目内全部 Application Agent。

    Args:
        runtime_config_path: Runtime YAML 配置路径。
        applications_root: 各 Application 所在的根目录。
        project_root: 项目根目录，用于解析项目资源路径。
        environ: 用于配置环境变量插值的映射。

    Returns:
        已校验的 Runtime 配置与发现到的 Agent 资源。
    """
    root = Path(project_root).resolve(strict=True)
    application_path = _resolve_project_path(
        root,
        applications_root,
        label="applications-root",
    )
    runtime_path = _resolve_project_path(
        root,
        runtime_config_path,
        label="runtime-config",
    )
    runtime_config = load_yaml_config(
        runtime_path,
        RuntimeConfig,
        environ=environ,
    )
    return ServiceAssets(
        runtime_config=runtime_config,
        agents=discover_agent_assets(application_path, environ=environ),
        project_root=root,
        applications_root=application_path,
    )


async def serve_runtime(
    *,
    assets: ServiceAssets,
    consumer_id: str,
    host: str,
    port: int,
    log_level: str,
) -> None:
    """装配 ServiceHost，并在 Uvicorn lifespan 下运行到进程退出。

    Args:
        assets: 已加载的 Runtime 配置与应用资源。
        consumer_id: 当前调度消费者标识。
        host: HTTP 服务监听地址。
        port: HTTP 服务监听端口。
        log_level: Uvicorn 日志级别。
    """
    service = await ServiceHost.create(
        runtime_config=assets.runtime_config,
        project_root=assets.project_root,
        agents=tuple(
            _to_runtime_spec(asset, project_root=assets.project_root)
            for asset in assets.agents
        ),
        consumer_id=consumer_id,
    )
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                app=create_runtime_api_app(
                    service,
                    platform=_create_platform_deps(assets, service),
                    evolution=_create_evolution_deps(assets),
                    evaluation=_create_evaluation_deps(assets),
                ),
                host=host,
                port=port,
                log_config=None,
                log_level=log_level,
            )
        )
        await _serve_until_host_failure(server, service)
    finally:
        await service.close()


async def _serve_until_host_failure(
    server: uvicorn.Server,
    service: ServiceHost,
) -> None:
    """运行 Uvicorn，并在 Dispatcher 顶层失败时请求节点退出。"""
    serve_task = asyncio.create_task(server.serve(), name="agentsystem-uvicorn")
    failure_task = asyncio.create_task(
        service.wait_failed(),
        name="agentsystem-host-failure",
    )
    done, _ = await asyncio.wait(
        {serve_task, failure_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if failure_task in done:
        server.should_exit = True
        try:
            await serve_task
        finally:
            await failure_task
        return
    failure_task.cancel()
    await asyncio.gather(failure_task, return_exceptions=True)
    await serve_task


def main() -> None:
    """解析 Service 参数，发现全部 Agent 并启动唯一 API/Dispatcher。"""
    arguments = build_parser().parse_args()
    root = arguments.project_root.resolve(strict=True)
    _load_project_environment(root)
    assets = load_service_assets(
        runtime_config_path=arguments.runtime_config,
        applications_root=arguments.applications_root,
        project_root=root,
    )
    consumer_id = _resolve_consumer_id(arguments.consumer_id)
    logging_runtime = setup_logging(
        assets.runtime_config.logging,
        project_root=root,
    )
    try:
        asyncio.run(
            serve_runtime(
                assets=assets,
                consumer_id=consumer_id,
                host=arguments.host,
                port=arguments.port,
                log_level=arguments.log_level,
            )
        )
    finally:
        logging_runtime.close()


def build_parser() -> argparse.ArgumentParser:
    """创建多 Agent Runtime Service CLI 参数解析器。"""
    parser = argparse.ArgumentParser(description="启动多 Agent Runtime Service")
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("config/runtime.yaml"),
    )
    parser.add_argument(
        "--applications-root",
        type=Path,
        default=Path("application"),
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--consumer-id")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10060)
    parser.add_argument("--log-level", default="info")
    return parser


def _create_platform_deps(
    assets: ServiceAssets,
    service: ServiceHost,
) -> PlatformDeps:
    """构造平台路由的磁盘存储与影子能力目录依赖。"""
    return PlatformDeps(
        store=PlatformStore(
            project_root=assets.project_root,
            applications_root=assets.applications_root,
        ),
        catalog=PlatformCatalog(
            package_root=Path(agentsystem.__file__).resolve().parent,
            applications_root=assets.applications_root,
            scheduler_enabled=assets.runtime_config.scheduler.enabled,
        ),
        active_agent_ids_provider=lambda: service.agent_ids,
    )


def _create_evolution_deps(assets: ServiceAssets) -> EvolutionDeps:
    """构造自进化任务路由的磁盘存储与 Run 注册表依赖。"""
    store = EvolutionStore(
        project_root=assets.project_root,
        tasks_root=assets.project_root / "evolution_tasks",
    )
    return EvolutionDeps(
        store=store,
        registry=EvolutionRunRegistry(
            project_root=assets.project_root,
            store=store,
            runtime_config=assets.runtime_config,
        ),
    )


def _create_evaluation_deps(assets: ServiceAssets) -> EvaluationDeps:
    """构造通用评测路由的磁盘存储与 Run 注册表依赖。"""
    store = EvaluationStore(tasks_root=assets.project_root / "eval_tasks")
    return EvaluationDeps(
        store=store,
        registry=EvaluationRunRegistry(store=store),
    )


def _resolve_consumer_id(value: str | None) -> str:
    """保留显式 consumer ID，缺省时生成本次进程使用的 UUID v4。"""
    return value if value is not None else str(uuid4())


def _load_project_environment(project_root: Path) -> None:
    """加载项目环境文件，同时保留已存在的部署环境变量。"""
    load_dotenv(dotenv_path=project_root / ".env", override=False)


def _to_runtime_spec(
    asset: AgentAssets,
    *,
    project_root: Path,
) -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        agent_config=asset.agent_config,
        base_prompt=asset.base_prompt,
        skill_roots=_append_global_skill_root(asset.skill_roots, project_root),
        plugin_sources=asset.plugin_sources,
        application_root=asset.application_root,
        knowledge_root=_global_knowledge_root(project_root),
    )


def _global_knowledge_root(project_root: Path) -> Path | None:
    """项目级全局知识库根目录；目录缺失时返回 None。"""
    knowledge_root = project_root / "knowledge"
    if not knowledge_root.is_dir():
        return None
    return knowledge_root


def _append_global_skill_root(
    skill_roots: tuple[Path, ...],
    project_root: Path,
) -> tuple[Path, ...]:
    """在应用根之后追加项目级全局 Skill 根；目录缺失时不追加。"""
    global_root = project_root / "skills"
    if not global_root.is_dir():
        return skill_roots
    return (*skill_roots, global_root)


def _resolve_project_path(root: Path, value: str | Path, *, label: str) -> Path:
    candidate = (root / value).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} 必须位于项目根目录内") from error
    return candidate


if __name__ == "__main__":
    main()
