"""对话目录和标题管理的 HTTP DTO 与路由。"""

from datetime import datetime

from agentsystem.common.errors import ServiceError, ServiceErrorKind
from agentsystem.common.models import StrictModel
from agentsystem.message.sessions import SessionInfo, SessionListQuery, SessionTitle
from agentsystem.service.application import ApplicationService
from fastapi import APIRouter, Query
from pydantic import Field, ValidationError

from server.chat.mapper import to_session_context


class SessionIdentityRequest(StrictModel):
    """对话操作的既有身份字段。"""

    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


class UpdateTitleRequest(SessionIdentityRequest):
    """只允许修改标题，不允许覆盖身份或时间。"""

    title: SessionTitle


class DeleteSessionRequest(SessionIdentityRequest):
    """默认软删除；显式开启 hard_delete 才清空历史。"""

    hard_delete: bool = Field(default=False, strict=True)


class SessionInfoResponse(SessionIdentityRequest):
    """对话目录的公开基本信息。"""

    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    title_version: int


class SessionListResponse(StrictModel):
    """按创建时间倒序的一页对话。"""

    sessions: tuple[SessionInfoResponse, ...]
    next_cursor: str | None = None


def to_session_info_response(info: SessionInfo) -> SessionInfoResponse:
    """显式映射身份和元数据，不泄漏内部存储键。"""
    return SessionInfoResponse(
        app_id=info.ctx.app_id,
        user_id=info.ctx.user_id,
        agent_id=info.ctx.agent_id,
        session_id=info.ctx.session_id,
        title=info.title,
        created_at=info.created_at,
        updated_at=info.updated_at,
        title_version=info.title_version,
    )


def register_session_catalog_routes(
    router: APIRouter, service: ApplicationService
) -> None:
    """注册列表、人工改名和一次性模型生成接口。"""

    @router.post("/sessions/{session_id}/delete", status_code=204)
    async def delete_session(session_id: str, request: DeleteSessionRequest) -> None:
        ctx = to_session_context(
            session_id, request.app_id, request.user_id, request.agent_id
        )
        await service.delete_session(ctx, hard_delete=request.hard_delete)

    @router.get("/sessions", response_model=SessionListResponse)
    async def list_sessions(
        app_id: str = Query(min_length=1),
        user_id: str = Query(min_length=1),
        agent_id: str | None = Query(default=None, min_length=1),
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    ) -> SessionListResponse:
        try:
            query = SessionListQuery(
                app_id=app_id,
                user_id=user_id,
                agent_id=agent_id,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError as error:
            raise ServiceError(
                kind=ServiceErrorKind.INVALID,
                code="invalid_session_cursor",
                message="会话分页游标无效或查询范围不匹配",
            ) from error
        result = await service.list_sessions(query)
        return SessionListResponse(
            sessions=tuple(to_session_info_response(item) for item in result.sessions),
            next_cursor=result.next_cursor,
        )

    @router.post("/sessions/{session_id}", response_model=SessionInfoResponse)
    async def rename_session(
        session_id: str, request: UpdateTitleRequest
    ) -> SessionInfoResponse:
        ctx = to_session_context(
            session_id, request.app_id, request.user_id, request.agent_id
        )
        return to_session_info_response(
            await service.update_session_title(ctx, request.title)
        )

    @router.post(
        "/sessions/{session_id}/title/generate", response_model=SessionInfoResponse
    )
    async def generate_session_title(
        session_id: str, request: SessionIdentityRequest
    ) -> SessionInfoResponse:
        ctx = to_session_context(
            session_id, request.app_id, request.user_id, request.agent_id
        )
        return to_session_info_response(await service.generate_session_title(ctx))
