"""Agent 配置平台 HTTP 路由：磁盘 CRUD、dry-run 校验与能力目录。"""

import os
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase

from agentsystem.common.models import JsonValue
from agentsystem.config.loader import _ENVIRONMENT_PATTERN
from agentsystem.config.models import AgentConfig
from fastapi import APIRouter, Query, status
from pydantic import ValidationError

from server.platform import store as platform_store
from server.platform.catalog import CatalogEntry, CatalogSnapshot, PlatformCatalog
from server.platform.knowledge.generation import (
    KbGenJobRegistry,
    register_kb_generation_routes,
)
from server.platform.knowledge.routes import register_knowledge_base_routes
from server.platform.schemas import (
    PlatformAgentDetailResponse,
    PlatformAgentListResponse,
    PlatformAgentResponse,
    PlatformAgentSummary,
    PlatformAgentUpsertRequest,
    PlatformAgentValidateRequest,
    PlatformAgentValidateResponse,
    PlatformCatalogEntry,
    PlatformCatalogOutbound,
    PlatformCatalogResponse,
    PlatformCatalogSkill,
    PlatformCatalogTool,
    PlatformDeletedResponse,
    PlatformIssue,
    PlatformResourceContentResponse,
    PlatformResourceItem,
    PlatformResourceListResponse,
    PlatformResourcePathResponse,
    PlatformResourceWriteRequest,
    PlatformSkillCreateRequest,
    PlatformSkillDetailResponse,
    PlatformSkillListResponse,
    PlatformSkillReferencesResponse,
    PlatformSkillResponse,
    PlatformSkillSummary,
    PlatformSkillUpdateRequest,
)
from server.platform.store import (
    AgentSummary,
    PlatformStore,
    SkillSummary,
)


@dataclass(frozen=True, slots=True)
class PlatformDeps:
    """平台路由的运行期依赖集合。"""

    store: PlatformStore
    catalog: PlatformCatalog
    active_agent_ids_provider: Callable[[], Collection[str]]


def create_platform_router(
    *,
    store: PlatformStore,
    catalog: PlatformCatalog,
    active_agent_ids_provider: Callable[[], Collection[str]],
    kb_gen_registry: KbGenJobRegistry | None = None,
) -> APIRouter:
    """创建挂载于 /v1/platform 前缀的配置平台路由。

    Args:
        store: 平台资源路径配置。
        catalog: 已注册能力的目录服务。
        active_agent_ids_provider: 按请求获取 Runtime 已加载 Agent 标识的函数。
        kb_gen_registry: 可选问题集生成任务注册表。

    Returns:
        配置平台的路由器。
    """
    router = APIRouter(prefix="/v1/platform")
    _register_agent_routes(router, store, catalog, active_agent_ids_provider)
    _register_catalog_route(router, store, catalog)
    _register_skill_routes(router, store)
    _register_skill_resource_routes(router, store)
    register_knowledge_base_routes(router, store)
    register_kb_generation_routes(
        router,
        store,
        kb_gen_registry or KbGenJobRegistry(store=store),
    )
    return router


