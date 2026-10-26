"""Agent 配置平台的纯文件 IO 存储层（无 HTTP 依赖）。"""

from __future__ import annotations

import re
import shutil
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import JsonValue, OutputMode
from agentsystem.config.models import AgentConfig
from agentsystem.skill.models import SkillSource
from agentsystem.skill.validator import (
    _FRONTMATTER_RE,
    NAME_RE,
    SkillValidator,
    _parse_frontmatter,
)
from pydantic import ValidationError

type AgentStatus = Literal["active", "pending", "invalid"]
type SkillScope = Literal["global", "private"]

_AGENT_DIR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")
_ALLOWED_RESOURCE_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml"})
_PROMPT_SUFFIXES = frozenset({".md", ".txt"})
_SECRET_MASK = "********"
_API_KEY_PATH: tuple[str, ...] = ("llm_model", "api_key")


@dataclass(frozen=True, slots=True)
class PlatformStore:
    """平台磁盘存储使用的项目根与 Application 根路径。"""

    project_root: Path
    applications_root: Path


@dataclass(frozen=True, slots=True)
class AgentSummary:
    """磁盘上单个 Agent 配置的摘要与生效状态。"""

    agent_id: str
    agent_name: str
    description: str | None
    output_mode: OutputMode
    model: str | None
    tool_count: int
    skill_count: int
    status: AgentStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class AgentDocument:
    """磁盘上单个 Agent 的配置原文、Prompt 与状态。"""

    config: dict[str, JsonValue]
    prompt_text: str
    status: AgentStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """磁盘上单个 Skill 的摘要、校验状态与可见性。"""

    name: str
    description: str
    source: SkillScope
    metadata: dict[str, str]
    resources: tuple[str, ...]
    valid: bool
    error: str | None
    referenced_by: tuple[str, ...]
    overridden_by: tuple[str, ...]
    overrides_global: bool


@dataclass(frozen=True, slots=True)
class SkillListView:
    """单 scope 的 Skill 列表及全局库可见性视图。"""

    skills: tuple[SkillSummary, ...]
    global_skills: tuple[SkillSummary, ...] | None


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """单个 Skill 的 frontmatter 字段与 Markdown 正文。"""

    name: str
    description: str
    metadata: dict[str, str]
    allowed_tools: tuple[str, ...]
    content: str
    resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillResourceItem:
    """单个 Skill 资源文件的相对路径与字节数。"""

    path: str
    size: int


# ---------------------------------------------------------------------------
# Agent 配置
# ---------------------------------------------------------------------------


def list_disk_agents(
    applications_root: Path,
    active_agent_ids: Collection[str],
) -> tuple[AgentSummary, ...]:
    """扫描磁盘中的 Agent 配置，无效配置仍以 invalid 状态列出。

    Args:
        applications_root: 存放各 Application 的根目录。
        active_agent_ids: 当前 Runtime 已加载的 Agent 标识。

    Returns:
        磁盘配置摘要；合法配置根据是否已加载标记为 active 或 pending。
    """
    summaries: list[AgentSummary] = []
    for config_path in _agent_config_paths(applications_root.resolve()):
        agent_dir = config_path.parent.parent
        raw, error = _load_raw_config(config_path)
        config: AgentConfig | None = None
        if error is None and raw is not None:
            try:
                config = AgentConfig.model_validate(raw)
            except ValidationError as exc:
                error = _format_validation_error(exc)
        if config is None:
            agent_id = _raw_agent_id(raw) or agent_dir.name
            summaries.append(
                AgentSummary(
                    agent_id=agent_id,
                    agent_name="",
                    description=None,
                    output_mode=OutputMode.COMPLETE,
                    model=None,
                    tool_count=0,
                    skill_count=0,
                    status="invalid",
                    error=error or "Agent 配置无效",
                )
            )
            continue
        summaries.append(summarize_agent(config, active_agent_ids))
    return tuple(summaries)


