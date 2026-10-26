"""评测 HTTP 请求与响应模型。"""

from enum import StrEnum

from agentsystem.common.models import StrictModel
from pydantic import Field

# ---------------------------------------------------------------------------
# 通用评测（/v1/evaluation）
# ---------------------------------------------------------------------------


class EvaluationTaskSummary(StrictModel):
    """单个评测任务的列表级摘要。"""

    task_id: str = Field(min_length=1)
    agent_id: str
    dataset: str
    case_count: int = Field(ge=0)
    run_count: int = Field(ge=0)
    valid: bool
    error: str | None = None


class EvaluationTaskListResponse(StrictModel):
    """全部评测任务摘要。"""

    tasks: tuple[EvaluationTaskSummary, ...]


class EvaluationRunStatus(StrEnum):
    """一次评测 Run 的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvaluationRunView(StrictModel):
    """评测 Run 状态的只读快照。"""

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: EvaluationRunStatus
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    case_count: int | None = None
    completed_cases: int = Field(ge=0)
    final_score: float | None = Field(default=None, ge=0, le=1)
    error: str | None = None


class EvaluationRunResponse(StrictModel):
    """单个评测 Run 快照响应。"""

    run: EvaluationRunView


class EvaluationTaskDetailResponse(StrictModel):
    """任务配置摘要与全部 Run 快照。"""

    task: EvaluationTaskSummary
    runs: tuple[EvaluationRunView, ...]
