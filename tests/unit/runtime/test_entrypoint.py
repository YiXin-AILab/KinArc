"""多 Agent Runtime Service 启动入口测试。"""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Never
from uuid import UUID

import pytest
from agentsystem.asset.factory import ManagedAssetStore
from agentsystem.config.models import PublicStreamConfig, RuntimeConfig
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.service.agents import AgentAssets
from agentsystem.service.host import ServiceHost
from server import entrypoint
from server.entrypoint import (
    ServiceAssets,
    build_parser,
    load_service_assets,
    serve_runtime,
)
from tests.redis_config import local_redis_config
from tests.runtime_config import TEST_ASSET_LIMITS, TEST_ASSET_STORE


def _write_agent(application_root: Path, agent_id: str = "example") -> None:
    (application_root / "config").mkdir(parents=True)
    (application_root / "prompt").mkdir()
    (application_root / "prompt" / "system.md").write_text(
        "你是测试助手。",
        encoding="utf-8",
    )
    (application_root / "config" / "agent.yaml").write_text(
        (
            f"agent_id: {agent_id}\n"
            "agent_name: 示例 Agent\n"
            "prompt: prompt/system.md\n"
            "outbound:\n"
            "  type: log\n"
            "llm_model:\n"
            "  type: litellm\n"
            "  model: openai/test-model\n"
        ),
        encoding="utf-8",
    )


def test_load_service_assets_combines_global_runtime_and_all_agents(
    tmp_path: Path,
) -> None:
    """入口加载一份全局 RuntimeConfig 和全部 Application Agent。"""
    runtime_path = tmp_path / "config" / "runtime.yaml"
    runtime_path.parent.mkdir()
    runtime_path.write_text(
        (
            "redis:\n  type: standalone\n  host: localhost\n"
            "  key_prefix: service-test\n"
            "asset:\n  max_file_bytes: 1048576\n"
            "asset_store:\n  type: local\n  root: data/assets\n"
        ),
        encoding="utf-8",
    )
    _write_agent(tmp_path / "application" / "a", "agent-a")
    _write_agent(tmp_path / "application" / "b", "agent-b")

    assets = load_service_assets(
        runtime_config_path=runtime_path,
        applications_root=tmp_path / "application",
        project_root=tmp_path,
        environ={},
    )

    assert assets.runtime_config.redis.key_prefix == "service-test"
    assert assets.project_root == tmp_path
    assert assets.applications_root == tmp_path / "application"
    assert [item.agent_config.agent_id for item in assets.agents] == [
        "agent-a",
        "agent-b",
    ]


def test_load_service_assets_rejects_applications_root_outside_project(
    tmp_path: Path,
) -> None:
    """Application 扫描根目录不能逃逸项目边界。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "runtime.yaml").write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="applications-root"):
        load_service_assets(
            runtime_config_path=project / "runtime.yaml",
            applications_root=outside,
            project_root=project,
            environ={},
        )


def test_load_service_assets_resolves_runtime_config_from_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相对 Runtime 配置不受启动进程当前目录影响。"""
    project = tmp_path / "project"
    config = project / "config"
    applications = project / "application"
    config.mkdir(parents=True)
    applications.mkdir()
    (config / "runtime.yaml").write_text(
        (
            "redis:\n  type: standalone\n  host: localhost\n"
            "asset:\n  max_file_bytes: 1048576\n"
            "asset_store:\n  type: local\n  root: data/assets\n"
        ),
        encoding="utf-8",
    )
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    assets = load_service_assets(
        runtime_config_path="config/runtime.yaml",
        applications_root="application",
        project_root=project,
        environ={},
    )

    assert assets.project_root == project