def summarize_agent(
    config: AgentConfig,
    active_agent_ids: Collection[str],
) -> AgentSummary:
    """从已校验配置构造摘要；不插值 `${VAR}` 占位符。

    Args:
        config: 当前组件使用的配置。
        active_agent_ids: Runtime 已加载的 Agent 标识。

    Returns:
        配置摘要及 active 或 pending 状态。
    """
    description = config.metadata.get("description")
    return AgentSummary(
        agent_id=config.agent_id,
        agent_name=config.agent_name,
        description=description if isinstance(description, str) else None,
        output_mode=config.output_mode,
        model=config.llm_model.model,
        tool_count=len(config.enabled_tools or ()),
        skill_count=len(config.enabled_skills),
        status="active" if config.agent_id in active_agent_ids else "pending",
        error=None,
    )


def read_agent(
    applications_root: Path,
    agent_id: str,
    active_agent_ids: Collection[str],
) -> AgentDocument:
    """读取单个 Agent 的配置与提示词，不插值环境变量，并遮蔽已有明文密钥。

    Args:
        applications_root: 存放各 Application 的根目录。
        agent_id: 目标 Agent 标识。
        active_agent_ids: 当前 Runtime 已加载的 Agent 标识。

    Returns:
        配置、提示词及生效状态；配置无效时附带校验错误。

    Raises:
        ServiceError: Agent 不存在。
        OSError: 提示词文件不可读。
    """
    agent_dir = _locate_agent_dir(applications_root, agent_id)
    raw, error = _load_raw_config(agent_dir / "config" / "agent.yaml")
    config: AgentConfig | None = None
    if error is None and raw is not None:
        try:
            config = AgentConfig.model_validate(raw)
        except ValidationError as exc:
            error = _format_validation_error(exc)
    prompt_text = ""
    if config is not None:
        prompt_path = _prompt_path(agent_dir, config.prompt)
        if prompt_path is None:
            error = "Prompt 路径越界"
        elif prompt_path.is_file():
            prompt_text = prompt_path.read_text(encoding="utf-8")
    status: AgentStatus
    if config is None:
        status = "invalid"
    else:
        status = "active" if config.agent_id in active_agent_ids else "pending"
    return AgentDocument(
        config=_mask_api_key(raw) if raw else {},
        prompt_text=prompt_text,
        status=status,
        error=error,
    )


def create_agent(
    applications_root: Path,
    config: Mapping[str, JsonValue],
    prompt_text: str,
) -> AgentConfig:
    """校验并写入新 Agent，agent_id 作为目录名且必须全局唯一。

    Args:
        applications_root: 存放各 Application 的根目录。
        config: 未插值的配置原文，密钥必须使用环境变量占位符。
        prompt_text: 要写入配置指定路径的提示词正文。

    Returns:
        校验后的 Agent 配置；磁盘文件已写入，不代表 Runtime 已加载。

    Raises:
        ServiceError: 配置、提示词或路径无效，或 Agent 已存在。
        OSError: 目录或文件无法写入。
    """
    parsed = _validate_agent_config(config)
    _validate_prompt_text(prompt_text)
    agent_id = parsed.agent_id
    if _AGENT_DIR_RE.fullmatch(agent_id) is None:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_agent_id",
            message="agent_id 只能包含字母、数字、-、_ 且不超过 64 字符",
        )
    root = applications_root.resolve()
    if agent_id in _known_agent_ids(root) or (root / agent_id).exists():
        raise ServiceError(
            kind=ServiceErrorKind.CONFLICT,
            code="agent_already_exists",
            message="Agent 已存在",
        )
    _write_agent_files(root / agent_id, parsed, config, prompt_text)
    return parsed


