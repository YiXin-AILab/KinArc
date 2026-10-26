"""受控 Plugin 文件发现与 import 测试。"""

from pathlib import Path

import pytest
from agentsystem.plugin.discovery import (
    discover_application_plugins,
    discover_builtin_plugins,
)
from agentsystem.plugin.models import PluginOrigin


def _write_plugin(path: Path, body: str = "def register(context):\n    pass\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_builtin_discovery_is_recursive_stable_and_ignores_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builtin 只递归读取命名为 plugins 的目录直属模块并稳定排序。"""
    package_root = tmp_path / "sample_runtime"
    _write_plugin(package_root / "__init__.py", "")
    _write_plugin(package_root / "zeta" / "__init__.py", "")
    _write_plugin(package_root / "zeta" / "plugins" / "__init__.py", "")
    _write_plugin(package_root / "zeta" / "plugins" / "mixed_tool.py")
    _write_plugin(package_root / "alpha" / "__init__.py", "")
    _write_plugin(package_root / "alpha" / "plugins" / "__init__.py", "")
    _write_plugin(package_root / "alpha" / "plugins" / "clock_provider.py")
    _write_plugin(package_root / "alpha" / "plugins" / "helper.py", "VALUE = 1\n")
    _write_plugin(package_root / "alpha" / "plugins" / "_private.py")
    _write_plugin(package_root / "alpha" / "plugins" / "nested" / "ignored_plugin.py")
    monkeypatch.syspath_prepend(str(tmp_path))

    sources = discover_builtin_plugins(package_root)

    assert [source.plugin_id for source in sources] == [
        "builtin.alpha.clock-provider",
        "builtin.zeta.mixed-tool",
    ]
    assert all(source.origin is PluginOrigin.BUILTIN for source in sources)


def test_application_discovery_is_agent_local_and_shallow(tmp_path: Path) -> None:
    """Application Loader 不能读取其他 Agent 或 plugins 子目录。"""
    first_root = tmp_path / "first_agent"
    second_root = tmp_path / "second_agent"
    _write_plugin(first_root / "plugins" / "z_tool.py")
    _write_plugin(first_root / "plugins" / "a_plugin.py")
    _write_plugin(first_root / "plugins" / "helper.py", "VALUE = 1\n")
    _write_plugin(first_root / "plugins" / "nested" / "ignored.py")
    _write_plugin(second_root / "plugins" / "foreign_tool.py")

    sources = discover_application_plugins(first_root, "first_agent")

    assert [source.plugin_id for source in sources] == [
        "application.first-agent.a-plugin",
        "application.first-agent.z-tool",
    ]
    assert all(source.origin is PluginOrigin.APPLICATION for source in sources)
    assert all("foreign" not in source.plugin_id for source in sources)


def test_application_discovery_rejects_symlink_escape(tmp_path: Path) -> None:
    """Plugin 文件的真实路径必须仍位于当前 Application 根。"""
    application_root = tmp_path / "agent"
    outside = tmp_path / "outside_plugin.py"
    _write_plugin(outside)
    plugins = application_root / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "escaped_plugin.py").symlink_to(outside)

    with pytest.raises(ValueError, match=r"escaped_plugin.py.*路径"):
        discover_application_plugins(application_root, "agent")


@pytest.mark.parametrize(
    ("body", "stage"),
    (
        ("register = 42\n", "protocol"),
        ("async def register(context):\n    pass\n", "protocol"),
        ("raise RuntimeError('boom')\n", "import"),
    ),
)
def test_application_discovery_reports_safe_protocol_and_import_errors(
    tmp_path: Path,
    body: str,
    stage: str,
) -> None:
    """发现失败包含 Agent、Plugin 和阶段，但不泄漏绝对根路径。"""
    application_root = tmp_path / "agent"
    _write_plugin(application_root / "plugins" / "broken_plugin.py", body)

    with pytest.raises(ValueError) as captured:
        discover_application_plugins(application_root, "agent")

    message = str(captured.value)
    assert "agent" in message
    assert "application.agent.broken-plugin" in message
    assert "broken_plugin.py" in message
    assert stage in message
    assert str(tmp_path) not in message
