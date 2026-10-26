"""Runtime 级资产上传与完整流式读取 HTTP 合约。"""

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager

from agentsystem.asset import AssetContent, AssetRecord
from agentsystem.service.application import ApplicationService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.chat.assets import create_asset_router

PNG_BYTES = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010804000000")


class _Assets(ApplicationService):
    """在 HTTP 边界外记录完整上传和 reader 生命周期。"""

    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str | None, str | None]] = []
        self.opened: list[str] = []
        self.reader_closed = False

    async def upload_asset(
        self,
        body: AsyncIterable[bytes],
        *,
        declared_media_type: str | None,
        filename: str | None,
    ) -> AssetRecord:
        value = b"".join([chunk async for chunk in body])
        self.uploads.append((value, declared_media_type, filename))
        return AssetRecord(
            asset_id="group2/M00/demo.png",
            media_type="image/png",
            filename=filename,
            size_bytes=len(value),
            checksum_sha256="a" * 64,
        )

    @asynccontextmanager
    async def open_asset(self, asset_id: str) -> AsyncIterator[AssetContent]:
        self.opened.append(asset_id)
        try:
            yield AssetContent(media_type="image/png", body=_body(PNG_BYTES))
        finally:
            self.reader_closed = True


async def _body(value: bytes) -> AsyncIterator[bytes]:
    yield value[:8]
    yield value[8:]


def _client(service: _Assets) -> TestClient:
    app = FastAPI()
    app.include_router(create_asset_router(service))
    return TestClient(app)


def test_multipart_upload_returns_record_and_ready_asset_source() -> None:
    """遗漏 source 会迫使前端重复拼装并可能写错 MIME。"""
    service = _Assets()

    response = _client(service).post(
        "/v1/assets",
        files={"file": ("demo.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 201
    assert response.json() == {
        "asset_id": "group2/M00/demo.png",
        "media_type": "image/png",
        "filename": "demo.png",
        "size_bytes": len(PNG_BYTES),
        "checksum_sha256": "a" * 64,
        "source": {
            "type": "asset",
            "asset_id": "group2/M00/demo.png",
            "media_type": "image/png",
            "filename": "demo.png",
        },
    }
    assert service.uploads == [(PNG_BYTES, "image/png", "demo.png")]


def test_read_preserves_path_asset_id_body_headers_and_closes_reader() -> None:
    """普通 str 路由会截断 Hydra 路径型 ID，漏关上下文会耗尽连接池。"""
    service = _Assets()

    response = _client(service).get("/v1/assets/group2/M00/demo.png")

    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["cache-control"] == "private, no-store"
    assert service.opened == ["group2/M00/demo.png"]
    assert service.reader_closed is True