def update_agent(
    applications_root: Path,
    agent_id: str,
    config: Mapping[str, JsonValue],
    prompt_text: str,
) -> AgentConfig:
    """覆盖已有 Agent 的配置与提示词，保持 agent_id 不变。

    Args:
        applications_root: 存放各 Application 的根目录。
        agent_id: 目标 Agent 标识。
        config: 未插值的配置原文，密钥必须使用环境变量占位符。
        prompt_text: 要写入配置指定路径的提示词正文。

    Returns:
        校验后的 Agent 配置；仅更新磁盘文件，不重载 Runtime。

    Raises:
        ServiceError: Agent 不存在、标识被修改或配置、提示词、路径无效。
        OSError: 文件无法写入。
    """
    parsed = _validate_agent_config(config)
    if parsed.agent_id != agent_id:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="agent_id_immutable",
            message="agent_id 创建后不可修改",
        )
    _validate_prompt_text(prompt_text)
    agent_dir = _locate_agent_dir(applications_root, agent_id)
    _write_agent_files(agent_dir, parsed, config, prompt_text)
    return parsed


def delete_agent(applications_root: Path, agent_id: str) -> None:
    """删除 Agent 所在的整个 Application 目录，不卸载 Runtime 中已加载的实例。

    Args:
        applications_root: 存放各 Application 的根目录。
        agent_id: 目标 Agent 标识。

    Raises:
        ServiceError: Agent 不存在，或目录越出 Application 根目录。
        OSError: 目录无法删除。
    """
    root = applications_root.resolve()
    agent_dir = _locate_agent_dir(root, agent_id).resolve()
    if agent_dir == root or not agent_dir.is_relative_to(root):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_agent_dir",
            message="Agent 目录越界",
        )
    shutil.rmtree(agent_dir)


def find_agent_dir(applications_root: Path, agent_id: str) -> Path | None:
    """按 agent_id 定位 Application 目录；无效配置回退按目录名匹配。

    Args:
        applications_root: 各 Application 所在的根目录。
        agent_id: 目标 Agent 标识。

    Returns:
        匹配的 Application 目录；未找到时返回 None。
    """
    root = applications_root.resolve()
    if not root.is_dir():
        return None
    for config_path in _agent_config_paths(root):
        agent_dir = config_path.parent.parent
        raw, _ = _load_raw_config(config_path)
        if raw is not None and raw.get("agent_id") == agent_id:
            return agent_dir
        if raw is None and agent_dir.name == agent_id:
            return agent_dir
    return None


# ---------------------------------------------------------------------------
# Skill 资源存储
# ---------------------------------------------------------------------------


def list_skills(store: PlatformStore, scope: str) -> SkillListView:
    """列出全局库或单个 Agent 私有库的 Skill，并附带可见性信息。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。

    Returns:
        当前范围的 Skill 摘要及全局库可见性视图。
    """
    references = _skill_reference_map(store.applications_root)
    private_map = _private_skill_map(store.applications_root)
    global_root = store.project_root / "skills"
    if scope == "global":
        return SkillListView(
            skills=tuple(
                describe_skill(
                    store,
                    "global",
                    name,
                    references=references,
                    private_map=private_map,
                )
                for name in _skill_dir_names(global_root)
            ),
            global_skills=None,
        )
    agent_dir = _locate_agent_dir(store.applications_root, scope)
    private_names = _skill_dir_names(agent_dir / "skills")
    global_names = _skill_dir_names(global_root)
    return SkillListView(
        skills=tuple(
            describe_skill(
                store,
                scope,
                name,
                references=references,
                private_map=private_map,
            )
            for name in private_names
        ),
        global_skills=tuple(
            describe_skill(
                store,
                "global",
                name,
                references=references,
                private_map=private_map,
                scope_hint=scope,
            )
            for name in global_names
        ),
    )


