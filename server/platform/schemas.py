"""配置平台 HTTP 请求与响应模型。"""

from typing import Literal

from agentsystem.common.models import JsonValue, OutputMode, StrictModel
from pydantic import Field

# ---------------------------------------------------------------------------
# Agent 配置平台（/v1/platform）
# ---------------------------------------------------------------------------


class PlatformIssue(StrictModel):
    """配置 dry-run 校验的字段级问题。"""

    field: str
    message: str = Field(min_length=1)


class PlatformAgentSummary(StrictModel):
    """磁盘 Agent 配置的公开摘要与生效状态。"""

    agent_id: str = Field(min_length=1)
    agent_name: str
    description: str | None = None
    output_mode: OutputMode
    model: str | None = None
    tool_count: int = Field(ge=0)
    skill_count: int = Field(ge=0)
    status: Literal["active", "pending", "invalid"]
    error: str | None = None


class PlatformAgentListResponse(StrictModel):
    """磁盘视角的全部 Agent 摘要。"""

    agents: tuple[PlatformAgentSummary, ...]


class PlatformAgentUpsertRequest(StrictModel):
    """创建或覆盖 Agent 的配置原文与 Prompt 文本。"""

    config: dict[str, JsonValue]
    prompt_text: str


class PlatformAgentResponse(StrictModel):
    """单个 Agent 摘要响应。"""

    agent: PlatformAgentSummary


class PlatformAgentDetailResponse(StrictModel):
    """单个 Agent 的配置原文、Prompt 与状态。"""

    config: dict[str, JsonValue]
    prompt_text: str
    status: Literal["active", "pending", "invalid"]
    error: str | None = None


class PlatformAgentValidateRequest(StrictModel):
    """Agent 配置 dry-run 校验请求；prompt_text 可省略。"""

    config: dict[str, JsonValue]
    prompt_text: str | None = None


class PlatformAgentValidateResponse(StrictModel):
    """dry-run 校验的字段级错误与警告。"""

    errors: tuple[PlatformIssue, ...]
    warnings: tuple[PlatformIssue, ...]


class PlatformDeletedResponse(StrictModel):
    """删除类端点的统一确认。"""

    deleted: bool


class PlatformCatalogTool(StrictModel):
    """能力目录中的单个工具。

    `agent_id` 为 null 表示 builtin 能力（所有 Agent 可用）；
    否则表示该工具由指定 Agent 的 application 插件私有提供，不可跨 Agent 引用。
    """

    name: str = Field(min_length=1)
    source: Literal["builtin", "application"]
    description: str
    agent_id: str | None = None


class PlatformCatalogEntry(StrictModel):
    """能力目录中的单个 Middleware 或 ContextProvider。

    `agent_id` 语义与 PlatformCatalogTool 相同：null 为 builtin，否则为私有归属。
    """

    name: str = Field(min_length=1)
    source: Literal["builtin", "application"]
    agent_id: str | None = None


class PlatformCatalogOutbound(StrictModel):
    """能力目录中的单个已注册 outbound 类型。"""

    type: str = Field(min_length=1)


class PlatformCatalogSkill(StrictModel):
    """能力目录中的单个全局 Skill。"""

    name: str = Field(min_length=1)
    description: str


class PlatformCatalogResponse(StrictModel):
    """平台可选能力的完整目录。"""

    tools: tuple[PlatformCatalogTool, ...]
    middlewares: tuple[PlatformCatalogEntry, ...]
    context_providers: tuple[PlatformCatalogEntry, ...]
    outbounds: tuple[PlatformCatalogOutbound, ...]
    global_skills: tuple[PlatformCatalogSkill, ...]


class PlatformSkillSummary(StrictModel):
    """磁盘 Skill 的公开摘要、校验状态与可见性。"""

    name: str = Field(min_length=1)
    description: str
    source: Literal["global", "private"]
    metadata: dict[str, str] = Field(default_factory=dict)
    resources: tuple[str, ...] = ()
    valid: bool
    error: str | None = None
    referenced_by: tuple[str, ...] = ()
    overridden_by: tuple[str, ...] = ()
    overrides_global: bool = False


class PlatformSkillListResponse(StrictModel):
    """单 scope 的 Skill 列表；私有 scope 附带全局库视图。"""

    skills: tuple[PlatformSkillSummary, ...]
    global_skills: tuple[PlatformSkillSummary, ...] | None = None


class PlatformSkillCreateRequest(StrictModel):
    """创建 Skill 的 frontmatter 字段与 Markdown 正文。"""

    scope: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    content: str
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


class PlatformSkillUpdateRequest(StrictModel):
    """更新 Skill 的 frontmatter 字段与 Markdown 正文。"""

    description: str
    content: str
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


class PlatformSkillResponse(StrictModel):
    """单个 Skill 摘要响应。"""

    skill: PlatformSkillSummary


class PlatformSkillDetailResponse(StrictModel):
    """单个 Skill 的完整 frontmatter 与正文。"""

    name: str = Field(min_length=1)
    description: str
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    content: str
    resources: tuple[str, ...] = ()


class PlatformSkillReferencesResponse(StrictModel):
    """引用指定 Skill 的全部 agent_id。"""

    agent_ids: tuple[str, ...]


class PlatformResourceItem(StrictModel):
    """单个 Skill 资源文件的路径与字节数。"""

    path: str = Field(min_length=1)
    size: int = Field(ge=0)


class PlatformResourceListResponse(StrictModel):
    """单个 Skill 的资源文件清单。"""

    resources: tuple[PlatformResourceItem, ...]


class PlatformResourceWriteRequest(StrictModel):
    """写入 Skill 资源文件的文本内容。"""

    content: str


class PlatformResourcePathResponse(StrictModel):
    """资源写入后的相对路径确认。"""

    path: str = Field(min_length=1)


class PlatformResourceContentResponse(StrictModel):
    """单个 Skill 资源文件的相对路径与文本内容。"""

    path: str = Field(min_length=1)
    content: str