def test_load_service_assets_rejects_runtime_config_outside_project(
    tmp_path: Path,
) -> None:
    """Runtime 配置与 Application 资产共享同一项目边界。"""
    project = tmp_path / "project"
    applications = project / "application"
    applications.mkdir(parents=True)
    outside = tmp_path / "runtime.yaml"
    outside.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime-config"):
        load_service_assets(
            runtime_config_path=outside,
            applications_root=applications,
            project_root=project,
            environ={},
        )


def test_parser_defaults_to_loopback() -> None:
    """默认启动只允许本机访问未鉴权的 Runtime 与管理接口。"""
    assert build_parser().parse_args([]).host == "127.0.0.1"


def test_parser_accepts_explicit_service_options(tmp_path: Path) -> None:
    """用户传入的路径、消费者和监听参数应正确交给启动层。"""
    arguments = build_parser().parse_args(
        [
            "--runtime-config",
            "custom.yaml",
            "--applications-root",
            "apps",
            "--project-root",
            str(tmp_path),
            "--consumer-id",
            "worker-test",
            "--host",
            "0.0.0.0",
            "--port",
            "12345",
        ]
    )

    assert arguments.runtime_config == Path("custom.yaml")
    assert arguments.applications_root == Path("apps")
    assert arguments.project_root == tmp_path
    assert arguments.consumer_id == "worker-test"
    assert arguments.host == "0.0.0.0"
    assert arguments.port == 12345


def test_load_project_environment_reads_project_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 默认从明确的项目根目录读取环境文件。"""
    monkeypatch.delenv("ENTRYPOINT_DOTENV_VALUE", raising=False)
    (tmp_path / ".env").write_text(
        "ENTRYPOINT_DOTENV_VALUE=from-file\n",
        encoding="utf-8",
    )

    entrypoint._load_project_environment(tmp_path)

    assert os.environ["ENTRYPOINT_DOTENV_VALUE"] == "from-file"


def test_load_project_environment_preserves_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署环境变量优先于项目环境文件。"""
    monkeypatch.setenv("ENTRYPOINT_DOTENV_VALUE", "from-process")
    (tmp_path / ".env").write_text(
        "ENTRYPOINT_DOTENV_VALUE=from-file\n",
        encoding="utf-8",
    )

    entrypoint._load_project_environment(tmp_path)

    assert os.environ["ENTRYPOINT_DOTENV_VALUE"] == "from-process"


def test_load_project_environment_allows_missing_file(tmp_path: Path) -> None:
    """环境文件不是生产部署的启动前置条件。"""
    entrypoint._load_project_environment(tmp_path)