def describe_skill(
    store: PlatformStore,
    scope: str,
    name: str,
    *,
    references: Mapping[str, list[str]] | None = None,
    private_map: Mapping[str, list[str]] | None = None,
    scope_hint: str | None = None,
) -> SkillSummary:
    """构造单个磁盘 Skill 的校验状态与可见性摘要。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        references: 可选 Skill 引用缓存，映射为引用该 Skill 的 Agent 列表。
        private_map: 可选私有 Skill 归属缓存，映射为拥有同名 Skill 的 Agent 列表。
        scope_hint: 可选 Agent 标识，用于限定全局 Skill 的同名覆盖信息。

    Returns:
        Skill 校验状态、归属及引用信息。
    """
    root = _existing_skill_root(store, scope, name)
    source = SkillSource.GLOBAL if scope == "global" else SkillSource.APPLICATION
    label: SkillScope = "global" if scope == "global" else "private"
    referenced_map = (
        dict(references)
        if references is not None
        else _skill_reference_map(store.applications_root)
    )
    owners_map = (
        dict(private_map)
        if private_map is not None
        else _private_skill_map(store.applications_root)
    )
    referenced_by = tuple(sorted(referenced_map.get(name, ())))
    if scope == "global":
        overridden_by = tuple(sorted(owners_map.get(name, ())))
        if scope_hint is not None:
            overridden_by = (scope_hint,) if scope_hint in overridden_by else ()
        overrides_global = False
    else:
        overridden_by = ()
        overrides_global = (store.project_root / "skills" / name).is_dir()
        referenced_by = tuple(item for item in referenced_by if item == scope)
    try:
        result = SkillValidator().validate(root, source)
    except ValueError as error:
        description, metadata = _frontmatter_fallback(root)
        return SkillSummary(
            name=root.name,
            description=description,
            source=label,
            metadata=metadata,
            resources=_list_resource_paths(root),
            valid=False,
            error=str(error),
            referenced_by=referenced_by,
            overridden_by=overridden_by,
            overrides_global=overrides_global,
        )
    return SkillSummary(
        name=result.name,
        description=result.description,
        source=label,
        metadata={str(key): str(value) for key, value in result.metadata.items()},
        resources=tuple(item for item in result.resources if item != "SKILL.md"),
        valid=True,
        error=None,
        referenced_by=referenced_by,
        overridden_by=overridden_by,
        overrides_global=overrides_global,
    )


def read_skill(store: PlatformStore, scope: str, name: str) -> SkillDocument:
    """读取单个 Skill 的 frontmatter 字段与 Markdown 正文。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。

    Returns:
        Skill 元数据与 Markdown 正文。
    """
    root = _existing_skill_root(store, scope, name)
    try:
        text = (root / "SKILL.md").read_text(encoding="utf-8")
    except OSError as error:
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="skill_not_found",
            message="Skill 缺少 SKILL.md",
        ) from error
    try:
        frontmatter = _parse_frontmatter(text)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_skill_frontmatter",
            message=str(error),
        ) from error
    description = frontmatter.get("description")
    return SkillDocument(
        name=root.name,
        description=description if isinstance(description, str) else "",
        metadata=_frontmatter_metadata(frontmatter.get("metadata")),
        allowed_tools=_frontmatter_allowed_tools(frontmatter.get("allowed-tools")),
        content=_skill_body(text),
        resources=_list_resource_paths(root),
    )


def create_skill(
    store: PlatformStore,
    *,
    scope: str,
    name: str,
    description: str,
    content: str,
    metadata: Mapping[str, str] = {},
    allowed_tools: Collection[str] = (),
) -> None:
    """创建全局或 Agent 私有 Skill；首个私有 Skill 自动补 skill_roots。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        description: 资源或业务内容的描述。
        content: 正文文本。
        metadata: Skill 的附加元数据。
        allowed_tools: Skill 声明的工具名称，仅作为资产字段保存。
    """
    agent_dir: Path | None = None
    if scope == "global":
        _validate_skill_name(name)
        base = store.project_root / "skills"
    else:
        agent_dir = _locate_agent_dir(store.applications_root, scope)
        _validate_skill_name(name)
        base = agent_dir / "skills"
    _validate_skill_fields(description)
    root = base / name
    if root.exists():
        raise ServiceError(
            kind=ServiceErrorKind.CONFLICT,
            code="skill_already_exists",
            message="Skill 已存在",
        )
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        _render_skill(
            name,
            description=description,
            content=content,
            metadata=metadata,
            allowed_tools=allowed_tools,
        ),
        encoding="utf-8",
    )
    if agent_dir is not None:
        _ensure_private_skill_root(agent_dir)


