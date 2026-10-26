"""平台知识库文档上传导入 API（docs/import-preview 与 docs/import）路由测试。"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from agentsystem.knowledge.ingestion import IngestChunk, IngestResult, IngestStats
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.errors import register_error_handlers
from server.platform.catalog import CatalogSnapshot, PlatformCatalog
from server.platform.knowledge import import_routes as platform_kb_import
from server.platform.routes import create_platform_router
from server.platform.store import PlatformStore


class _FakeCatalog(PlatformCatalog):
    """固定快照的目录替身，避免在路由测试中执行影子发现。"""

    def __init__(self) -> None:
        self._snapshot = CatalogSnapshot(
            tools=(),
            middlewares=(),
            context_providers=(),
            outbounds=("log",),
        )


def _build_app(root: Path) -> FastAPI:
    store = PlatformStore(
        project_root=root,
        applications_root=root / "application",
    )
    app = FastAPI()
    app.include_router(
        create_platform_router(
            store=store,
            catalog=_FakeCatalog(),
            active_agent_ids_provider=lambda: (),
        )
    )
    register_error_handlers(app)
    return app


def _client(root: Path) -> TestClient:
    return TestClient(_build_app(root))


_DOCS = [
    {
        "doc_id": "fee",
        "title": "工时费标准",
        "category": "费用",
        "content": "工时费按小时收取标准费用",
        "updated_at": "2026-01-01",
    },
]


def _write_global_kb(root: Path, name: str, docs: list[dict[str, str]]) -> Path:
    kb_dir = root / "knowledge" / name
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        f"name: {name}\nretrieval:\n  strategy: bm25\n  top_k: 5\n",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    return kb_dir


_MARKDOWN = """# 售后质保政策

整车质保三年不限里程，核心零部件享受延长质保服务，易损件按厂家标准执行。
工时费按小时收取标准费用，具体标准以门店公示为准，详询当地授权经销商。

## 索赔流程

客户到店登记后由服务顾问初步判定，符合条件者提交厂家索赔系统审核，
审核周期一般为三到五个工作日，通过后安排免费维修或更换。
"""

_IMPORT_URL = "/v1/platform/knowledge-bases/global/pricing/docs/import"
_PREVIEW_URL = "/v1/platform/knowledge-bases/global/pricing/docs/import-preview"


def _new_doc(doc_id: str) -> dict[str, str]:
    return {
        "doc_id": doc_id,
        "title": f"{doc_id} 标题",
        "category": "政策",
        "content": f"{doc_id} 的正文内容",
        "updated_at": "2026-02-01",
    }


# ---------------------------------------------------------------------------
# 上传预览
# ---------------------------------------------------------------------------


def test_preview_markdown_returns_chunks_and_does_not_persist(tmp_path: Path) -> None:
    """MD 上传返回 chunks/warnings/stats 预览，且不写 documents.json。"""
    kb_dir = _write_global_kb(tmp_path, "pricing", _DOCS)
    before = (kb_dir / "documents.json").read_text(encoding="utf-8")

    response = _client(tmp_path).post(
        _PREVIEW_URL,
        files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["knowledge_base"] == "pricing"
    assert body["filename"] == "policy.md"
    assert body["media_type"] == "markdown"
    assert body["chunks"]
    first = body["chunks"][0]
    assert first["suggested_doc_id"]
    assert first["duplicate_of"] is None
    assert body["stats"]["chunk_count"] == len(body["chunks"])
    assert (kb_dir / "documents.json").read_text(encoding="utf-8") == before


def test_preview_marks_duplicate_chunks(tmp_path: Path) -> None:
    """chunk 内容与库内现有文档完全一致时标记 duplicate_of。"""
    kb_dir = _write_global_kb(tmp_path, "pricing", _DOCS)
    client = _client(tmp_path)
    upload = {"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")}

    content = client.post(_PREVIEW_URL, files=upload).json()["chunks"][0]["content"]
    docs = json.loads((kb_dir / "documents.json").read_text(encoding="utf-8"))
    docs.append({**_new_doc("existing"), "content": content})
    (kb_dir / "documents.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )

    chunks = client.post(_PREVIEW_URL, files=upload).json()["chunks"]
    assert chunks[0]["duplicate_of"] == "existing"
    assert all(chunk["duplicate_of"] in (None, "existing") for chunk in chunks)


@pytest.mark.parametrize("filename", ["notes.txt", "evil.exe"])
def test_preview_rejects_unsupported_extension(tmp_path: Path, filename: str) -> None:
    """白名单外扩展名返回 400，错误码 kb_unsupported_file_type。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        _PREVIEW_URL, files={"file": (filename, b"content", "text/plain")}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "kb_unsupported_file_type"


def test_preview_rejects_oversized_file(tmp_path: Path) -> None:
    """超过 10MB 的文件返回 400，错误码 kb_file_too_large。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        _PREVIEW_URL,
        files={"file": ("big.md", b"a" * (10 * 1024 * 1024 + 1), "text/markdown")},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "kb_file_too_large"


def test_preview_rejects_empty_file(tmp_path: Path) -> None:
    """空文件返回 400，错误码 kb_file_empty。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        _PREVIEW_URL, files={"file": ("empty.md", b"", "text/markdown")}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "kb_file_empty"


