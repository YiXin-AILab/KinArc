"""HTTP 同步错误的唯一映射与日志边界。"""

import logging

from agentsystem.common.errors import (
    ServiceError,
    ServiceErrorKind,
)
from agentsystem.common.logging import log_unexpected_exception
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.chat.schemas import ErrorResponse

LOGGER = logging.getLogger(__name__)

_HTTP_STATUS_BY_KIND = {
    ServiceErrorKind.INVALID: 400,
    ServiceErrorKind.UNAUTHORIZED: 401,
    ServiceErrorKind.FORBIDDEN: 403,
    ServiceErrorKind.NOT_FOUND: 404,
    ServiceErrorKind.CONFLICT: 409,
    ServiceErrorKind.PAYLOAD_TOO_LARGE: 413,
    ServiceErrorKind.CAPACITY: 429,
    ServiceErrorKind.UNAVAILABLE: 503,
}


def register_error_handlers(app: FastAPI) -> None:
    """注册已知 ServiceError 与未知程序异常的统一响应。"""

    @app.exception_handler(ServiceError)
    async def service_error_handler(
        request: Request,
        error: ServiceError,
    ) -> JSONResponse:
        del request
        payload = ErrorResponse(code=error.code, message=error.message)
        return JSONResponse(
            status_code=_HTTP_STATUS_BY_KIND[error.kind],
            content=payload.model_dump(mode="json"),
            headers={"Retry-After": "1"}
            if error.kind is ServiceErrorKind.CAPACITY
            else None,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        del request
        error_id = log_unexpected_exception(
            LOGGER,
            "http_request_failed",
            error,
            operation="http.request",
        )
        payload = ErrorResponse(
            code="internal_error",
            message="服务内部错误",
            error_id=error_id,
        )
        return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))


__all__ = ["register_error_handlers"]
