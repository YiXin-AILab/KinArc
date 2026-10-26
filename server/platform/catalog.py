"""平台能力目录：对 Plugin 体系做影子发现，不触碰运行时实例。"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from agentsystem.context.providers import ContextProviderRunner
from agentsystem.message.manager import MessageManager
from agentsystem.plugin.discovery import (
    discover_application_plugins,
    discover_builtin_plugins,
)
from agentsystem.plugin.loader import load_plugin_contributions
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.schedule.service import ScheduleService
from agentsystem.skill.index import SkillIndex
from agentsystem.skill.models import SkillRoots

LOGGER = logging.getLogger(__name__)

_CATALOG_AGENT_ID = "platform-catalog"
_BUILTIN_OUTBOUNDS = ("log", "stream")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """一个可配置能力的名称、来源、描述与所属 Agent。"""

    name: str
    source: Literal["builtin", "application"]
    description: str = ""
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """一次影子发现得到的能力目录缓存。"""

    tools: tuple[CatalogEntry, ...]
    middlewares: tuple[CatalogEntry, ...]
    context_providers: tuple[CatalogEntry, ...]
    outbounds: tuple[str, ...]


class _CatalogMessageManager:
    """MessageManager 占位实现：Plugin 装配期只检查存在性，从不调用。"""

    def __getattr__(self, name: str) -> object:
        raise NotImplementedError(f"平台目录占位 MessageManager 不支持 {name}")


class _CatalogScheduleService:
    """ScheduleService 占位实现：Plugin 装配期只检查存在性，从不调用。"""

    def __getattr__(self, name: str) -> object:
        raise NotImplementedError(f"平台目录占位 ScheduleService 不支持 {name}")


class PlatformCatalog:
    """进程内构建一次并缓存的 Plugin 能力影子目录。"""

    def __init__(
        self,
        *,
        package_root: Path,
        applications_root: Path,
        scheduler_enabled: bool = False,
    ) -> None:
        """记录发现参数；首次读取快照时才执行插件发现与注册。

        Args:
            package_root: Runtime 包的插件发现根目录。
            applications_root: 各 Application 所在的根目录。
            scheduler_enabled: 是否将定时任务能力纳入发现目录。
        """
        self._package_root = package_root
        self._applications_root = applications_root
        self._scheduler_enabled = scheduler_enabled
        self._snapshot: CatalogSnapshot | None = None

    def snapshot(self) -> CatalogSnapshot:
        """返回缓存快照；首次调用时执行影子发现。"""
        if self._snapshot is None:
            self._snapshot = self._discover()
        return self._snapshot

    def refresh(self) -> CatalogSnapshot:
        """重新执行影子发现并替换缓存。"""
        self._snapshot = self._discover()
        return self._snapshot

    def _discover(self) -> CatalogSnapshot:
        services = _catalog_services(scheduler_enabled=self._scheduler_enabled)
        tools: dict[tuple[str, str | None], CatalogEntry] = {}
        middlewares: dict[tuple[str, str | None], CatalogEntry] = {}
        providers: dict[tuple[str, str | None], CatalogEntry] = {}
        for source, owner in self._discover_sources():
            label: Literal["builtin", "application"] = (
                "builtin" if source.origin is PluginOrigin.BUILTIN else "application"
            )
            try:
                contributions = load_plugin_contributions(
                    (source,),
                    agent_id=_CATALOG_AGENT_ID,
                    options={},
                    services=services,
                )
            except ValueError as error:
                LOGGER.warning(
                    "platform_catalog_plugin_failed plugin_id=%s error=%s",
                    source.plugin_id,
                    error,
                )
                continue
            for contribution in contributions:
                for tool in contribution.tools:
                    tools.setdefault(
                        (tool.name, owner),
                        CatalogEntry(
                            name=tool.name,
                            source=label,
                            description=tool.description,
                            agent_id=owner,
                        ),
                    )
                for middleware in contribution.middlewares:
                    middlewares.setdefault(
                        (middleware.name, owner),
                        CatalogEntry(
                            name=middleware.name,
                            source=label,
                            agent_id=owner,
                        ),
                    )
                for provider in contribution.context_providers:
                    providers.setdefault(
                        (provider.provider_type, owner),
                        CatalogEntry(
                            name=provider.provider_type,
                            source=label,
                            agent_id=owner,
                        ),
                    )
        return CatalogSnapshot(
            tools=tuple(tools[key] for key in sorted(tools, key=_entry_sort_key)),
            middlewares=tuple(
                middlewares[key] for key in sorted(middlewares, key=_entry_sort_key)
            ),
            context_providers=tuple(
                providers[key] for key in sorted(providers, key=_entry_sort_key)
            ),
            outbounds=_BUILTIN_OUTBOUNDS,
        )

    def _discover_sources(self) -> tuple[tuple[PluginSource, str | None], ...]:
        """返回全部插件来源及其所属 agent_id（builtin 为 None）。"""
        sources: list[tuple[PluginSource, str | None]] = [
            (source, None) for source in discover_builtin_plugins(self._package_root)
        ]
        for agent_dir, agent_id in _iter_application_agents(self._applications_root):
            try:
                sources.extend(
                    (source, agent_id)
                    for source in discover_application_plugins(agent_dir, agent_id)
                )
            except ValueError as error:
                LOGGER.warning(
                    "platform_catalog_discovery_failed application=%s error=%s",
                    agent_dir.name,
                    error,
                )
        return tuple(sources)


def _entry_sort_key(key: tuple[str, str | None]) -> tuple[str, str]:
    """按能力名与所属 agent_id 排序；builtin（None）排在同名应用能力之前。"""
    return key[0], key[1] or ""


def _iter_application_agents(applications_root: Path) -> Iterator[tuple[Path, str]]:
    """按目录顺序产出（Application 目录, agent_id）；无效配置跳过。"""
    if not applications_root.is_dir():
        return
    for child in sorted(applications_root.iterdir()):
        config_path = child / "config" / "agent.yaml"
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or not config_path.is_file()
        ):
            continue
        agent_id = _read_agent_id(config_path)
        if agent_id is not None:
            yield child, agent_id


def _read_agent_id(config_path: Path) -> str | None:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    agent_id = raw.get("agent_id")
    return agent_id if isinstance(agent_id, str) and agent_id else None


def _catalog_services(*, scheduler_enabled: bool) -> PluginSetupServices:
    """构造与运行时装配一致的只读 capability 表。"""
    services = PluginSetupServices()
    services.register(
        ContextProviderRunner,
        ContextProviderRunner(timeout_seconds=5.0),
    )
    services.register(SkillIndex, SkillIndex({}))
    services.register(SkillRoots, SkillRoots(()))
    # Protocol 类仅作为运行时 capability key，占位实例在装配期不会被调用。
    services.register(
        MessageManager,  # type: ignore[type-abstract]
        cast(MessageManager, _CatalogMessageManager()),
    )
    if scheduler_enabled:
        services.register(
            ScheduleService,
            cast(ScheduleService, _CatalogScheduleService()),
        )
    services.freeze()
    return services


__all__ = ["CatalogEntry", "CatalogSnapshot", "PlatformCatalog"]