def test_preview_rejects_non_utf8_markdown(tmp_path: Path) -> None:
    """非 UTF-8 编码的 .md 返回 400。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        _PREVIEW_URL,
        files={"file": ("gbk.md", "中文内容".encode("gbk"), "text/markdown")},
    )

    assert response.status_code == 400


def test_preview_rejects_binary_disguised_as_markdown(tmp_path: Path) -> None:
    """zip 内容伪装成 .md 被二进制嗅探否决，返回 400。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    zip_bytes = b"PK\x03\x04" + b"\x00" * 200

    response = _client(tmp_path).post(
        _PREVIEW_URL, files={"file": ("fake.md", zip_bytes, "text/markdown")}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "kb_binary_content"


def test_preview_pdf_via_ingest_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF 预览走本模块 ingest 边界；单测替换 ingest_document 不触发 docling。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    seen: list[str] = []

    def fake_ingest(data: bytes, *, filename: str) -> IngestResult:
        seen.append(filename)
        return IngestResult(
            chunks=(
                IngestChunk(
                    index=0,
                    suggested_doc_id="manual_1",
                    suggested_title="第一章",
                    suggested_category="manual",
                    content="PDF 转换出的第一章内容",
                    section_path="第一章",
                    has_table=False,
                    char_count=12,
                ),
            ),
            warnings=(),
            stats=IngestStats(
                chunk_count=1,
                min_chars=12,
                max_chars=12,
                avg_chars=12,
                broken_tables=0,
                dropped_images=0,
                cleaned_lines=0,
            ),
        )

    monkeypatch.setattr(platform_kb_import, "ingest_document", fake_ingest)

    response = _client(tmp_path).post(
        _PREVIEW_URL,
        files={"file": ("manual.pdf", b"%PDF-fake", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert seen == ["manual.pdf"]
    assert body["media_type"] == "pdf"
    assert body["chunks"][0]["suggested_doc_id"] == "manual_1"


async def test_preview_slow_parse_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """慢速同步解析被隔离到线程：事件循环上的独立协程仍能推进。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    def slow_ingest(data: bytes, *, filename: str) -> IngestResult:
        del data, filename
        time.sleep(0.4)
        return IngestResult(
            chunks=(),
            warnings=(),
            stats=IngestStats(
                chunk_count=0,
                min_chars=0,
                max_chars=0,
                avg_chars=0,
                broken_tables=0,
                dropped_images=0,
                cleaned_lines=0,
            ),
        )

    monkeypatch.setattr(platform_kb_import, "ingest_document", slow_ingest)

    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    transport = httpx.ASGITransport(app=_build_app(tmp_path))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        ticker_task = asyncio.create_task(ticker())
        try:
            response = await client.post(
                _PREVIEW_URL,
                files={"file": ("manual.pdf", b"%PDF-fake", "application/pdf")},
            )
        finally:
            stop.set()
            await ticker_task

    assert response.status_code == 200
    assert ticks >= 5


# ---------------------------------------------------------------------------
# 批量入库
# ---------------------------------------------------------------------------


def test_import_docs_appends_and_returns_201(tmp_path: Path) -> None:
    """成功导入返回 201 与 imported 条数，GET docs 可见新文档。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    client = _client(tmp_path)

    response = client.post(
        _IMPORT_URL, json={"docs": [_new_doc("warranty"), _new_doc("claim")]}
    )

    assert response.status_code == 201
    assert response.json() == {"knowledge_base": "pricing", "imported": 2}
    listed = client.get("/v1/platform/knowledge-bases/global/pricing/docs")
    assert [doc["doc_id"] for doc in listed.json()["docs"]] == [
        "fee",
        "warranty",
        "claim",
    ]


def test_import_docs_conflict_with_existing_lists_ids(tmp_path: Path) -> None:
    """与库内现有 doc_id 冲突返回 409，message 列出全部冲突 ID 且不写盘。"""
    kb_dir = _write_global_kb(tmp_path, "pricing", [*_DOCS, _new_doc("promo")])
    before = (kb_dir / "documents.json").read_text(encoding="utf-8")

    response = _client(tmp_path).post(
        _IMPORT_URL,
        json={"docs": [_new_doc("fee"), _new_doc("promo"), _new_doc("new")]},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "kb_doc_exists"
    assert "fee" in body["message"]
    assert "promo" in body["message"]
    assert (kb_dir / "documents.json").read_text(encoding="utf-8") == before


def test_import_docs_conflict_within_request(tmp_path: Path) -> None:
    """请求体内 doc_id 自重复返回 409。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        _IMPORT_URL,
        json={"docs": [_new_doc("warranty"), _new_doc("warranty")]},
    )

    assert response.status_code == 409
    assert "warranty" in response.json()["message"]


def test_import_docs_empty_list_rejected(tmp_path: Path) -> None:
    """空 docs 列表被请求模型拒绝（422）。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(_IMPORT_URL, json={"docs": []})

    assert response.status_code == 422


def test_import_routes_missing_kb_returns_404(tmp_path: Path) -> None:
    """库不存在时预览与导入均返回 404。"""
    client = _client(tmp_path)
    missing_preview = "/v1/platform/knowledge-bases/global/ghost/docs/import-preview"
    missing_import = "/v1/platform/knowledge-bases/global/ghost/docs/import"

    preview = client.post(
        missing_preview,
        files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
    )
    assert preview.status_code == 404

    response = client.post(missing_import, json={"docs": [_new_doc("a")]})
    assert response.status_code == 404