def update_skill(
    store: PlatformStore,
    scope: str,
    name: str,
    *,
    description: str,
    content: str,
    metadata: Mapping[str, str] = {},
    allowed_tools: Collection[str] = (),
) -> None:
    """重写已存在 Skill 的 SKILL.md；目录名与 name 保持不变。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        description: 资源或业务内容的描述。
        content: 正文文本。
        metadata: Skill 的附加元数据。
        allowed_tools: Skill 声明的工具名称，仅作为资产字段保存。
    """
    root = _existing_skill_root(store, scope, name)
    _validate_skill_fields(description)
    (root / "SKILL.md").write_text(
        _render_skill(
            root.name,
            description=description,
            content=content,
            metadata=metadata,
            allowed_tools=allowed_tools,
        ),
        encoding="utf-8",
    )


def delete_skill(store: PlatformStore, scope: str, name: str) -> None:
    """删除整个 Skill 目录，删除前确认目录未越出所在库根。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
    """
    root = _existing_skill_root(store, scope, name)
    base = root.parent.resolve()
    resolved = root.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_skill_dir",
            message="Skill 目录越界",
        )
    shutil.rmtree(resolved)


def list_skill_names(store: PlatformStore, scope: str) -> tuple[str, ...]:
    """返回 scope 下磁盘 Skill 名称；未知 Agent scope 返回空集。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。

    Returns:
        按名称排序的 Skill 名称元组；未知 Agent 范围返回空元组。
    """
    if scope == "global":
        return _skill_dir_names(store.project_root / "skills")
    agent_dir = find_agent_dir(store.applications_root, scope)
    if agent_dir is None:
        return ()
    return _skill_dir_names(agent_dir / "skills")


def skill_references(applications_root: Path, name: str) -> tuple[str, ...]:
    """返回 enabled_skills 引用了指定名称的全部 agent_id。

    Args:
        applications_root: 各 Application 所在的根目录。
        name: 目标资源名称。

    Returns:
        引用该 Skill 的 Agent 标识元组。
    """
    return tuple(sorted(_skill_reference_map(applications_root).get(name, ())))


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


def knowledge_base_reference_map(applications_root: Path) -> dict[str, tuple[str, ...]]:
    """扫描全部 agent.yaml，返回知识库名到引用它的 agent_id 列表的映射。"""
    result: dict[str, list[str]] = {}
    for config_path in _agent_config_paths(applications_root.resolve()):
        raw, _ = _load_raw_config(config_path)
        if raw is None:
            continue
        agent_id = _raw_agent_id(raw)
        refs = raw.get("knowledge_bases")
        if agent_id is None or not isinstance(refs, list):
            continue
        for item in refs:
            if isinstance(item, Mapping):
                ref_name = item.get("name")
                if isinstance(ref_name, str):
                    result.setdefault(ref_name, []).append(agent_id)
    return {name: tuple(sorted(agent_ids)) for name, agent_ids in result.items()}


def list_agent_knowledge_roots(
    applications_root: Path,
) -> tuple[tuple[str, Path], ...]:
    """枚举含有 knowledge/ 目录的 Agent，返回 (agent_id, knowledge 根目录)。"""
    roots: list[tuple[str, Path]] = []
    for config_path in _agent_config_paths(applications_root.resolve()):
        agent_dir = config_path.parent.parent
        knowledge_root = agent_dir / "knowledge"
        if not knowledge_root.is_dir():
            continue
        raw, _ = _load_raw_config(config_path)
        agent_id = _raw_agent_id(raw) or agent_dir.name
        roots.append((agent_id, knowledge_root))
    return tuple(roots)


# ---------------------------------------------------------------------------
# Skill 资源文件
# ---------------------------------------------------------------------------


