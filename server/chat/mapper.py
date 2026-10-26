"""HTTP DTO 与传输无关 Runtime 应用模型之间的纯映射。"""

from agentsystem.common.models import SessionContext
from agentsystem.message.models import UserContentBlock
from agentsystem.service.models import (
    AgentCatalogView,
    ApplicationStatus,
    CancelCommand,
    MessageSubmissionResult,
    PendingInputsView,
    ResumeCommand,
    SessionMessagesView,
    SessionSnapshot,
    SubmitMessageCommand,
)
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from server.chat.schemas import (
    AgentCatalogItemResponse,
    AgentCatalogResponse,
    ApiSessionStatus,
    CancelSessionRequest,
    MessageSubmissionRequest,
    MessageSubmissionResponse,
    PendingInputResponse,
    PendingInputsResponse,
    ResumeSubmissionRequest,
    SessionMessageResponse,
    SessionMessagesResponse,
    SessionPublicResultResponse,
    SessionRunTimingResponse,
    SessionSnapshotResponse,
    SessionStatusResponse,
    SessionTokenUsageResponse,
)


def to_agent_catalog_response(view: AgentCatalogView) -> AgentCatalogResponse:
    """把 Runtime 目录字段映射为稳定 HTTP DTO。"""
    return AgentCatalogResponse(
        agents=tuple(
            AgentCatalogItemResponse(
                id=item.agent_id,
                name=item.agent_name,
                description=item.description,
                output_mode=item.output_mode,
            )
            for item in view.agents
        )
    )


def to_submit_command(request: MessageSubmissionRequest) -> SubmitMessageCommand:
    """把 HTTP 消息请求映射为 Runtime 命令。"""
    content: tuple[UserContentBlock, ...] = tuple(
        TypeAdapter(UserContentBlock).validate_python(block.model_dump(mode="json"))
        for block in request.content
    )
    return SubmitMessageCommand(
        message_id=request.message_id,
        role=request.role,
        origin=request.origin if request.origin is not None else request.role.value,
        content=content,
        mode=request.mode,
    )


def to_resume_command(request: ResumeSubmissionRequest) -> ResumeCommand:
    """把 HTTP 恢复请求映射为 Runtime 命令。"""
    return ResumeCommand(
        resume_id=request.resume_id,
        resume_token=request.resume_token,
        input=request.input,
    )


def to_cancel_command(request: CancelSessionRequest) -> CancelCommand:
    """把 HTTP 取消请求映射为 Runtime 命令。"""
    del request
    return CancelCommand()


def to_session_context(
    session_id: str,
    app_id: str,
    user_id: str,
    agent_id: str,
) -> SessionContext:
    """把 HTTP 身份字段映射为共享 session 上下文。"""
    try:
        return SessionContext(
            session_id=session_id,
            app_id=app_id,
            user_id=user_id,
            agent_id=agent_id,
        )
    except ValidationError as error:
        raise HTTPException(
            status_code=422, detail="invalid_session_identity"
        ) from error


def to_status_response(
    session_id: str,
    status: ApplicationStatus,
) -> SessionStatusResponse:
    """把 Runtime 受理状态映射为 HTTP 响应。"""
    return SessionStatusResponse(
        session_id=session_id,
        status=ApiSessionStatus(status.value),
    )


def to_message_submission_response(
    session_id: str,
    result: MessageSubmissionResult,
) -> MessageSubmissionResponse:
    """映射不带投递状态的普通消息接收结果。"""
    return MessageSubmissionResponse(
        session_id=session_id,
        message_id=result.message_id,
    )


def to_pending_inputs_response(view: PendingInputsView) -> PendingInputsResponse:
    """映射不暴露 claim 信息的待执行输入。"""
    return PendingInputsResponse(
        session_id=view.session_id,
        inputs=tuple(PendingInputResponse(**item.model_dump()) for item in view.inputs),
    )


def to_snapshot_response(snapshot: SessionSnapshot) -> SessionSnapshotResponse:
    """把 Runtime session 快照映射为 HTTP 响应。"""
    result = snapshot.result
    public_result = (
        SessionPublicResultResponse(**result.model_dump())
        if result is not None
        else None
    )
    return SessionSnapshotResponse(
        session_id=snapshot.session_id,
        status=snapshot.status,
        result=public_result,
        usage=(
            SessionTokenUsageResponse(**snapshot.usage.model_dump())
            if snapshot.usage is not None
            else None
        ),
        timing=(
            SessionRunTimingResponse(**snapshot.timing.model_dump())
            if snapshot.timing is not None
            else None
        ),
    )


def to_messages_response(view: SessionMessagesView) -> SessionMessagesResponse:
    """把 Runtime 消息视图映射为 HTTP 响应。"""
    return SessionMessagesResponse(
        session_id=view.session_id,
        messages=tuple(
            SessionMessageResponse(**message.model_dump()) for message in view.messages
        ),
    )
