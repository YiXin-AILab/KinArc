"""Evolution 自进化任务 HTTP 路由：任务 CRUD、Run 生命周期与 SSE 进度流。"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse

from server.platform.evolution.runner import (
    EvolutionRunRegistry,
    ProgressEvent,
    RunView,
)
from server.platform.evolution.schemas import (
    EvolutionCandidateDetail,
    EvolutionCaseScore,
    EvolutionChangelogResponse,
    EvolutionRoleResult,
    EvolutionRunResponse,
    EvolutionRunStatus,
    EvolutionRunView,
    EvolutionTaskCreateRequest,
    EvolutionTaskDetailResponse,
    EvolutionTaskListResponse,
    EvolutionTaskResponse,
    EvolutionTaskSummary,
    EvolutionTemplateListResponse,
    EvolutionTemplateSummary,
)
from server.platform.evolution.store import (
    EvolutionStore,
    RoleResult,
    TaskOverrides,
    TaskSummary,
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}
_HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class EvolutionDeps:
    """Evolution 路由的运行期依赖集合。"""

    store: EvolutionStore
    registry: EvolutionRunRegistry


def create_evolution_router(deps: EvolutionDeps) -> APIRouter:
    """创建挂载于 /v1/evolution 前缀的自进化任务路由。"""
    router = APIRouter(prefix="/v1/evolution")
    _register_task_routes(router, deps.store, deps.registry)
    _register_run_routes(router, deps.registry)
    return router


def _register_task_routes(
    router: APIRouter,
    store: EvolutionStore,
    registry: EvolutionRunRegistry,
) -> None:
    """注册任务列表、模板与克隆创建路由。"""

    @router.get("/tasks", response_model=EvolutionTaskListResponse)
    def list_tasks() -> EvolutionTaskListResponse:
        """返回全部 Evolution 任务摘要（合并活跃 Run 状态）。"""
        active = registry.active_run()
        return EvolutionTaskListResponse(
            tasks=tuple(_task_summary_dto(item, active) for item in store.list_tasks())
        )

    @router.get("/templates", response_model=EvolutionTemplateListResponse)
    def list_templates() -> EvolutionTemplateListResponse:
        """返回可克隆的任务模板列表。"""
        return EvolutionTemplateListResponse(
            templates=tuple(
                EvolutionTemplateSummary(
                    task_id=item.task_id,
                    objective=item.objective,
                    initial_target=item.initial_target,
                    dataset_case_counts=item.dataset_case_counts,
                )
                for item in store.list_templates()
            )
        )

    @router.post(
        "/tasks",
        response_model=EvolutionTaskResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(request: EvolutionTaskCreateRequest) -> EvolutionTaskResponse:
        """从模板克隆新任务；配置经 TaskConfig 严格校验后落盘。"""
        summary = store.create_task(
            template_id=request.template_id,
            task_id=request.task_id,
            overrides=TaskOverrides(
                objective=request.objective,
                initial_target=request.initial_target,
                max_iterations=request.max_iterations,
                improver_timeout_seconds=request.improver_timeout_seconds,
                max_case_concurrency=request.max_case_concurrency,
                max_retries=request.max_retries,
                rollback_enabled=request.rollback_enabled,
                exploration_patience=request.exploration_patience,
                settings=request.settings,
            ),
        )
        return EvolutionTaskResponse(
            task=_task_summary_dto(summary, registry.active_run())
        )

    @router.get("/tasks/{task_id}", response_model=EvolutionTaskDetailResponse)
    def read_task(task_id: str) -> EvolutionTaskDetailResponse:
        """返回任务配置摘要与全部候选详情（含 changelog 原文）。"""
        detail = store.task_detail(task_id)
        active = registry.active_run()
        return EvolutionTaskDetailResponse(
            task_id=detail.task_id,
            objective=detail.objective,
            initial_target=detail.initial_target,
            max_iterations=detail.max_iterations,
            settings=detail.settings,
            candidates=tuple(
                EvolutionCandidateDetail(
                    candidate_id=candidate.candidate_id,
                    parent_id=candidate.parent_id,
                    is_rollback=candidate.is_rollback,
                    train=_role_result_dto(candidate.train),
                    validation=_role_result_dto(candidate.validation),
                    test=_role_result_dto(candidate.test),
                )
                for candidate in detail.candidates
            ),
            active_run_id=(
                active.run_id
                if active is not None and active.task_id == task_id
                else None
            ),
        )

    @router.get(
        "/tasks/{task_id}/candidates/{candidate_id}/changelog",
        response_model=EvolutionChangelogResponse,
    )
    def read_changelog(task_id: str, candidate_id: str) -> EvolutionChangelogResponse:
        """按需返回单个候选的 changelog 原文。"""
        return EvolutionChangelogResponse(
            candidate_id=candidate_id,
            changelog_markdown=store.changelog(task_id, candidate_id),
        )


def _register_run_routes(router: APIRouter, registry: EvolutionRunRegistry) -> None:
    """注册 Run 启动、查询、停止与 SSE 进度流路由。"""

    @router.post(
        "/tasks/{task_id}/runs",
        response_model=EvolutionRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def start_run(task_id: str) -> EvolutionRunResponse:
        """启动一个进化 Run；已有活跃 Run 时返回 409。"""
        try:
            view = registry.start(task_id)
        except RuntimeError as error:
            raise ServiceError(
                kind=ServiceErrorKind.CONFLICT,
                code="evolution_run_active",
                message=str(error),
            ) from error
        return EvolutionRunResponse(run=_run_view_dto(view))

    @router.get("/runs/{run_id}", response_model=EvolutionRunResponse)
    def read_run(run_id: str) -> EvolutionRunResponse:
        """返回指定 Run 的状态快照。"""
        return EvolutionRunResponse(run=_run_view_dto(_get_run(registry, run_id)))

    @router.post("/runs/{run_id}/stop", response_model=EvolutionRunResponse)
    def stop_run(run_id: str) -> EvolutionRunResponse:
        """请求协作式停止一个活跃 Run；已终态时返回 409。"""
        _get_run(registry, run_id)
        try:
            view = registry.stop(run_id)
        except RuntimeError as error:
            raise ServiceError(
                kind=ServiceErrorKind.CONFLICT,
                code="evolution_run_not_active",
                message=str(error),
            ) from error
        return EvolutionRunResponse(run=_run_view_dto(view))

    @router.get("/runs/{run_id}/events")
    async def stream_run_events(request: Request, run_id: str) -> StreamingResponse:
        """SSE 进度事件流；支持 Last-Event-ID 续读与心跳。"""
        _get_run(registry, run_id)
        last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
        queue = registry.subscribe(run_id, loop=asyncio.get_running_loop())
        return StreamingResponse(
            _event_stream(
                request=request,
                registry=registry,
                run_id=run_id,
                queue=queue,
                last_event_id=last_event_id,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )


async def _event_stream(
    *,
    request: Request,
    registry: EvolutionRunRegistry,
    run_id: str,
    queue: asyncio.Queue[ProgressEvent | None],
    last_event_id: int,
) -> AsyncIterator[bytes]:
    sent = last_event_id
    try:
        while True:
            if await request.is_disconnected():
                return
            backlog = registry.events_after(run_id, sent)
            if backlog:
                for event in backlog:
                    sent = event.cursor
                    yield _encode_progress_frame(event)
                continue
            if registry.is_terminal(run_id):
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield b": ping\n\n"
                continue
            if item is None:
                continue
            if item.cursor <= sent:
                continue
            sent = item.cursor
            yield _encode_progress_frame(item)
    finally:
        registry.unsubscribe(run_id, queue)


def _encode_progress_frame(event: ProgressEvent) -> bytes:
    data = json.dumps(
        {
            "cursor": event.cursor,
            "ts": event.ts,
            "event": event.event,
            "message": event.message,
            "fields": dict(event.fields),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.cursor}\nevent: progress\ndata: {data}\n\n".encode()


def _parse_last_event_id(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return max(int(value), 0)
    except ValueError:
        return 0


def _get_run(registry: EvolutionRunRegistry, run_id: str) -> RunView:
    try:
        return registry.get(run_id)
    except KeyError as error:
        raise ServiceError(
            kind=ServiceErrorKind.NOT_FOUND,
            code="evolution_run_not_found",
            message=f"Run 不存在：{run_id}",
        ) from error


def _task_summary_dto(
    item: TaskSummary,
    active: RunView | None,
) -> EvolutionTaskSummary:
    return EvolutionTaskSummary(
        task_id=item.task_id,
        objective=item.objective,
        initial_target=item.initial_target,
        max_iterations=item.max_iterations,
        candidate_count=item.candidate_count,
        best_validation_score=item.best_validation_score,
        best_candidate_id=item.best_candidate_id,
        total_tokens=item.total_tokens,
        valid=item.valid,
        error=item.error,
        active_run_id=(
            active.run_id
            if active is not None and active.task_id == item.task_id
            else None
        ),
    )


def _role_result_dto(item: RoleResult | None) -> EvolutionRoleResult | None:
    if item is None:
        return None
    return EvolutionRoleResult(
        total_score=item.total_score,
        correct=item.correct,
        case_count=item.case_count,
        total_tokens=item.total_tokens,
        duration_seconds=item.duration_seconds,
        cases=tuple(
            EvolutionCaseScore(
                case_id=case.case_id,
                score=case.score,
                failure_kind=case.failure_kind,
            )
            for case in item.cases
        ),
    )


def _run_view_dto(view: RunView) -> EvolutionRunView:
    return EvolutionRunView(
        run_id=view.run_id,
        task_id=view.task_id,
        status=EvolutionRunStatus(view.status.value),
        started_at=view.started_at,
        finished_at=view.finished_at,
        stop_reason=view.stop_reason,
        error=view.error,
        best_candidate_id=view.best_candidate_id,
        validation_score=view.validation_score,
        event_count=view.event_count,
    )


__all__ = ["EvolutionDeps", "create_evolution_router"]
