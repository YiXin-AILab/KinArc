"""通用评测 HTTP 路由：任务查询、Run 启动/轮询、报告与对比。"""

from dataclasses import dataclass

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from evaluation.models import EvalReport
from evaluation.report import ComparisonReport, compare_reports
from fastapi import APIRouter, Query, status

from server.platform.evaluation.runner import (
    EvalRunView,
    EvaluationRunRegistry,
)
from server.platform.evaluation.schemas import (
    EvaluationRunResponse,
    EvaluationRunStatus,
    EvaluationRunView,
    EvaluationTaskDetailResponse,
    EvaluationTaskListResponse,
    EvaluationTaskSummary,
)
from server.platform.evaluation.store import EvaluationStore


@dataclass(frozen=True, slots=True)
class EvaluationDeps:
    """评测路由的运行期依赖集合。"""

    store: EvaluationStore
    registry: EvaluationRunRegistry


def create_evaluation_router(deps: EvaluationDeps) -> APIRouter:
    """创建挂载于 /v1/evaluation 前缀的评测路由（轮询模型，无 SSE）。"""
    router = APIRouter(prefix="/v1/evaluation")

    @router.get("/tasks", response_model=EvaluationTaskListResponse)
    def list_tasks() -> EvaluationTaskListResponse:
        """返回全部评测任务摘要。"""
        return EvaluationTaskListResponse(
            tasks=tuple(
                EvaluationTaskSummary.model_validate(item)
                for item in deps.store.list_tasks()
            )
        )

    @router.get("/tasks/{task_id}", response_model=EvaluationTaskDetailResponse)
    def read_task(task_id: str) -> EvaluationTaskDetailResponse:
        """返回任务摘要与全部 Run 快照。"""
        summary = _task_summary(deps.store, task_id)
        return EvaluationTaskDetailResponse(
            task=summary,
            runs=tuple(
                _run_view_dto(view) for view in deps.registry.list_runs(task_id)
            ),
        )

    @router.post(
        "/tasks/{task_id}/runs",
        response_model=EvaluationRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def start_run(task_id: str) -> EvaluationRunResponse:
        """启动一个评测 Run（允许并发，每 Run 独立目录）。"""
        return EvaluationRunResponse(run=_run_view_dto(deps.registry.start(task_id)))

    @router.get("/runs/{run_id}", response_model=EvaluationRunResponse)
    def read_run(run_id: str) -> EvaluationRunResponse:
        """返回指定 Run 的状态与进度快照。"""
        return EvaluationRunResponse(run=_run_view_dto(_get_run(deps, run_id)))

    @router.get("/runs/{run_id}/report", response_model=EvalReport)
    def read_report(run_id: str) -> EvalReport:
        """返回已完成 Run 的完整报告。"""
        return deps.store.read_report(run_id)

    @router.get("/runs/{run_id}/compare", response_model=ComparisonReport)
    def compare_run(
        run_id: str,
        baseline: str = Query(min_length=1),
    ) -> ComparisonReport:
        """对比当前 Run 与 baseline Run 的报告。"""
        current = deps.store.read_report(run_id)
        baseline_report = deps.store.read_report(baseline)
        return compare_reports(baseline_report, current)

    return router


def _task_summary(
    store: EvaluationStore,
    task_id: str,
) -> EvaluationTaskSummary:
    store.task_dir(task_id)
    for item in store.list_tasks():
        if item["task_id"] == task_id:
            return EvaluationTaskSummary.model_validate(item)
    raise ServiceError(
        kind=ServiceErrorKind.NOT_FOUND,
        code="evaluation_task_not_found",
        message=f"评测任务不存在：{task_id}",
    )


def _get_run(deps: EvaluationDeps, run_id: str) -> EvalRunView:
    try:
        return deps.registry.get(run_id)
    except KeyError as error:
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="evaluation_run_not_found",
            message=f"Run 不存在：{run_id}",
        ) from error


def _run_view_dto(view: EvalRunView) -> EvaluationRunView:
    return EvaluationRunView(
        run_id=view.run_id,
        task_id=view.task_id,
        status=EvaluationRunStatus(view.status.value),
        created_at=view.created_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        case_count=view.case_count,
        completed_cases=view.completed_cases,
        final_score=view.final_score,
        error=view.error,
    )


__all__ = ["EvaluationDeps", "create_evaluation_router"]