def list_resources(
    store: PlatformStore,
    scope: str,
    name: str,
) -> tuple[SkillResourceItem, ...]:
    """列出 Skill 目录内除 SKILL.md 外的全部资源文件。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。

    Returns:
        资源文件的相对路径与字节数。
    """
    root = _existing_skill_root(store, scope, name)
    items: list[SkillResourceItem] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path == root / "SKILL.md":
            continue
        items.append(
            SkillResourceItem(
                path=path.relative_to(root).as_posix(),
                size=path.stat().st_size,
            )
        )
    return tuple(items)


def read_resource(store: PlatformStore, scope: str, name: str, path: str) -> str:
    """按相对路径读取单个白名单文本资源。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        path: Skill 根目录内的相对资源路径。

    Returns:
        资源文件的 UTF-8 文本。
    """
    root = _existing_skill_root(store, scope, name)
    candidate = _resource_path(root, path)
    if not candidate.is_file():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="resource_not_found",
            message="Skill 资源不存在",
        )
    return candidate.read_text(encoding="utf-8")


def write_resource(
    store: PlatformStore,
    scope: str,
    name: str,
    path: str,
    content: str,
) -> None:
    """写入或覆盖单个白名单文本资源。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        path: Skill 根目录内的相对资源路径。
        content: 正文文本。
    """
    root = _existing_skill_root(store, scope, name)
    candidate = _resource_path(root, path)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")


def delete_resource(store: PlatformStore, scope: str, name: str, path: str) -> None:
    """删除单个白名单文本资源。

    Args:
        store: 平台资源路径配置。
        scope: 资源范围：global 或 Agent 标识。
        name: 目标资源名称。
        path: Skill 根目录内的相对资源路径。
    """
    root = _existing_skill_root(store, scope, name)
    candidate = _resource_path(root, path)
    if not candidate.is_file():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="resource_not_found",
            message="Skill 资源不存在",
        )
    candidate.unlink()


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _agent_config_paths(applications_root: Path) -> tuple[Path, ...]:
    if not applications_root.is_dir():
        return ()
    paths = (
        child / "config" / "agent.yaml"
        for child in applications_root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )
    return tuple(sorted((path for path in paths if path.is_file()), key=str))


def _load_raw_config(
    config_path: Path,
) -> tuple[dict[str, JsonValue] | None, str | None]:
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return None, f"YAML 解析失败：{type(error).__name__}"
    if not isinstance(loaded, dict):
        return None, "YAML 配置根节点必须是 mapping"
    return {str(key): _json_value(value) for key, value in loaded.items()}, None


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _raw_agent_id(raw: Mapping[str, JsonValue] | None) -> str | None:
    if raw is None:
        return None
    value = raw.get("agent_id")
    return value if isinstance(value, str) and value else None


def _known_agent_ids(applications_root: Path) -> frozenset[str]:
    known: set[str] = set()
    for config_path in _agent_config_paths(applications_root):
        agent_id = _raw_agent_id(_load_raw_config(config_path)[0])
        if agent_id is not None:
            known.add(agent_id)
    return frozenset(known)


def _locate_agent_dir(applications_root: Path, agent_id: str) -> Path:
    agent_dir = find_agent_dir(applications_root, agent_id)
    if agent_dir is None:
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="agent_not_found",
            message="Agent 不存在",
        )
    return agent_dir


def _validate_agent_config(config: Mapping[str, JsonValue]) -> AgentConfig:
    _validate_api_key_placeholder(config)
    try:
        return AgentConfig.model_validate(config)
    except ValidationError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="agent_config_invalid",
            message=_format_validation_error(error),
        ) from error


def _validate_api_key_placeholder(config: Mapping[str, JsonValue]) -> None:
    """拒绝 ``llm_model.api_key`` 中的非 ``${ENV_NAME}`` 占位符值。

    必须在任何写文件动作之前调用。错误消息只携带字段路径，绝不包含被拒绝的
    原始值。
    """
    value = _nested_config_value(config, _API_KEY_PATH)
    if value is None or value == "":
        return
    if isinstance(value, str) and _ENV_PLACEHOLDER_RE.fullmatch(value):
        return
    raise ServiceError(
        kind=ServiceErrorKind.INVALID,
        code="agent_secret_not_placeholder",
        message=(
            f"{'.'.join(_API_KEY_PATH)} 只接受 ${{ENV_NAME}} 环境变量占位符，"
            "禁止明文密钥"
        ),
    )


