"""从 Application 目录发现并加载隔离的 Agent 静态资产。"""

import logging
import os
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import Field

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import StrictModel
from agentsystem.config.loader import interpolate_environment, load_yaml_config
from agentsystem.config.models import AgentConfig, NonEmptyName
from agentsystem.llm.client import LLMClient
from agentsystem.plugin.discovery import discover_application_plugins
from agentsystem.plugin.models import PluginSource
from agentsystem.runtime.session import AgentSession

LOGGER = logging.getLogger(__name__)


type AsyncClose = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AgentRuntimeBundle:
    """封装已装配 Agent 的运行入口，由 Registry 统一管理关闭。

    Attributes:
        agent_config: 该 Agent 的固定配置。
        session: 可被 Dispatcher 调用的会话执行入口。
        llm_client: 可选模型客户端，供标题生成等应用操作复用。
        close_callback: 可选 Agent 专属资源关闭函数，不负责共享基础设施。
    """

    agent_config: AgentConfig
    session: AgentSession
    llm_client: LLMClient | None = None
    close_callback: AsyncClose | None = None


class AgentRegistry:
    """按 agent_id 提供只读运行目录，并集中调用 Agent 专属关闭回调。

    构造时固定目录顺序，后续查询不修改注册内容；共享连接由 ServiceHost 关闭。
    """

    def __init__(self, bundles: Iterable[AgentRuntimeBundle]) -> None:
        """拒绝重复 ID，并按 agent_id 形成稳定顺序。

        Args:
            bundles: 已完成装配的 Agent 集合。

        Raises:
            ValueError: 多个 Bundle 声明相同 agent_id。
        """
        items: dict[str, AgentRuntimeBundle] = {}
        for bundle in bundles:
            agent_id = bundle.agent_config.agent_id
            if agent_id in items:
                raise ValueError(f"重复 agent_id：{agent_id}")
            items[agent_id] = bundle
        self._items = MappingProxyType(dict(sorted(items.items())))

    def resolve(self, agent_id: str) -> AgentRuntimeBundle | None:
        """查询 Agent 的运行入口。

        Args:
            agent_id: 注册时使用的 Agent 标识。

        Returns:
            对应 Bundle；未知 Agent 返回 None。
        """
        return self._items.get(agent_id)

    def require(self, agent_id: str) -> AgentRuntimeBundle:
        """查询必须存在的 Agent 运行入口。

        Args:
            agent_id: 注册时使用的 Agent 标识。

        Returns:
            对应的 Runtime Bundle。

        Raises:
            ServiceError: 未注册该 Agent，错误码为 agent_not_found。
        """
        bundle = self.resolve(agent_id)
        if bundle is None:
            raise ServiceError(
                kind=ServiceErrorKind.NOT_FOUND,
                code="agent_not_found",
                message="Agent 不存在",
            )
        return bundle

    def agent_ids(self) -> tuple[str, ...]:
        """返回确定性排序的全部已注册 Agent ID。"""
        return tuple(self._items)

    async def close(self) -> None:
        """依次调用全部专属关闭回调，普通异常不跳过后续 Agent。

        Raises:
            ExceptionGroup: 一个或多个关闭回调失败，包含收集到的原异常。

        取消等控制流异常继续传播；本方法本身不记录已关闭标志。
        """
        errors: list[Exception] = []
        for bundle in self._items.values():
            if bundle.close_callback is None:
                continue
            try:
                await bundle.close_callback()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("关闭 Agent Runtime Bundle 失败", errors)


class PromptDocument(StrictModel):
    """校验独立 Prompt YAML 的内容，未知字段由严格模型拒绝。

    Attributes:
        prompt: 非空基础提示词文本。
    """

    prompt: NonEmptyName = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ApplicationRoot:
    """Plugin 解析应用相对路径使用的 Application 根目录。"""

    path: Path


@dataclass(frozen=True, slots=True)
class KnowledgeRoot:
    """Plugin 解析平台级全局知识库资源使用的项目 knowledge/ 根目录。"""

    path: Path


@dataclass(frozen=True, slots=True)
class AgentAssets:
    """保存已验证路径并完成 Plugin 发现的启动资产，不创建运行连接。

    Attributes:
        agent_config: 已解析并校验的 Agent 配置。
        base_prompt: 已读取且完成环境变量插值的基础提示词。
        skill_roots: 当前应用内已解析的 Skill 根目录。
        plugin_sources: 当前应用发现的 Plugin 来源。
        application_root: 已解析的应用绝对路径。
    """

    agent_config: AgentConfig
    base_prompt: str
    skill_roots: tuple[Path, ...]
    plugin_sources: tuple[PluginSource, ...]
    application_root: Path


@dataclass(frozen=True, slots=True)
class _ConfiguredAgent:
    """已完成配置解析、尚未加载文件资产的候选。"""

    application_root: Path
    config: AgentConfig


