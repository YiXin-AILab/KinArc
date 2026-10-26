"""从受控内置与 Application 目录发现 Agent Plugin。"""

import hashlib
import importlib
import importlib.util
import inspect
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import cast

from agentsystem.plugin.models import PluginOrigin, PluginRegister, PluginSource


def discover_builtin_plugins(package_root: str | Path) -> tuple[PluginSource, ...]:
    """递归发现包内 plugins 目录的直属 Python 模块并执行导入。

    Args:
        package_root: 已存在且可按包名导入的内置包根目录。

    Returns:
        按相对路径排序的插件来源；没有 register 的模块被忽略。

    Raises:
        OSError: 根目录或源文件不可访问。
        ValueError: 路径逃逸、导入失败、register 非同步 callable 或身份重复。
    """
    root = Path(package_root).resolve(strict=True)
    candidates = tuple(
        path
        for plugin_dir in root.rglob("plugins")
        if plugin_dir.is_dir()
        for path in _plugin_files(plugin_dir)
    )
    sources = (
        _load_builtin_source(root, path)
        for path in sorted(
            candidates, key=lambda item: item.relative_to(root).as_posix()
        )
    )
    return _unique_sources(source for source in sources if source is not None)


def discover_application_plugins(
    application_root: str | Path,
    agent_id: str,
) -> tuple[PluginSource, ...]:
    """发现当前 Application 的 plugins 直属模块并执行导入。

    Args:
        application_root: 已存在的 Application 根目录。
        agent_id: 参与插件身份和隔离模块名生成的 Agent 标识。

    Returns:
        按文件相对路径排序的来源元组；无 plugins 目录时为空。
        不递归发现子目录，且忽略没有 register 的模块。

    Raises:
        OSError: 根目录或源文件不可访问。
        ValueError: 路径逃逸、导入失败、register 非同步 callable 或身份重复。
    """
    root = Path(application_root).resolve(strict=True)
    plugin_dir = root / "plugins"
    if not plugin_dir.is_dir():
        return ()
    candidates = sorted(
        _plugin_files(plugin_dir),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    sources = (
        _load_application_source(root, path, agent_id=agent_id) for path in candidates
    )
    return _unique_sources(source for source in sources if source is not None)


def _plugin_files(plugin_dir: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in plugin_dir.iterdir()
        if path.is_file()
        and path.suffix == ".py"
        and path.name != "__init__.py"
        and not path.name.startswith((".", "_"))
    )


def _load_builtin_source(root: Path, path: Path) -> PluginSource | None:
    relative = _safe_relative_path(root, path)
    plugin_id = _builtin_plugin_id(relative)
    module_name = ".".join((root.name, *relative.with_suffix("").parts))
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        raise _discovery_error(
            plugin_id=plugin_id,
            relative=relative,
            stage="import",
            detail=type(error).__name__,
        ) from error
    return _source_from_module(
        module,
        plugin_id=plugin_id,
        origin=PluginOrigin.BUILTIN,
        path=path,
        relative=relative,
    )


def _load_application_source(
    root: Path,
    path: Path,
    *,
    agent_id: str,
) -> PluginSource | None:
    relative = _safe_relative_path(root, path)
    plugin_id = _application_plugin_id(agent_id, relative)
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    module_name = f"_agentsystem_plugin_{_normalize(agent_id)}_{digest}"
    try:
        module = _load_module_from_path(module_name, path)
    except Exception as error:
        raise _discovery_error(
            plugin_id=plugin_id,
            relative=relative,
            stage="import",
            detail=type(error).__name__,
            agent_id=agent_id,
        ) from error
    return _source_from_module(
        module,
        plugin_id=plugin_id,
        origin=PluginOrigin.APPLICATION,
        path=path,
        relative=relative,
        agent_id=agent_id,
    )


def _load_module_from_path(module_name: str, path: Path) -> ModuleType:
    """先登记隔离模块名供导入期使用，执行失败时撤销 sys.modules 条目。"""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法创建 Plugin module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _source_from_module(
    module: ModuleType,
    *,
    plugin_id: str,
    origin: PluginOrigin,
    path: Path,
    relative: Path,
    agent_id: str | None = None,
) -> PluginSource | None:
    if not hasattr(module, "register"):
        return None
    register = module.register
    if not callable(register) or inspect.iscoroutinefunction(register):
        raise _discovery_error(
            plugin_id=plugin_id,
            relative=relative,
            stage="protocol",
            detail="register 必须是同步 callable",
            agent_id=agent_id,
        )
    return PluginSource(
        plugin_id=plugin_id,
        origin=origin,
        path=path,
        register=cast(PluginRegister, register),
    )


def _safe_relative_path(root: Path, path: Path) -> Path:
    """解析符号链接后验证源文件归属，防止受控插件目录中的路径逃逸。"""
    try:
        return path.resolve(strict=True).relative_to(root)
    except ValueError as error:
        raise ValueError(f"Plugin {path.name} 路径逃逸受控根目录") from error


def _builtin_plugin_id(relative: Path) -> str:
    parts = tuple(part for part in relative.with_suffix("").parts if part != "plugins")
    return ".".join(("builtin", *(_normalize(part) for part in parts)))


def _application_plugin_id(agent_id: str, relative: Path) -> str:
    return ".".join(
        (
            "application",
            _normalize(agent_id),
            _normalize(relative.stem),
        )
    )


def _normalize(value: str) -> str:
    return value.replace("_", "-")


def _unique_sources(sources: Iterable[PluginSource]) -> tuple[PluginSource, ...]:
    items = tuple(sources)
    identifiers = tuple(item.plugin_id for item in items)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Plugin ID 重复")
    return items


def _discovery_error(
    *,
    plugin_id: str,
    relative: Path,
    stage: str,
    detail: str,
    agent_id: str | None = None,
) -> ValueError:
    agent = f" agent_id={agent_id}" if agent_id is not None else ""
    return ValueError(
        f"Agent Plugin 发现失败{agent} plugin_id={plugin_id} "
        f"path={relative.as_posix()} stage={stage}: {detail}"
    )