def _mask_api_key(config: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """对读取响应中的 ``llm_model.api_key`` 脱敏。

    空值与 ``${ENV_NAME}`` 占位符保持原文（不插值），其余任意类型的值统一
    替换为掩码。
    """
    masked = dict(config)
    node = masked
    for key in _API_KEY_PATH[:-1]:
        child = node.get(key)
        if not isinstance(child, Mapping):
            return masked
        child = dict(child)
        node[key] = child
        node = child
    leaf = node.get(_API_KEY_PATH[-1])
    if leaf is None or leaf == "":
        return masked
    if isinstance(leaf, str) and _ENV_PLACEHOLDER_RE.fullmatch(leaf):
        return masked
    node[_API_KEY_PATH[-1]] = _SECRET_MASK
    return masked


def _nested_config_value(
    config: Mapping[str, JsonValue],
    path: tuple[str, ...],
) -> object:
    """按路径读取嵌套配置值；任一层缺失或不是 mapping 时返回 None。"""
    node: object = config
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _format_validation_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "config"
        parts.append(f"{field}: {item['msg']}")
    return "; ".join(parts)


def _validate_prompt_text(prompt_text: str) -> None:
    if not prompt_text.strip():
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="prompt_empty",
            message="Prompt 不能为空",
        )


def _prompt_path(agent_dir: Path, prompt: str) -> Path | None:
    try:
        return _resolve_within(agent_dir, prompt)
    except ValueError:
        return None


def _write_agent_files(
    agent_dir: Path,
    parsed: AgentConfig,
    raw_config: Mapping[str, JsonValue],
    prompt_text: str,
) -> None:
    try:
        prompt_path = _resolve_within(agent_dir, parsed.prompt)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_prompt_path",
            message="Prompt 路径必须位于 Application 根目录内",
        ) from error
    if prompt_path.suffix.lower() not in _PROMPT_SUFFIXES:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="unsupported_prompt_file",
            message="Prompt 文件必须是 .md 或 .txt",
        )
    config_dir = agent_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        yaml.safe_dump(dict(raw_config), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")


def _resolve_within(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("路径必须位于目标目录内") from error
    return candidate


def _validate_skill_name(name: str) -> None:
    if NAME_RE.fullmatch(name) is None:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_skill_name",
            message="Skill 名称不符合规范",
        )


def _validate_skill_fields(description: str) -> None:
    if not description.strip():
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="skill_description_empty",
            message="Skill description 不能为空",
        )


def _skill_root(store: PlatformStore, scope: str, name: str) -> Path:
    _validate_skill_name(name)
    if scope == "global":
        return store.project_root / "skills" / name
    return _locate_agent_dir(store.applications_root, scope) / "skills" / name


def _existing_skill_root(store: PlatformStore, scope: str, name: str) -> Path:
    root = _skill_root(store, scope, name)
    if not root.is_dir():
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="skill_not_found",
            message="Skill 不存在",
        )
    return root


def _skill_dir_names(skills_root: Path) -> tuple[str, ...]:
    if not skills_root.is_dir():
        return ()
    return tuple(
        sorted(
            child.name
            for child in skills_root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        )
    )


