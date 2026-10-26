"""Runtime 级资产上传与完整流式读取 HTTP adapter。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Annotated

from agentsystem.service.application import ApplicationService
from agentsystem.service.models import AssetContent, AssetRecord
from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import StreamingResponse

from server.chat.schemas import AssetUploadResponse, InputAssetSource

_UPLOAD_CHUNK_BYTES = 64 * 1024


def create_asset_router(service: ApplicationService) -> APIRouter:
    """创建只依赖 ApplicationService 资产端口的公开路由。"""
    router = APIRouter(prefix="/v1")

    @router.post(
        "/assets",
        response_model=AssetUploadResponse,
        response_model_exclude_none=True,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_asset(
        file: Annotated[UploadFile, File()],
    ) -> AssetUploadResponse:
        try:
            record = await service.upload_asset(
                _upload_body(file),
                declared_media_type=file.content_type,
                filename=file.filename,
            )
        finally:
            await file.close()
        return _upload_response(record)

    @router.get("/assets/{asset_id:path}")
    async def read_asset(asset_id: str) -> StreamingResponse:
        manager = service.open_asset(asset_id)
        content = await manager.__aenter__()
        return StreamingResponse(
            _stream_body(manager, content.body),
            media_type=content.media_type,
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "private, no-store",
            },
        )

    return router


async def _upload_body(file: UploadFile) -> AsyncIterator[bytes]:
    """按固定上限读取 multipart 临时文件，避免 Server 整体缓冲。"""
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        yield chunk


async def _stream_body(
    manager: AbstractAsyncContextManager[AssetContent],
    body: AsyncIterator[bytes],
) -> AsyncIterator[bytes]:
    """把响应退出原因传给资产上下文并确保只关闭一次。"""
    try:
        async for chunk in body:
            yield chunk
    except BaseException as error:
        suppress = await manager.__aexit__(type(error), error, error.__traceback__)
        if not suppress:
            raise
    else:
        await manager.__aexit__(None, None, None)


def _upload_response(record: AssetRecord) -> AssetUploadResponse:
    """把 Store 无关记录映射为前端可直接复用的 AssetSource。"""
    source = InputAssetSource(
        asset_id=record.asset_id,
        media_type=record.media_type,
        filename=record.filename,
    )
    return AssetUploadResponse(**record.model_dump(), source=source)


__all__ = ["create_asset_router"]