def _register_agent_routes(
    router: APIRouter,
    store: PlatformStore,
    catalog: PlatformCatalog,
    active_agent_ids_provider: Callable[[], Collection[str]],
) -> None:
    """注册 Agent 磁盘 CRUD 与 dry-run 校验路由。"""

    @router.get("/agents", response_model=PlatformAgentListResponse)
    def list_agents() -> PlatformAgentListResponse:
        """返回磁盘全部 Agent（含待重启与配置无效）的摘要。"""
        agents = platform_store.list_disk_agents(
            store.applications_root,
            active_agent_ids_provider(),
        )
        return PlatformAgentListResponse(
            agents=tuple(_agent_summary_dto(item) for item in agents)
        )

    @router.post(
        "/agents",
        response_model=PlatformAgentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent(request: PlatformAgentUpsertRequest) -> PlatformAgentResponse:
        """校验并落盘新 Agent；重启后生效。"""
        parsed = platform_store.create_agent(
            store.applications_root,
            request.config,
            request.prompt_text,
        )
        return PlatformAgentResponse(
            agent=_agent_summary_dto(
                platform_store.summarize_agent(
                    parsed,
                    active_agent_ids_provider(),
                )
            )
        )

    @router.get("/agents/{agent_id}", response_model=PlatformAgentDetailResponse)
    def read_agent(agent_id: str) -> PlatformAgentDetailResponse:
        """读取单个 Agent 的配置原文与 Prompt。"""
        document = platform_store.read_agent(
            store.applications_root,
            agent_id,
            active_agent_ids_provider(),
        )
        return PlatformAgentDetailResponse(
            config=document.config,
            prompt_text=document.prompt_text,
            status=document.status,
            error=document.error,
        )

    @router.put("/agents/{agent_id}", response_model=PlatformAgentResponse)
    def update_agent(
        agent_id: str,
        request: PlatformAgentUpsertRequest,
    ) -> PlatformAgentResponse:
        """覆盖单个 Agent 的配置与 Prompt；agent_id 不可修改。"""
        parsed = platform_store.update_agent(
            store.applications_root,
            agent_id,
            request.config,
            request.prompt_text,
        )
        return PlatformAgentResponse(
            agent=_agent_summary_dto(
                platform_store.summarize_agent(
                    parsed,
                    active_agent_ids_provider(),
                )
            )
        )

    @router.delete("/agents/{agent_id}", response_model=PlatformDeletedResponse)
    def delete_agent(agent_id: str) -> PlatformDeletedResponse:
        """删除单个 Agent 的整个 Application 目录。"""
        platform_store.delete_agent(store.applications_root, agent_id)
        return PlatformDeletedResponse(deleted=True)

    @router.post(
        "/agents/validate",
        response_model=PlatformAgentValidateResponse,
    )
    def validate_agent(
        request: PlatformAgentValidateRequest,
    ) -> PlatformAgentValidateResponse:
        """对 Agent 配置草稿执行结构、环境变量与引用三层 dry-run 校验。"""
        errors, warnings = _validate_draft(
            store=store,
            catalog=catalog,
            config=request.config,
            prompt_text=request.prompt_text,
        )
        return PlatformAgentValidateResponse(errors=errors, warnings=warnings)


def _register_catalog_route(
    router: APIRouter,
    store: PlatformStore,
    catalog: PlatformCatalog,
) -> None:
    """注册影子能力目录路由。"""

    @router.get("/catalog", response_model=PlatformCatalogResponse)
    def get_catalog() -> PlatformCatalogResponse:
        """返回影子发现的可选工具、中间件、ContextProvider 与 outbound。"""
        snapshot = catalog.snapshot()
        global_skills = platform_store.list_skills(store, "global").skills
        return PlatformCatalogResponse(
            tools=tuple(
                PlatformCatalogTool(
                    name=item.name,
                    source=item.source,
                    description=item.description,
                    agent_id=item.agent_id,
                )
                for item in snapshot.tools
            ),
            middlewares=tuple(
                PlatformCatalogEntry(
                    name=item.name,
                    source=item.source,
                    agent_id=item.agent_id,
                )
                for item in snapshot.middlewares
            ),
            context_providers=tuple(
                PlatformCatalogEntry(
                    name=item.name,
                    source=item.source,
                    agent_id=item.agent_id,
                )
                for item in snapshot.context_providers
            ),
            outbounds=tuple(
                PlatformCatalogOutbound(type=item) for item in snapshot.outbounds
            ),
            global_skills=tuple(
                PlatformCatalogSkill(name=item.name, description=item.description)
                for item in global_skills
            ),
        )


def _register_skill_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册全局/私有 Skill 与资源文件路由。"""

    @router.get("/skills", response_model=PlatformSkillListResponse)
    def list_skills(
        scope: str = Query(min_length=1),
    ) -> PlatformSkillListResponse:
        """列出全局库或指定 Agent 私有库的 Skill。"""
        view = platform_store.list_skills(store, scope)
        return PlatformSkillListResponse(
            skills=tuple(_skill_summary_dto(item) for item in view.skills),
            global_skills=None
            if view.global_skills is None
            else tuple(_skill_summary_dto(item) for item in view.global_skills),
        )

    @router.post(
        "/skills",
        response_model=PlatformSkillResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_skill(request: PlatformSkillCreateRequest) -> PlatformSkillResponse:
        """创建全局或 Agent 私有 Skill。"""
        platform_store.create_skill(
            store,
            scope=request.scope,
            name=request.name,
            description=request.description,
            content=request.content,
            metadata=request.metadata,
            allowed_tools=request.allowed_tools,
        )
        return PlatformSkillResponse(
            skill=_skill_summary_dto(
                platform_store.describe_skill(store, request.scope, request.name)
            )
        )

    @router.get(
        "/skills/{name}/references",
        response_model=PlatformSkillReferencesResponse,
    )
    def get_skill_references(name: str) -> PlatformSkillReferencesResponse:
        """返回 enabled_skills 引用指定名称的全部 agent_id。"""
        return PlatformSkillReferencesResponse(
            agent_ids=platform_store.skill_references(store.applications_root, name)
        )

    @router.get(
        "/skills/{scope}/{name}",
        response_model=PlatformSkillDetailResponse,
    )
    def read_skill(scope: str, name: str) -> PlatformSkillDetailResponse:
        """读取单个 Skill 的 frontmatter 字段与 Markdown 正文。"""
        document = platform_store.read_skill(store, scope, name)
        return PlatformSkillDetailResponse(
            name=document.name,
            description=document.description,
            metadata=document.metadata,
            allowed_tools=document.allowed_tools,
            content=document.content,
            resources=document.resources,
        )

    @router.put("/skills/{scope}/{name}", response_model=PlatformSkillResponse)
    def update_skill(
        scope: str,
        name: str,
        request: PlatformSkillUpdateRequest,
    ) -> PlatformSkillResponse:
        """重写单个 Skill 的 SKILL.md。"""
        platform_store.update_skill(
            store,
            scope,
            name,
            description=request.description,
            content=request.content,
            metadata=request.metadata,
            allowed_tools=request.allowed_tools,
        )
        return PlatformSkillResponse(
            skill=_skill_summary_dto(platform_store.describe_skill(store, scope, name))
        )

    @router.delete(
        "/skills/{scope}/{name}",
        response_model=PlatformDeletedResponse,
    )
    def delete_skill(scope: str, name: str) -> PlatformDeletedResponse:
        """删除整个 Skill 目录。"""
        platform_store.delete_skill(store, scope, name)
        return PlatformDeletedResponse(deleted=True)


def _register_skill_resource_routes(router: APIRouter, store: PlatformStore) -> None:
    """注册 Skill 资源文件的列表、读取、写入与删除路由。"""

    @router.get(
        "/skills/{scope}/{name}/resources",
        response_model=PlatformResourceListResponse,
    )
    def list_resources(scope: str, name: str) -> PlatformResourceListResponse:
        """列出单个 Skill 的资源文件。"""
        items = platform_store.list_resources(store, scope, name)
        return PlatformResourceListResponse(
            resources=tuple(
                PlatformResourceItem(path=item.path, size=item.size) for item in items
            )
        )

    @router.get(
        "/skills/{scope}/{name}/resources/{path:path}",
        response_model=PlatformResourceContentResponse,
    )
    def read_resource(
        scope: str,
        name: str,
        path: str,
    ) -> PlatformResourceContentResponse:
        """读取单个白名单文本资源的内容。"""
        content = platform_store.read_resource(store, scope, name, path)
        return PlatformResourceContentResponse(path=path, content=content)

    @router.put(
        "/skills/{scope}/{name}/resources/{path:path}",
        response_model=PlatformResourcePathResponse,
    )
    def write_resource(
        scope: str,
        name: str,
        path: str,
        request: PlatformResourceWriteRequest,
    ) -> PlatformResourcePathResponse:
        """写入或覆盖单个白名单文本资源。"""
        platform_store.write_resource(store, scope, name, path, request.content)
        return PlatformResourcePathResponse(path=path)

    @router.delete(
        "/skills/{scope}/{name}/resources/{path:path}",
        response_model=PlatformDeletedResponse,
    )
    def delete_resource(scope: str, name: str, path: str) -> PlatformDeletedResponse:
        """删除单个白名单文本资源。"""
        platform_store.delete_resource(store, scope, name, path)
        return PlatformDeletedResponse(deleted=True)


def _agent_summary_dto(item: AgentSummary) -> PlatformAgentSummary:
    return PlatformAgentSummary(
        agent_id=item.agent_id,
        agent_name=item.agent_name,
        description=item.description,
        output_mode=item.output_mode,
        model=item.model,
        tool_count=item.tool_count,
        skill_count=item.skill_count,
        status=item.status,
        error=item.error,
    )


def _skill_summary_dto(item: SkillSummary) -> PlatformSkillSummary:
    return PlatformSkillSummary(
        name=item.name,
        description=item.description,
        source=item.source,
        metadata=item.metadata,
        resources=item.resources,
        valid=item.valid,
        error=item.error,
        referenced_by=item.referenced_by,
        overridden_by=item.overridden_by,
        overrides_global=item.overrides_global,
    )


def _validate_draft(
    *,
    store: PlatformStore,
    catalog: PlatformCatalog,
    config: dict[str, JsonValue],
    prompt_text: str | None,
) -> tuple[tuple[PlatformIssue, ...], tuple[PlatformIssue, ...]]:
    """结构、环境变量与引用三层只读校验；不读取任何环境变量的值。"""
    errors: list[PlatformIssue] = []
    warnings = _environment_warnings(config)
    if prompt_text is not None and not prompt_text.strip():
        errors.append(PlatformIssue(field="prompt_text", message="Prompt 不能为空"))
    try:
        parsed = AgentConfig.model_validate(config)
    except ValidationError as error:
        errors.extend(_structure_issues(error))
        return tuple(errors), tuple(warnings)
    reference_errors, reference_warnings = _reference_issues(
        store=store,
        snapshot=catalog.snapshot(),
        config=parsed,
    )
    errors.extend(reference_errors)
    warnings.extend(reference_warnings)
    return tuple(errors), tuple(warnings)


def _structure_issues(error: ValidationError) -> list[PlatformIssue]:
    issues: list[PlatformIssue] = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "config"
        issues.append(PlatformIssue(field=field, message=str(item["msg"])))
    return issues


def _environment_warnings(
    config: Mapping[str, JsonValue],
) -> list[PlatformIssue]:
    """只报告缺失环境变量的名字，不读取或回显任何变量值。"""
    seen: set[tuple[str, str]] = set()
    warnings: list[PlatformIssue] = []
    for field, variable in _walk_env_placeholders(config, ""):
        if variable in os.environ or (field, variable) in seen:
            continue
        seen.add((field, variable))
        warnings.append(PlatformIssue(field=field, message=f"缺少环境变量：{variable}"))
    return warnings


def _walk_env_placeholders(
    value: JsonValue | Mapping[str, JsonValue],
    path: str,
) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        for match in _ENVIRONMENT_PATTERN.finditer(value):
            yield path, match.group(1)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _walk_env_placeholders(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_env_placeholders(item, f"{path}.{index}")


def _reference_issues(
    *,
    store: PlatformStore,
    snapshot: CatalogSnapshot,
    config: AgentConfig,
) -> tuple[list[PlatformIssue], list[PlatformIssue]]:
    errors: list[PlatformIssue] = []
    warnings: list[PlatformIssue] = []
    _capability_warnings(
        config.enabled_tools,
        snapshot.tools,
        agent_id=config.agent_id,
        field="enabled_tools",
        label="工具",
        warnings=warnings,
    )
    _capability_warnings(
        config.enabled_middlewares,
        snapshot.middlewares,
        agent_id=config.agent_id,
        field="enabled_middlewares",
        label="中间件",
        warnings=warnings,
    )
    visible_skills = set(platform_store.list_skill_names(store, "global"))
    visible_skills |= set(platform_store.list_skill_names(store, config.agent_id))
    for name in config.enabled_skills:
        if name not in visible_skills:
            errors.append(
                PlatformIssue(
                    field="enabled_skills",
                    message=f"Skill 不存在：{name}",
                )
            )
    if config.outbound.type not in set(snapshot.outbounds):
        errors.append(
            PlatformIssue(
                field="outbound.type",
                message=f"未知 outbound 类型：{config.outbound.type}",
            )
        )
    agent_dir = platform_store.find_agent_dir(
        store.applications_root,
        config.agent_id,
    )
    for root_value in config.skill_roots:
        if agent_dir is None or not (agent_dir / root_value).is_dir():
            warnings.append(
                PlatformIssue(
                    field="skill_roots",
                    message=f"skill_roots 目录不存在：{root_value}",
                )
            )
    return errors, warnings


def _capability_warnings(
    patterns: tuple[str, ...] | None,
    entries: tuple[CatalogEntry, ...],
    *,
    agent_id: str,
    field: str,
    label: str,
    warnings: list[PlatformIssue],
) -> None:
    """按「builtin + 本 Agent 私有」范围匹配能力模式，零匹配产出警告。

    精确名字若属于其他 Agent 的 application 插件，警告中明确指出归属，
    因为运行时装配只允许引用本 Agent 自己的私有插件能力。
    """
    if not patterns:
        return
    scoped = {
        entry.name
        for entry in entries
        if entry.source == "builtin" or entry.agent_id == agent_id
    }
    foreign: dict[str, list[str]] = {}
    for entry in entries:
        if entry.source == "application" and entry.agent_id != agent_id:
            owner = entry.agent_id or "unknown"
            foreign.setdefault(entry.name, []).append(owner)
    for pattern in patterns:
        if any(fnmatchcase(name, pattern) for name in scoped):
            continue
        owners = foreign.get(pattern)
        if owners is not None:
            warnings.append(
                PlatformIssue(
                    field=field,
                    message=(
                        f"{label} {pattern} 属于其他 Agent（{', '.join(owners)}）"
                        "的私有插件，不可跨 Agent 使用"
                    ),
                )
            )
            continue
        warnings.append(
            PlatformIssue(
                field=field,
                message=f"{label}模式未匹配任何可用能力：{pattern}",
            )
        )


__all__ = ["PlatformDeps", "create_platform_router"]