def _skill_reference_map(applications_root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for config_path in _agent_config_paths(applications_root.resolve()):
        raw, _ = _load_raw_config(config_path)
        if raw is None:
            continue
        agent_id = _raw_agent_id(raw)
        enabled = raw.get("enabled_skills")
        if agent_id is None or not isinstance(enabled, list):
            continue
        for item in enabled:
            if isinstance(item, str):
                result.setdefault(item, []).append(agent_id)
    return result


def _private_skill_map(applications_root: Path) -> dict[str, list[str]]:
    """skill 名称到拥有同名私有 Skill 的 agent_id 列表的映射。"""
    result: dict[str, list[str]] = {}
    root = applications_root.resolve()
    for config_path in _agent_config_paths(root):
        agent_id = _raw_agent_id(_load_raw_config(config_path)[0])
        if agent_id is None:
            continue
        agent_dir = config_path.parent.parent
        for name in _skill_dir_names(agent_dir / "skills"):
            result.setdefault(name, []).append(agent_id)
    return result


def _ensure_private_skill_root(agent_dir: Path) -> None:
    """首个私有 Skill 创建时向 agent.yaml 补充 skill_roots: [skills]。"""
    config_path = agent_dir / "config" / "agent.yaml"
    raw, error = _load_raw_config(config_path)
    if raw is None:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="agent_config_invalid",
            message=error or "Agent 配置无效",
        )
    roots = raw.get("skill_roots")
    values: list[JsonValue] = (
        [item for item in roots if isinstance(item, str)]
        if isinstance(roots, list)
        else []
    )
    if "skills" in values:
        return
    values.append("skills")
    raw["skill_roots"] = values
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _render_skill(
    name: str,
    *,
    description: str,
    content: str,
    metadata: Mapping[str, str],
    allowed_tools: Collection[str],
) -> str:
    frontmatter: dict[str, object] = {"name": name, "description": description}
    if metadata:
        frontmatter["metadata"] = dict(metadata)
    tools = [item for item in allowed_tools if item]
    if tools:
        frontmatter["allowed-tools"] = tools
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    body = content.strip()
    if body:
        return f"---\n{header}---\n\n{body}\n"
    return f"---\n{header}---\n"


def _skill_body(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return ""
    return text[match.end() :].strip()


def _frontmatter_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str)}


def _frontmatter_allowed_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _frontmatter_fallback(root: Path) -> tuple[str, dict[str, str]]:
    try:
        text = (root / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(text)
    except (OSError, ValueError):
        return "", {}
    description = frontmatter.get("description")
    return (
        description if isinstance(description, str) else "",
        _frontmatter_metadata(frontmatter.get("metadata")),
    )


def _list_resource_paths(root: Path) -> tuple[str, ...]:
    try:
        paths = [
            path
            for path in root.rglob("*")
            if path.is_file() and path != root / "SKILL.md"
        ]
    except OSError:
        return ()
    return tuple(sorted(path.relative_to(root).as_posix() for path in paths))


def _resource_path(root: Path, relative: str) -> Path:
    if Path(relative).name == "SKILL.md" and Path(relative).parts == ("SKILL.md",):
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_resource_path",
            message="SKILL.md 不能作为资源文件操作",
        )
    try:
        candidate = _resolve_within(root, relative)
    except ValueError as error:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="invalid_resource_path",
            message="资源路径必须位于 Skill 目录内",
        ) from error
    if candidate.suffix.lower() not in _ALLOWED_RESOURCE_SUFFIXES:
        raise ServiceError(
            kind=ServiceErrorKind.INVALID,
            code="unsupported_resource_type",
            message="资源文件仅支持 .md/.txt/.json/.yaml/.yml",
        )
    return candidate


__all__ = [
    "AgentDocument",
    "AgentStatus",
    "AgentSummary",
    "PlatformStore",
    "SkillDocument",
    "SkillListView",
    "SkillResourceItem",
    "SkillSummary",
    "create_agent",
    "create_skill",
    "delete_agent",
    "delete_resource",
    "delete_skill",
    "describe_skill",
    "find_agent_dir",
    "knowledge_base_reference_map",
    "list_agent_knowledge_roots",
    "list_disk_agents",
    "list_resources",
    "list_skill_names",
    "list_skills",
    "read_agent",
    "read_resource",
    "read_skill",
    "skill_references",
    "summarize_agent",
    "update_agent",
    "update_skill",
    "write_resource",
]