def discover_agent_assets(
    applications_root: str | Path,
    *,
    environ: Mapping[str, str] = os.environ,
) -> tuple[AgentAssets, ...]:
    """读取应用子目录中的 Agent 配置及静态资产，失败时拒绝部分目录。

    Args:
        applications_root: 包含各应用目录的根路径。
        environ: 配置和提示词插值使用的环境变量映射。

    Returns:
        按配置文件路径稳定排序的完整 Agent 资产集合。

    Raises:
        OSError: 应用根路径不存在或无法读取。
        ValueError: 多个应用声明相同 agent_id。
        ExceptionGroup: 配置或应用资产加载失败，包含原始异常。
    """
    root = Path(applications_root).resolve(strict=True)
    configured, config_errors = _load_agent_configs(root, environ)
    if config_errors:
        raise ExceptionGroup("Agent 静态资产加载失败", config_errors)
    _reject_duplicate_agent_ids(configured)
    assets: list[AgentAssets] = []
    asset_errors: list[Exception] = []
    for candidate in configured:
        try:
            assets.append(_load_agent_assets(candidate, environ))
        except Exception as error:
            asset_errors.append(error)
            _log_load_failure(
                candidate.application_root,
                stage="application_assets",
                error=error,
                agent_id=candidate.config.agent_id,
            )
    if asset_errors:
        raise ExceptionGroup("Agent 静态资产加载失败", asset_errors)
    return tuple(assets)


def _load_agent_configs(
    applications_root: Path,
    environ: Mapping[str, str],
) -> tuple[tuple[_ConfiguredAgent, ...], tuple[Exception, ...]]:
    configured: list[_ConfiguredAgent] = []
    errors: list[Exception] = []
    for config_path in _agent_config_paths(applications_root):
        application_root = config_path.parent.parent.resolve(strict=True)
        try:
            config = load_yaml_config(config_path, AgentConfig, environ=environ)
        except Exception as error:
            errors.append(error)
            _log_load_failure(
                application_root,
                stage="agent_config",
                error=error,
            )
            continue
        configured.append(
            _ConfiguredAgent(application_root=application_root, config=config)
        )
    return tuple(configured), tuple(errors)


def _agent_config_paths(applications_root: Path) -> tuple[Path, ...]:
    paths = (
        child / "config" / "agent.yaml"
        for child in applications_root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )
    return tuple(sorted((path for path in paths if path.is_file()), key=str))


def _reject_duplicate_agent_ids(configured: tuple[_ConfiguredAgent, ...]) -> None:
    counts = Counter(candidate.config.agent_id for candidate in configured)
    duplicates = tuple(
        sorted(agent_id for agent_id, count in counts.items() if count > 1)
    )
    if not duplicates:
        return
    joined = ", ".join(duplicates)
    LOGGER.error("duplicate_agent_id agent_ids=%s", joined)
    raise ValueError(f"重复 agent_id：{joined}")


def _load_agent_assets(
    candidate: _ConfiguredAgent,
    environ: Mapping[str, str],
) -> AgentAssets:
    application_root = candidate.application_root
    config = candidate.config
    prompt_path = _resolve_application_path(
        application_root,
        config.prompt,
        label="Prompt",
    )
    skill_roots = tuple(
        _resolve_application_path(application_root, value, label="Skill root")
        for value in config.skill_roots
    )
    return AgentAssets(
        agent_config=config,
        base_prompt=_load_prompt(prompt_path, environ),
        skill_roots=skill_roots,
        plugin_sources=discover_application_plugins(
            application_root,
            config.agent_id,
        ),
        application_root=application_root,
    )


def _load_prompt(path: Path, environ: Mapping[str, str]) -> str:
    if path.suffix.lower() in {".md", ".txt"}:
        rendered = interpolate_environment(path.read_text(encoding="utf-8"), environ)
        if not isinstance(rendered, str) or not rendered.strip():
            raise ValueError("Prompt 文本不能为空")
        return rendered.strip()
    return load_yaml_config(path, PromptDocument, environ=environ).prompt


def _resolve_application_path(
    application_root: Path,
    value: str,
    *,
    label: str,
) -> Path:
    candidate = (application_root / value).resolve(strict=True)
    try:
        candidate.relative_to(application_root)
    except ValueError as error:
        raise ValueError(f"{label} 必须位于 Application 根目录内") from error
    return candidate


def _log_load_failure(
    application_root: Path,
    *,
    stage: str,
    error: Exception,
    agent_id: str | None = None,
) -> None:
    LOGGER.error(
        "agent_load_failed application=%s agent_id=%s stage=%s error_type=%s",
        application_root,
        agent_id or "unknown",
        stage,
        type(error).__name__,
    )


__all__ = [
    "AgentAssets",
    "AgentRegistry",
    "AgentRuntimeBundle",
    "ApplicationRoot",
    "KnowledgeRoot",
    "discover_agent_assets",
]
