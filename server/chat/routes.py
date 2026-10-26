"""V1 session 路由，不兼容旧 Agent API。"""

from agentsystem.config.models import PublicStreamConfig
from agentsystem.service.application import ApplicationService
from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse

from server.chat.mapper import (
    to_agent_catalog_response,
    to_cancel_command,
    to_message_submission_response,
    to_messages_response,
    to_pending_inputs_response,
    to_resume_command,
    to_session_context,
    to_snapshot_response,
    to_status_response,
    to_submit_command,
)
from server.chat.schemas import (
    AgentCatalogResponse,
    CancelSessionRequest,
    MessageSubmissionRequest,
    MessageSubmissionResponse,
    PendingInputsResponse,
    ResumeSubmissionRequest,
    SessionMessagesResponse,
    SessionSnapshotResponse,
    SessionStatusResponse,
    SessionTodosResponse,
)
from server.chat.sessions import register_session_catalog_routes
from server.chat.sse import (
    SseConnectionLimiter,
    SseDiagnosticSink,
    stream_sse,
)


def create_router(
    service: ApplicationService,
    *,
    sse_config: PublicStreamConfig | None = None,
    sse_diagnostic_sink: SseDiagnosticSink | None = None,
) -> APIRouter:
    """创建显式注入应用服务的 V1 路由。"""
    router = APIRouter(prefix="/v1")
    register_session_catalog_routes(router, service)
    _register_todo_route(router, service)
    stream_config = sse_config or PublicStreamConfig()
    limiter = SseConnectionLimiter(
        total=stream_config.max_connections_total,
        per_user=stream_config.max_connections_per_user,
        per_session=stream_config.max_connections_per_session,
    )

    @router.post(
        "/sessions/{session_id}/messages",
        response_model=MessageSubmissionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_message(
        session_id: str,
        request: MessageSubmissionRequest,
    ) -> MessageSubmissionResponse:
        ctx = to_session_context(
            session_id, request.app_id, request.user_id, request.agent_id
        )
        result = await service.submit_message(ctx, to_submit_command(request))
        return to_message_submission_response(session_id, result)

    @router.post(
        "/sessions/{session_id}/resume",
        response_model=SessionStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def resume(
        session_id: str,
        request: ResumeSubmissionRequest,
    ) -> SessionStatusResponse:
        ctx = to_session_context(
            session_id, request.app_id, request.user_id, request.agent_id
        )
        result = await service.resume(ctx, to_resume_command(request))
        return to_status_response(session_id, result)

    @router.post(
        "/sessions/{session_id}/cancel",
        response_model=SessionStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel(
        session_id: str,
        request: CancelSessionRequest,
    ) -> SessionStatusResponse:
        ctx = to_session_context(
            session_id, request.app_id, request.user_id, request.agent_id
        )
        result = await service.cancel(ctx, to_cancel_command(request))
        return to_status_response(session_id, result)

    @router.get(
        "/sessions/{session_id}",
        response_model=SessionSnapshotResponse,
        response_model_exclude_none=True,
    )
    async def snapshot(
        session_id: str,
        app_id: str = Query(min_length=1),
        user_id: str = Query(min_length=1),
        agent_id: str = Query(min_length=1),
    ) -> SessionSnapshotResponse:
        result = await service.snapshot(
            to_session_context(session_id, app_id, user_id, agent_id)
        )
        if result is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        return to_snapshot_response(result)

    @router.get(
        "/sessions/{session_id}/messages",
        response_model=SessionMessagesResponse,
        response_model_exclude_none=True,
    )
    async def list_messages(
        session_id: str,
        app_id: str = Query(min_length=1),
        user_id: str = Query(min_length=1),
        agent_id: str = Query(min_length=1),
    ) -> SessionMessagesResponse:
        result = await service.list_messages(
            to_session_context(session_id, app_id, user_id, agent_id),
        )
        return to_messages_response(result)

    @router.get(
        "/sessions/{session_id}/events",
        responses={
            200: {
                "description": "Resumable session event stream",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def stream_events(
        request: Request,
        session_id: str,
        app_id: str = Query(min_length=1),
        user_id: str = Query(min_length=1),
        agent_id: str = Query(min_length=1),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        lease = await limiter.acquire(user_id=user_id, session_id=session_id)
        try:
            subscription = await service.open_event_stream(
                to_session_context(session_id, app_id, user_id, agent_id),
                last_event_id,
            )
        except BaseException:
            await lease.close()
            raise
        body = stream_sse(
            request=request,
            subscription=subscription,
            lease=lease,
            config=stream_config,
            user_id=user_id,
            session_id=session_id,
            is_reconnect=last_event_id is not None,
            diagnostic_sink=sse_diagnostic_sink,
        )
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Vary": "Authorization",
            },
        )

    _register_catalog_route(router, service)
    _register_pending_route(router, service)
    return router


def _register_todo_route(router: APIRouter, service: ApplicationService) -> None:
    """注册不依赖会话状态的完整清单查询。"""

    @router.get("/sessions/{session_id}/todos", response_model=SessionTodosResponse)
    async def read_todos(
        session_id: str = Path(min_length=1, max_length=191),
        app_id: str = Query(min_length=1, max_length=191),
        user_id: str = Query(min_length=1, max_length=191),
        agent_id: str = Query(min_length=1, max_length=191),
    ) -> SessionTodosResponse:
        """返回当前身份的完整清单；不存在返回空数组。"""
        todos = await service.read_todos(
            to_session_context(session_id, app_id, user_id, agent_id)
        )
        return SessionTodosResponse(todos=todos)


def _register_catalog_route(
    router: APIRouter,
    service: ApplicationService,
) -> None:
    """注册只读 Agent 目录，保持主路由工厂复杂度稳定。"""

    @router.get("/agents", response_model=AgentCatalogResponse)
    async def list_agents() -> AgentCatalogResponse:
        """返回当前 Runtime 成功注册的 Agent 目录。"""
        return to_agent_catalog_response(service.list_agents())


def _register_pending_route(
    router: APIRouter,
    service: ApplicationService,
) -> None:
    """注册独立 pending 查询，避免主路由工厂继续增长。"""

    @router.get(
        "/sessions/{session_id}/inputs/pending",
        response_model=PendingInputsResponse,
    )
    async def list_pending_inputs(
        session_id: str,
        app_id: str = Query(min_length=1),
        user_id: str = Query(min_length=1),
        agent_id: str = Query(min_length=1),
    ) -> PendingInputsResponse:
        result = await service.list_pending_inputs(
            to_session_context(session_id, app_id, user_id, agent_id),
        )
        return to_pending_inputs_response(result)
