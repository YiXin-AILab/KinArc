"""自进化 HTTP 请求与响应模型。"""

from enum import StrEnum

from agentsystem.common.models import JsonValue, StrictModel
from pydantic import Field


class EvolutionTaskSummary(StrictModel):
    """单个 Evolution 任务的列表级摘要。"""

    task_id: str = Field(min_length=1)
    objective: str
    initial_target: str
    max_iterations: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    best_validation_score: float | None = None
    best_candidate_id: str | None = None
    total_tokens: int = Field(ge=0)
    valid: bool
    error: str | None = None
    active_run_id: str | None = None


class EvolutionTaskListResponse(StrictModel):
    """全部 Evolution 任务摘要。"""

    tasks: tuple[EvolutionTaskSummary, ...]


class EvolutionTemplateSummary(StrictModel):
    """可克隆的 Evolution 任务模板摘要。"""

    task_id: str = Field(min_length=1)
    objective: str
    initial_target: str
    dataset_case_counts: dict[str, int]


class EvolutionTemplateListResponse(StrictModel):
    """全部可克隆模板。"""

    templates: tuple[EvolutionTemplateSummary, ...]


class EvolutionTaskCreateRequest(StrictModel):
    """从模板克隆新任务的请求；标量字段缺省时沿用模板。"""

    template_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    objective: str | None = Field(default=None, min_length=1)
    initial_target: str | None = Field(default=None, min_length=1)
    max_iterations: int | None = Field(default=None, ge=0)
    improver_timeout_seconds: float | None = Field(default=None, gt=0)
    max_case_concurrency: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    rollback_enabled: bool | None = None
    exploration_patience: int | None = Field(default=None, ge=1)
    settings: dict[str, JsonValue] | None = None


class EvolutionTaskResponse(StrictModel):
    """单个任务摘要响应。"""

    task: EvolutionTaskSummary


class EvolutionCaseScore(StrictModel):
    """单个 Case 的评分事实。"""

    case_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    failure_kind: str | None = None


class EvolutionRoleResult(StrictModel):
    """一个候选在某个数据集上的聚合结果。"""

    total_score: float = Field(ge=0, le=1)
    correct: int = Field(ge=0)
    case_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    cases: tuple[EvolutionCaseScore, ...]


class EvolutionCandidateDetail(StrictModel):
    """一个候选版本的完整展示事实（changelog 体积大，经单独端点按需获取）。"""

    candidate_id: str = Field(min_length=1)
    parent_id: str | None = None
    is_rollback: bool
    train: EvolutionRoleResult | None = None
    validation: EvolutionRoleResult | None = None
    test: EvolutionRoleResult | None = None


class EvolutionChangelogResponse(StrictModel):
    """单个候选的 changelog 原文。"""

    candidate_id: str = Field(min_length=1)
    changelog_markdown: str


class EvolutionTaskDetailResponse(StrictModel):
    """任务配置摘要与全部候选详情。"""

    task_id: str = Field(min_length=1)
    objective: str
    initial_target: str
    max_iterations: int = Field(ge=0)
    settings: dict[str, JsonValue]
    candidates: tuple[EvolutionCandidateDetail, ...]
    active_run_id: str | None = None


class EvolutionRunStatus(StrEnum):
    """一次 Evolution Run 的生命周期状态。"""

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class EvolutionRunView(StrictModel):
    """Run 状态的只读快照。"""

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: EvolutionRunStatus
    started_at: float
    finished_at: float | None = None
    stop_reason: str | None = None
    error: str | None = None
    best_candidate_id: str | None = None
    validation_score: float | None = None
    event_count: int = Field(ge=0)


class EvolutionRunResponse(StrictModel):
    """单个 Run 快照响应。"""

    run: EvolutionRunView