def test_load_service_assets_does_not_implicitly_read_dotenv(
    tmp_path: Path,
) -> None:
    """程序化配置 API 不产生隐藏的 dotenv 文件读取。"""
    (tmp_path / ".env").write_text(
        "ENTRYPOINT_DOTENV_VALUE=from-file\n",
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(
        "redis:\n  type: standalone\n  host: ${ENTRYPOINT_DOTENV_VALUE}\n",
        encoding="utf-8",
    )
    (tmp_path / "application").mkdir()

    with pytest.raises(
        ValueError,
        match="ENTRYPOINT_DOTENV_VALUE",
    ):
        load_service_assets(
            runtime_config_path=runtime_path,
            applications_root=tmp_path / "application",
            project_root=tmp_path,
            environ={},
        )


def test_resolve_consumer_id_generates_uuid4_when_absent() -> None:
    """未配置 worker 标识时为当前进程生成 UUID v4。"""
    consumer_id = entrypoint._resolve_consumer_id(None)

    parsed = UUID(consumer_id)
    assert parsed.version == 4
    assert str(parsed) == consumer_id


def test_resolve_consumer_id_preserves_explicit_value() -> None:
    """显式 worker 标识不被默认生成逻辑替换。"""
    assert entrypoint._resolve_consumer_id("worker-1") == "worker-1"


async def test_runtime_create_closes_asset_store_when_redis_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后续基础设施启动失败时，已经创建的 AssetStore 必须关闭。"""
    closed = 0

    async def close() -> None:
        nonlocal closed
        closed += 1

    async def fake_asset_store(*args: object, **kwargs: object) -> ManagedAssetStore:
        del args, kwargs
        return ManagedAssetStore(store=object(), close=close)  # type: ignore[arg-type]

    async def fail_redis(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(
        "agentsystem.service.host.create_asset_store",
        fake_asset_store,
    )
    monkeypatch.setattr(
        "agentsystem.service.host.create_redis_client",
        fail_redis,
    )

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await ServiceHost.create(
            runtime_config=RuntimeConfig(
                redis=local_redis_config(),
                asset=TEST_ASSET_LIMITS,
                asset_store=TEST_ASSET_STORE,
            ),
            project_root=tmp_path,
            agents=(),
            consumer_id="worker",
        )

    assert closed == 1


async def test_serve_runtime_closes_service_when_server_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uvicorn 绑定失败时也关闭已经装配的 ServiceHost。"""
    closed = False

    class FakeService:
        application = object()
        public_stream_config = PublicStreamConfig()

        async def wait_failed(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            nonlocal closed
            closed = True

    service = FakeService()

    class FakeServiceHost:
        @staticmethod
        async def create(**kwargs: object) -> FakeService:
            agents = kwargs["agents"]
            assert isinstance(agents, tuple) and len(agents) == 1
            return service

    class FailingServer:
        def __init__(self, config: object) -> None:
            del config

        async def serve(self) -> None:
            raise RuntimeError("bind failed")

    uvicorn_options: dict[str, object] = {}

    def capture_uvicorn_config(**options: object) -> object:
        uvicorn_options.update(options)
        return object()

    application_root = Path("/tmp/example")
    monkeypatch.setattr("server.entrypoint.ServiceHost", FakeServiceHost)
    monkeypatch.setattr("server.entrypoint.uvicorn.Config", capture_uvicorn_config)
    monkeypatch.setattr("server.entrypoint.uvicorn.Server", FailingServer)
    assets = ServiceAssets(
        runtime_config=RuntimeConfig.model_validate(
            {
                "redis": {"type": "standalone", "host": "localhost"},
                "asset": {"max_file_bytes": 1_048_576},
                "asset_store": {"type": "local"},
            }
        ),
        agents=(_agent_assets(application_root),),
        project_root=Path("/tmp"),
        applications_root=Path("/tmp"),
    )

    with pytest.raises(RuntimeError, match="bind failed"):
        await serve_runtime(
            assets=assets,
            consumer_id="worker",
            host="127.0.0.1",
            port=8000,
            log_level="info",
        )

    assert closed is True
    assert uvicorn_options["log_config"] is None


def test_main_closes_logging_when_runtime_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务装配或启动异常也必须关闭进程级日志文件。"""
    closed = 0

    class FakeParser:
        def parse_args(self) -> SimpleNamespace:
            return SimpleNamespace(
                project_root=tmp_path,
                runtime_config=Path("config/runtime.yaml"),
                applications_root=Path("application"),
                consumer_id="worker",
                host="127.0.0.1",
                port=8000,
                log_level="info",
            )

    class FakeLoggingRuntime:
        def close(self) -> None:
            nonlocal closed
            closed += 1

    assets = ServiceAssets(
        runtime_config=RuntimeConfig.model_validate(
            {
                "redis": {"type": "standalone", "host": "localhost"},
                "asset": {"max_file_bytes": 1_048_576},
                "asset_store": {"type": "local"},
            }
        ),
        agents=(),
        project_root=tmp_path,
        applications_root=tmp_path,
    )

    def fail_serve_runtime(**kwargs: object) -> Never:
        del kwargs
        raise RuntimeError("startup failed")

    monkeypatch.setattr(entrypoint, "build_parser", FakeParser)
    monkeypatch.setattr(entrypoint, "_load_project_environment", lambda _: None)
    monkeypatch.setattr(entrypoint, "load_service_assets", lambda **_: assets)
    monkeypatch.setattr(
        entrypoint,
        "setup_logging",
        lambda *_, **__: FakeLoggingRuntime(),
        raising=False,
    )
    monkeypatch.setattr(entrypoint, "serve_runtime", fail_serve_runtime)

    with pytest.raises(RuntimeError, match="startup failed"):
        entrypoint.main()

    assert closed == 1


@pytest.mark.parametrize("stage", ["app", "config", "server"])
async def test_serve_runtime_closes_host_when_server_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Host 创建后任一 Server 构造阶段失败都必须关闭资源。"""
    closed = 0

    class FakeHost:
        application = object()
        public_stream_config = PublicStreamConfig()

        async def close(self) -> None:
            nonlocal closed
            closed += 1

    async def create(**kwargs: object) -> FakeHost:
        del kwargs
        return FakeHost()

    monkeypatch.setattr("server.entrypoint.ServiceHost.create", create)

    def fail(name: str) -> object:
        def factory(*args: object, **kwargs: object) -> object:
            del args, kwargs
            if stage == name:
                raise RuntimeError(f"{name} failed")
            return object()

        return factory

    monkeypatch.setattr("server.entrypoint.create_runtime_api_app", fail("app"))
    monkeypatch.setattr("server.entrypoint.uvicorn.Config", fail("config"))
    monkeypatch.setattr("server.entrypoint.uvicorn.Server", fail("server"))
    assets = ServiceAssets(
        runtime_config=RuntimeConfig.model_validate(
            {
                "redis": {"type": "standalone", "host": "localhost"},
                "asset": {"max_file_bytes": 1_048_576},
                "asset_store": {"type": "local"},
            }
        ),
        agents=(),
        project_root=Path("/tmp"),
        applications_root=Path("/tmp"),
    )

    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        await serve_runtime(
            assets=assets,
            consumer_id="worker",
            host="127.0.0.1",
            port=8000,
            log_level="info",
        )

    assert closed == 1


async def test_serve_runtime_stops_uvicorn_when_host_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher 顶层失败后入口必须请求 Uvicorn 退出。"""
    closed = 0
    failure = asyncio.Event()

    class FakeHost:
        application = object()
        public_stream_config = PublicStreamConfig()

        async def wait_failed(self) -> None:
            await failure.wait()
            raise RuntimeError("dispatcher failed")

        async def close(self) -> None:
            nonlocal closed
            closed += 1

    async def create(**kwargs: object) -> FakeHost:
        del kwargs
        return FakeHost()

    class FakeServer:
        should_exit = False

        def __init__(self, config: object) -> None:
            del config

        async def serve(self) -> None:
            failure.set()
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr("server.entrypoint.ServiceHost.create", create)
    monkeypatch.setattr(
        "server.entrypoint.create_runtime_api_app",
        lambda *_, **__: object(),
    )
    monkeypatch.setattr("server.entrypoint.uvicorn.Config", lambda **_: object())
    monkeypatch.setattr("server.entrypoint.uvicorn.Server", FakeServer)
    assets = ServiceAssets(
        runtime_config=RuntimeConfig.model_validate(
            {
                "redis": {"type": "standalone", "host": "localhost"},
                "asset": {"max_file_bytes": 1_048_576},
                "asset_store": {"type": "local"},
            }
        ),
        agents=(),
        project_root=Path("/tmp"),
        applications_root=Path("/tmp"),
    )

    with pytest.raises(RuntimeError, match="dispatcher failed"):
        await asyncio.wait_for(
            serve_runtime(
                assets=assets,
                consumer_id="worker",
                host="127.0.0.1",
                port=8000,
                log_level="info",
            ),
            timeout=1,
        )

    assert closed == 1


def _agent_assets(application_root: Path) -> AgentAssets:
    """构造不访问文件系统的入口装配资产。"""
    from agentsystem.config.models import (
        AgentConfig,
        AgentOutboundConfig,
        ModelParamsConfig,
    )

    return AgentAssets(
        agent_config=AgentConfig(
            agent_id="agent",
            agent_name="Agent",
            prompt="prompt.md",
            outbound=AgentOutboundConfig(
                type="log",
            ),
            llm_model=ModelParamsConfig(model="openai/test"),
        ),
        base_prompt="prompt",
        skill_roots=(),
        plugin_sources=(),
        application_root=application_root,
    )


def test_runtime_spec_preserves_discovered_plugin_sources(tmp_path: Path) -> None:
    """Server 入口只转交受控发现结果，不重新扫描或执行 Plugin。"""

    def register(context: object) -> None:
        del context

    source = PluginSource(
        plugin_id="application.agent.audit-plugin",
        origin=PluginOrigin.APPLICATION,
        path=tmp_path / "audit_plugin.py",
        register=register,
    )
    asset = replace(_agent_assets(tmp_path), plugin_sources=(source,))

    spec = entrypoint._to_runtime_spec(asset, project_root=tmp_path)

    assert spec.plugin_sources == (source,)


def test_runtime_spec_appends_global_skill_root_after_application_roots(
    tmp_path: Path,
) -> None:
    """项目级 skills 目录存在时追加为末尾的全局兜底根。"""
    global_root = tmp_path / "skills"
    global_root.mkdir()
    app_root = tmp_path / "application" / "agent"
    app_skills = app_root / "skills"
    app_skills.mkdir(parents=True)
    asset = replace(_agent_assets(app_root), skill_roots=(app_skills,))

    spec = entrypoint._to_runtime_spec(asset, project_root=tmp_path)

    assert spec.skill_roots == (app_skills, global_root)


def test_runtime_spec_keeps_application_skill_roots_first(tmp_path: Path) -> None:
    """多个应用 Skill 根保持原顺序，全局根永远在最后。"""
    (tmp_path / "skills").mkdir()
    app_root = tmp_path / "application" / "agent"
    first = app_root / "skills"
    second = app_root / "more-skills"
    first.mkdir(parents=True)
    second.mkdir()
    asset = replace(_agent_assets(app_root), skill_roots=(first, second))

    spec = entrypoint._to_runtime_spec(asset, project_root=tmp_path)

    assert spec.skill_roots == (first, second, tmp_path / "skills")


def test_runtime_spec_skips_missing_global_skill_root(tmp_path: Path) -> None:
    """项目没有 skills 目录时不追加全局根，保证无全局库也可启动。"""
    app_root = tmp_path / "application" / "agent"
    app_skills = app_root / "skills"
    app_skills.mkdir(parents=True)
    asset = replace(_agent_assets(app_root), skill_roots=(app_skills,))

    spec = entrypoint._to_runtime_spec(asset, project_root=tmp_path)

    assert spec.skill_roots == (app_skills,)


def test_runtime_spec_maps_application_root(tmp_path: Path) -> None:
    """AgentAssets 的 application_root 必须转交给 Runtime Spec。"""
    app_root = tmp_path / "application" / "agent"
    app_root.mkdir(parents=True)
    asset = _agent_assets(app_root)

    spec = entrypoint._to_runtime_spec(asset, project_root=tmp_path)

    assert spec.application_root == app_root


def test_runtime_spec_maps_global_knowledge_root(tmp_path: Path) -> None:
    """项目 knowledge/ 目录存在时注入全局知识库根，缺失时为 None。"""
    app_root = tmp_path / "application" / "agent"
    app_root.mkdir(parents=True)
    asset = _agent_assets(app_root)

    missing = entrypoint._to_runtime_spec(asset, project_root=tmp_path)
    assert missing.knowledge_root is None

    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    spec = entrypoint._to_runtime_spec(asset, project_root=tmp_path)

    assert spec.knowledge_root == knowledge_root
