"""影子能力目录（server.platform.catalog）单元测试。"""

from pathlib import Path

import agentsystem
from server.platform.catalog import PlatformCatalog

_PACKAGE_ROOT = Path(agentsystem.__file__).resolve().parent


def _write_application(
    applications_root: Path,
    directory: str,
    agent_id: str,
    plugin_body: str | None = None,
) -> Path:
    """创建最小 Application 目录，可选带一个插件模块。"""
    agent_dir = applications_root / directory
    (agent_dir / "config").mkdir(parents=True)
    (agent_dir / "config" / "agent.yaml").write_text(
        f"agent_id: {agent_id}\n",
        encoding="utf-8",
    )
    if plugin_body is not None:
        plugins = agent_dir / "plugins"
        plugins.mkdir()
        (plugins / "fake_plugin.py").write_text(plugin_body, encoding="utf-8")
    return agent_dir


_FAKE_PLUGIN = '''
"""测试用假插件。"""

from agentsystem.plugin.context import PluginSetupContext


def register(context: PluginSetupContext) -> None:
    """注册一个工具、一个中间件与一个 ContextProvider。"""

    def fake_city_tool(city: str) -> str:
        """按城市查询。"""

        return city

    context.register_tool(fake_city_tool, name="fake_city_tool", description="查询城市")

    from agentsystem.middleware.models import (
        MiddlewareDecision,
        MiddlewareDecisionType,
        MiddlewarePoint,
    )

    async def noop(invocation: object) -> MiddlewareDecision:
        del invocation
        return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

    context.register_middleware(
        noop,
        name="fake-middleware",
        handler_id="noop",
        point=MiddlewarePoint.BEFORE_RUN,
    )

    from agentsystem.context.providers import ContextProvider

    class _Provider:
        provider_name = "fake-provider"

        async def get(self, ctx: object) -> None:
            del ctx
            return None

        async def put(self, ctx: object, message: object) -> None:
            del ctx, message

    context.register_context_provider("fake-provider", lambda: _Provider())
'''

_BROKEN_PLUGIN = '''
"""注册时失败的假插件。"""

from agentsystem.plugin.context import PluginSetupContext


def register(context: PluginSetupContext) -> None:
    """模拟注册期失败。"""

    raise RuntimeError("boom")
'''


def test_catalog_discovers_builtin_and_application_plugins(tmp_path: Path) -> None:
    """影子发现同时枚举内置与 Application 插件贡献。"""
    _write_application(tmp_path, "fake_app", "fake-agent", _FAKE_PLUGIN)

    catalog = PlatformCatalog(
        package_root=_PACKAGE_ROOT,
        applications_root=tmp_path,
    )
    snapshot = catalog.snapshot()

    tools = {entry.name: entry for entry in snapshot.tools}
    assert tools["skill_view"].source == "builtin"
    assert tools["skill_view"].agent_id is None
    assert tools["fake_city_tool"].source == "application"
    assert tools["fake_city_tool"].agent_id == "fake-agent"
    assert tools["fake_city_tool"].description == "查询城市"
    middlewares = {entry.name: entry for entry in snapshot.middlewares}
    assert middlewares["fake-middleware"].source == "application"
    assert middlewares["fake-middleware"].agent_id == "fake-agent"
    providers = {entry.name: entry for entry in snapshot.context_providers}
    assert providers["fake-provider"].source == "application"
    assert providers["fake-provider"].agent_id == "fake-agent"
    assert "current-datetime" in providers
    assert providers["current-datetime"].agent_id is None
    assert snapshot.outbounds == ("log", "stream")


def test_catalog_keeps_same_name_tools_per_agent(tmp_path: Path) -> None:
    """两个 Agent 的同名私有工具各自保留归属，不被合并去重。"""
    _write_application(tmp_path, "app_a", "agent-a", _FAKE_PLUGIN)
    _write_application(tmp_path, "app_b", "agent-b", _FAKE_PLUGIN)

    catalog = PlatformCatalog(
        package_root=_PACKAGE_ROOT,
        applications_root=tmp_path,
    )
    snapshot = catalog.snapshot()

    owners = {
        entry.agent_id for entry in snapshot.tools if entry.name == "fake_city_tool"
    }
    assert owners == {"agent-a", "agent-b"}


def test_catalog_skips_failing_plugin_and_caches_snapshot(tmp_path: Path) -> None:
    """单个插件注册失败不拖垮目录；快照缓存且可刷新。"""
    _write_application(tmp_path, "broken_app", "broken-agent", _BROKEN_PLUGIN)

    catalog = PlatformCatalog(
        package_root=_PACKAGE_ROOT,
        applications_root=tmp_path,
    )
    first = catalog.snapshot()

    assert "skill_view" in {entry.name for entry in first.tools}
    assert catalog.snapshot() is first

    refreshed = catalog.refresh()
    assert refreshed.tools == first.tools


def test_catalog_ignores_agents_with_invalid_config(tmp_path: Path) -> None:
    """agent.yaml 无法解析的目录不参与应用插件发现。"""
    broken = tmp_path / "broken_app" / "config"
    broken.mkdir(parents=True)
    (broken / "agent.yaml").write_text("agent_id: [\n", encoding="utf-8")

    catalog = PlatformCatalog(
        package_root=_PACKAGE_ROOT,
        applications_root=tmp_path,
    )
    snapshot = catalog.snapshot()

    assert "skill_view" in {entry.name for entry in snapshot.tools}


def test_catalog_registers_schedule_tools_when_scheduler_enabled(
    tmp_path: Path,
) -> None:
    """Scheduler 启用时目录包含定时任务工具，禁用时省略。"""
    (tmp_path / "application").mkdir()
    enabled = PlatformCatalog(
        package_root=_PACKAGE_ROOT,
        applications_root=tmp_path / "application",
        scheduler_enabled=True,
    )
    disabled = PlatformCatalog(
        package_root=_PACKAGE_ROOT,
        applications_root=tmp_path / "application",
    )

    enabled_names = {entry.name for entry in enabled.snapshot().tools}
    disabled_names = {entry.name for entry in disabled.snapshot().tools}

    assert "schedule_task_create" in enabled_names
    assert "schedule_task_create" not in disabled_names
