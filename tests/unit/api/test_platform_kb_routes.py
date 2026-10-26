"""平台知识库 API（/v1/platform/knowledge-bases）路由合约测试。"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from agentsystem.knowledge import documents as knowledge_documents
from agentsystem.knowledge.gate import GateVerdict
from agentsystem.knowledge.models import RelevanceLevel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.errors import register_error_handlers
from server.platform.catalog import CatalogSnapshot, PlatformCatalog
from server.platform.knowledge import routes as platform_kb
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
    {
        "doc_id": "account",
        "title": "开户流程",
        "category": "流程",
        "content": "开户需要身份证明与手机号",
        "updated_at": "2026-01-01",
    },
    {
        "doc_id": "promo",
        "title": "促销活动",
        "category": "活动",
        "content": "夏季促销活动现已正式开启",
        "updated_at": "2026-01-01",
    },
]


def _write_global_kb(
    root: Path,
    name: str,
    docs: list[dict[str, str]],
    *,
    top_k: int = 5,
    gate: bool = False,
) -> Path:
    kb_dir = root / "knowledge" / name
    kb_dir.mkdir(parents=True)
    gate_block = "relevance_gate:\n  model: openai/small\n" if gate else ""
    (kb_dir / "kb.yaml").write_text(
        f"name: {name}\n"
        f"description: {name} 描述\n"
        f"retrieval:\n  strategy: bm25\n  top_k: {top_k}\n"
        f"{gate_block}",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    return kb_dir


def _write_agent(root: Path, agent_id: str, *, kb_names: tuple[str, ...] = ()) -> Path:
    agent_dir = root / "application" / agent_id
    (agent_dir / "config").mkdir(parents=True)
    (agent_dir / "prompt").mkdir()
    (agent_dir / "prompt" / "system.md").write_text("你是助手。\n", encoding="utf-8")
    kb_block = ""
    if kb_names:
        lines = "\n".join(f"  - name: {name}" for name in kb_names)
        kb_block = f"knowledge_bases:\n{lines}\n"
    (agent_dir / "config" / "agent.yaml").write_text(
        (
            f"agent_id: {agent_id}\n"
            f"agent_name: {agent_id} name\n"
            "prompt: prompt/system.md\n"
            f"{kb_block}"
            "outbound:\n  type: log\n"
            "llm_model:\n  type: litellm\n  model: openai/test-model\n"
        ),
        encoding="utf-8",
    )
    return agent_dir


def _write_app_kb(root: Path, agent_id: str, name: str) -> Path:
    kb_dir = root / "application" / agent_id / "knowledge" / name
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        f"name: {name}\nretrieval:\n  strategy: bm25\n  top_k: 5\n",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text("[]\n", encoding="utf-8")
    return kb_dir


# ---------------------------------------------------------------------------
# 列表与新建
# ---------------------------------------------------------------------------


def test_list_knowledge_bases_includes_scopes_and_references(tmp_path: Path) -> None:
    """列表覆盖全局与应用级库，并扫描 agent.yaml 的 knowledge_bases 引用。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    _write_agent(tmp_path, "agent-a", kb_names=("pricing",))
    _write_app_kb(tmp_path, "agent-a", "private_kb")

    response = _client(tmp_path).get("/v1/platform/knowledge-bases")

    assert response.status_code == 200
    items = {item["name"]: item for item in response.json()["knowledge_bases"]}
    assert items["pricing"]["scope"] == "global"
    assert items["pricing"]["doc_count"] == 3
    assert items["pricing"]["strategy"] == "bm25"
    assert items["pricing"]["top_k"] == 5
    assert items["pricing"]["has_gate"] is False
    assert items["pricing"]["referenced_by"] == ["agent-a"]
    assert items["pricing"]["valid"] is True
    assert items["private_kb"]["scope"] == "agent-a"


def test_list_marks_invalid_manifest(tmp_path: Path) -> None:
    """kb.yaml 无效的库以 valid=False 列出而不是炸掉整个列表。"""
    kb_dir = tmp_path / "knowledge" / "broken"
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text("name: broken\n", encoding="utf-8")

    response = _client(tmp_path).get("/v1/platform/knowledge-bases")

    assert response.status_code == 200
    item = response.json()["knowledge_bases"][0]
    assert item["valid"] is False
    assert item["error"]


def test_create_knowledge_base(tmp_path: Path) -> None:
    """新建库落盘默认 kb.yaml（bm25）与空 documents.json，返回摘要。"""
    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases",
        json={"name": "new_kb", "description": "新库"},
    )

    assert response.status_code == 201
    assert response.json()["knowledge_base"]["name"] == "new_kb"
    kb_dir = tmp_path / "knowledge" / "new_kb"
    manifest = yaml.safe_load((kb_dir / "kb.yaml").read_text(encoding="utf-8"))
    assert manifest["retrieval"] == {"strategy": "bm25", "top_k": 5}
    assert json.loads((kb_dir / "documents.json").read_text(encoding="utf-8")) == []


def test_create_knowledge_base_conflict_and_invalid_name(tmp_path: Path) -> None:
    """重名 409；非法名称与未知 Agent scope 分别 400/404。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    client = _client(tmp_path)

    conflict = client.post("/v1/platform/knowledge-bases", json={"name": "pricing"})
    assert conflict.status_code == 409

    invalid = client.post("/v1/platform/knowledge-bases", json={"name": "bad name!"})
    assert invalid.status_code == 400

    unknown_agent = client.post(
        "/v1/platform/knowledge-bases",
        json={"scope": "no-such-agent", "name": "kb1"},
    )
    assert unknown_agent.status_code == 404


# ---------------------------------------------------------------------------
# kb.yaml 读写
# ---------------------------------------------------------------------------


def test_read_manifest_returns_raw_config(tmp_path: Path) -> None:
    """读取 kb.yaml 原文，检索配置字段完整返回。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).get("/v1/platform/knowledge-bases/global/pricing")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "pricing"
    assert body["scope"] == "global"
    assert body["config"]["retrieval"] == {"strategy": "bm25", "top_k": 5}


def test_update_manifest_rewrites_kb_yaml(tmp_path: Path) -> None:
    """覆盖写 kb.yaml 并保留 ${VAR} 占位符原文。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    new_config = {
        "name": "pricing",
        "description": "更新后的描述",
        "retrieval": {"strategy": "bm25", "top_k": 10},
        "relevance_gate": {
            "model": "openai/small",
            "api_key": "${KB_GATE_API_KEY}",
            "min_relevance": "relevant",
        },
    }

    response = _client(tmp_path).put(
        "/v1/platform/knowledge-bases/global/pricing",
        json={"config": new_config},
    )

    assert response.status_code == 200
    on_disk = yaml.safe_load(
        (tmp_path / "knowledge" / "pricing" / "kb.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["retrieval"]["top_k"] == 10
    assert on_disk["relevance_gate"]["api_key"] == "${KB_GATE_API_KEY}"


def test_update_manifest_rejects_name_change_and_invalid_config(
    tmp_path: Path,
) -> None:
    """name 与目录不一致或结构无效时 400，且不落盘。"""
    kb_dir = _write_global_kb(tmp_path, "pricing", _DOCS)
    before = (kb_dir / "kb.yaml").read_text(encoding="utf-8")
    client = _client(tmp_path)

    renamed = client.put(
        "/v1/platform/knowledge-bases/global/pricing",
        json={"config": {"name": "other", "retrieval": {"strategy": "bm25"}}},
    )
    assert renamed.status_code == 400

    invalid = client.put(
        "/v1/platform/knowledge-bases/global/pricing",
        json={"config": {"name": "pricing"}},
    )
    assert invalid.status_code == 400
    assert (kb_dir / "kb.yaml").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# 密钥占位符与脱敏
# ---------------------------------------------------------------------------

_PLAINTEXT_KEY = "sk-plaintext-leak-check"


@pytest.mark.parametrize(
    "secret_block",
    [
        {
            "retrieval": {
                "strategy": "bm25",
                "embedding": {
                    "model": "openai/emb",
                    "api_key": _PLAINTEXT_KEY,
                },
            }
        },
        {"rerank": {"model": "openai/rerank", "api_key": _PLAINTEXT_KEY}},
        {"relevance_gate": {"model": "openai/gate", "api_key": _PLAINTEXT_KEY}},
    ],
    ids=["embedding", "rerank", "relevance_gate"],
)
def test_update_manifest_rejects_plaintext_api_key(
    tmp_path: Path,
    secret_block: dict[str, Any],
) -> None:
    """secret 字段明文值被拒绝（400），不落盘，响应不回显原始密钥。"""
    kb_dir = _write_global_kb(tmp_path, "pricing", _DOCS)
    before = (kb_dir / "kb.yaml").read_text(encoding="utf-8")
    config: dict[str, Any] = {
        "name": "pricing",
        "retrieval": {"strategy": "bm25", "top_k": 5},
        **secret_block,
    }
    if "retrieval" in secret_block:
        config["retrieval"] = secret_block["retrieval"]

    response = _client(tmp_path).put(
        "/v1/platform/knowledge-bases/global/pricing",
        json={"config": config},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "kb_secret_not_placeholder"
    assert _PLAINTEXT_KEY not in response.text
    assert (kb_dir / "kb.yaml").read_text(encoding="utf-8") == before


def test_update_manifest_accepts_env_placeholders_for_secret_fields(
    tmp_path: Path,
) -> None:
    """三处 secret 字段的 ${ENV_NAME} 占位符可保存并保持占位符原文。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    config = {
        "name": "pricing",
        "retrieval": {
            "strategy": "bm25",
            "top_k": 5,
            "embedding": {
                "model": "openai/emb",
                "api_key": "${KB_EMBEDDING_API_KEY}",
            },
        },
        "rerank": {"model": "openai/rerank", "api_key": "${KB_RERANK_API_KEY}"},
        "relevance_gate": {
            "model": "openai/gate",
            "api_key": "${KB_GATE_API_KEY}",
        },
    }

    response = _client(tmp_path).put(
        "/v1/platform/knowledge-bases/global/pricing",
        json={"config": config},
    )

    assert response.status_code == 200
    on_disk = yaml.safe_load(
        (tmp_path / "knowledge" / "pricing" / "kb.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["retrieval"]["embedding"]["api_key"] == "${KB_EMBEDDING_API_KEY}"
    assert on_disk["rerank"]["api_key"] == "${KB_RERANK_API_KEY}"
    assert on_disk["relevance_gate"]["api_key"] == "${KB_GATE_API_KEY}"
    body_config = response.json()["config"]
    assert body_config["rerank"]["api_key"] == "${KB_RERANK_API_KEY}"


def test_read_manifest_masks_plaintext_secret_on_disk(tmp_path: Path) -> None:
    """磁盘上历史遗留的明文密钥在 GET 响应中被脱敏，不回显原文。"""
    kb_dir = tmp_path / "knowledge" / "pricing"
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        "name: pricing\n"
        "retrieval:\n"
        "  strategy: bm25\n"
        "  top_k: 5\n"
        "  embedding:\n"
        "    model: openai/emb\n"
        f"    api_key: {_PLAINTEXT_KEY}\n"
        "rerank:\n"
        "  model: openai/rerank\n"
        f"  api_key: {_PLAINTEXT_KEY}\n"
        "relevance_gate:\n"
        "  model: openai/gate\n"
        f"  api_key: {_PLAINTEXT_KEY}\n",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text("[]\n", encoding="utf-8")

    response = _client(tmp_path).get("/v1/platform/knowledge-bases/global/pricing")

    assert response.status_code == 200
    assert _PLAINTEXT_KEY not in response.text
    config = response.json()["config"]
    assert config["retrieval"]["embedding"]["api_key"] != _PLAINTEXT_KEY
    assert config["rerank"]["api_key"] != _PLAINTEXT_KEY
    assert config["relevance_gate"]["api_key"] != _PLAINTEXT_KEY


def _set_nested(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = config
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


_SECRET_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("retrieval", "embedding", "api_key"),
    ("rerank", "api_key"),
    ("relevance_gate", "api_key"),
)


@pytest.mark.parametrize(
    ("secret_path", "raw_value", "raw_marker"),
    [
        (path, value, marker)
        for path in _SECRET_FIELD_PATHS
        for value, marker in (
            (987654321, "987654321"),
            (["sk-raw-list-item"], "sk-raw-list-item"),
            ({"token": "sk-raw-nested"}, "sk-raw-nested"),
        )
    ],
    ids=[
        f"{path[-2]}-{kind}"
        for path in _SECRET_FIELD_PATHS
        for kind in ("number", "array", "object")
    ],
)
def test_read_manifest_masks_non_string_secret_on_disk(
    tmp_path: Path,
    secret_path: tuple[str, ...],
    raw_value: Any,
    raw_marker: str,
) -> None:
    """历史 kb.yaml 中非字符串 secret（数字/数组/对象）在 GET 响应中脱敏。"""
    config: dict[str, Any] = {
        "name": "pricing",
        "retrieval": {"strategy": "bm25", "top_k": 5},
    }
    _set_nested(config, secret_path, raw_value)
    kb_dir = tmp_path / "knowledge" / "pricing"
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    (kb_dir / "documents.json").write_text("[]\n", encoding="utf-8")

    response = _client(tmp_path).get("/v1/platform/knowledge-bases/global/pricing")

    assert response.status_code == 200
    assert raw_marker not in response.text
    body_config = response.json()["config"]
    node = body_config
    for key in secret_path[:-1]:
        node = node[key]
    assert node[secret_path[-1]] == "********"


def test_read_manifest_preserves_placeholder_and_missing_secret_fields(
    tmp_path: Path,
) -> None:
    """合法 ${ENV_NAME} 占位符原样返回；缺失的 secret 字段不会被新增 key。"""
    kb_dir = tmp_path / "knowledge" / "pricing"
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        "name: pricing\n"
        "retrieval:\n"
        "  strategy: bm25\n"
        "  top_k: 5\n"
        "  embedding:\n"
        "    model: openai/emb\n"
        "    api_key: ${KB_EMBEDDING_API_KEY}\n"
        "rerank:\n"
        "  model: openai/rerank\n",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text("[]\n", encoding="utf-8")

    response = _client(tmp_path).get("/v1/platform/knowledge-bases/global/pricing")

    assert response.status_code == 200
    config = response.json()["config"]
    assert config["retrieval"]["embedding"]["api_key"] == "${KB_EMBEDDING_API_KEY}"
    assert "api_key" not in config["rerank"]
    assert "relevance_gate" not in config


async def test_search_slow_prepare_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """慢速同步检索准备（文件读取/索引构建）被隔离，事件循环协程仍可推进。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    original_load = knowledge_documents.load_documents

    def slow_load_documents(path: Path) -> Any:
        time.sleep(0.4)
        return original_load(path)

    monkeypatch.setattr(platform_kb, "load_documents", slow_load_documents)

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
                "/v1/platform/knowledge-bases/global/pricing/search",
                json={"query": "工时费"},
            )
        finally:
            stop.set()
            await ticker_task

    assert response.status_code == 200
    assert ticks >= 5


# ---------------------------------------------------------------------------
# 文档 CRUD
# ---------------------------------------------------------------------------


def test_doc_crud_roundtrip(tmp_path: Path) -> None:
    """新增/读取/替换/删除文档的完整闭环。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    client = _client(tmp_path)
    new_doc = {
        "doc_id": "warranty",
        "title": "质保政策",
        "category": "政策",
        "content": "整车质保三年不限里程",
        "updated_at": "2026-02-01",
    }

    created = client.post(
        "/v1/platform/knowledge-bases/global/pricing/docs", json=new_doc
    )
    assert created.status_code == 201

    listed = client.get("/v1/platform/knowledge-bases/global/pricing/docs")
    assert [doc["doc_id"] for doc in listed.json()["docs"]] == [
        "fee",
        "account",
        "promo",
        "warranty",
    ]

    replaced = client.put(
        "/v1/platform/knowledge-bases/global/pricing/docs/warranty",
        json={**new_doc, "content": "整车质保三年或十万公里"},
    )
    assert replaced.status_code == 200
    assert replaced.json()["doc"]["content"] == "整车质保三年或十万公里"

    deleted = client.delete("/v1/platform/knowledge-bases/global/pricing/docs/warranty")
    assert deleted.status_code == 200
    assert deleted.json()["doc"]["doc_id"] == "warranty"
    remaining = client.get("/v1/platform/knowledge-bases/global/pricing/docs")
    assert [doc["doc_id"] for doc in remaining.json()["docs"]] == [
        "fee",
        "account",
        "promo",
    ]


def test_doc_error_branches(tmp_path: Path) -> None:
    """重复 409、doc_id 不一致 400、不存在 404、非法 ID 400。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    client = _client(tmp_path)

    duplicate = client.post(
        "/v1/platform/knowledge-bases/global/pricing/docs", json=_DOCS[0]
    )
    assert duplicate.status_code == 409

    mismatch = client.put(
        "/v1/platform/knowledge-bases/global/pricing/docs/fee",
        json={**_DOCS[0], "doc_id": "other"},
    )
    assert mismatch.status_code == 400

    missing = client.delete("/v1/platform/knowledge-bases/global/pricing/docs/ghost")
    assert missing.status_code == 404

    bad_id = client.post(
        "/v1/platform/knowledge-bases/global/pricing/docs",
        json={**_DOCS[0], "doc_id": "../escape"},
    )
    assert bad_id.status_code == 400

    unknown_kb = client.get("/v1/platform/knowledge-bases/global/ghost/docs")
    assert unknown_kb.status_code == 404


# ---------------------------------------------------------------------------
# 检索测试
# ---------------------------------------------------------------------------


def test_search_returns_chunks_with_scores(tmp_path: Path) -> None:
    """bm25 检索返回片段与得分；无裁判时无 gate 字段且不标记 filtered。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/search",
        json={"query": "工时费如何收取"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["no_relevant_docs"] is False
    first = body["chunks"][0]
    assert first["doc_id"] == "fee"
    assert first["score"] > 0
    assert first["gate_relevance"] is None
    assert first["filtered"] is False


def test_search_doc_scope_restricts_candidates(tmp_path: Path) -> None:
    """doc_scope 圈定时只在子集内检索；未知 doc_id 返回 400。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    client = _client(tmp_path)

    scoped = client.post(
        "/v1/platform/knowledge-bases/global/pricing/search",
        json={"query": "工时费如何收取", "doc_scope": ["account"]},
    )
    assert scoped.status_code == 200
    assert [chunk["doc_id"] for chunk in scoped.json()["chunks"]] == ["account"]

    unknown = client.post(
        "/v1/platform/knowledge-bases/global/pricing/search",
        json={"query": "工时费", "doc_scope": ["ghost"]},
    )
    assert unknown.status_code == 400


def test_search_strategy_override_requires_embedding(tmp_path: Path) -> None:
    """把 bm25 库覆盖为 vector 策略且未配置 embedding 时返回 400。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/search",
        json={"query": "工时费", "strategy": "vector"},
    )

    assert response.status_code == 400
    assert "embedding" in response.json()["message"]


def _fake_gate_class(verdicts: dict[str, RelevanceLevel]) -> type:
    """构造按 doc_id 返回固定判定的 RelevanceGate 替身。"""

    class _FakeGate:
        def __init__(self, config: Any) -> None:
            del config

        async def judge(self, query: str, documents: Any) -> list[GateVerdict]:
            del query
            return [
                GateVerdict(
                    doc_id=document.doc_id,
                    relevance=verdicts.get(document.doc_id, RelevanceLevel.RELEVANT),
                    reason="测试判定",
                )
                for document in documents
            ]

    return _FakeGate


def test_search_marks_gate_filtered_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用裁判时被剔除的候选以 filtered=True 返回，并携带裁判理由。"""
    _write_global_kb(tmp_path, "pricing", _DOCS, gate=True)
    monkeypatch.setattr(
        platform_kb,
        "RelevanceGate",
        _fake_gate_class({"account": RelevanceLevel.IRRELEVANT}),
    )

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/search",
        json={"query": "工时费 开户"},
    )

    assert response.status_code == 200
    chunks = {chunk["doc_id"]: chunk for chunk in response.json()["chunks"]}
    assert chunks["account"]["filtered"] is True
    assert chunks["account"]["gate_relevance"] == "irrelevant"
    assert chunks["account"]["gate_reason"] == "测试判定"
    assert chunks["fee"]["filtered"] is False
    assert response.json()["no_relevant_docs"] is False


def test_search_gate_override_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gate: {enabled: false} 关闭库默认裁判，只影响本次调用。"""
    kb_dir = _write_global_kb(tmp_path, "pricing", _DOCS, gate=True)
    monkeypatch.setattr(
        platform_kb,
        "RelevanceGate",
        _fake_gate_class({"fee": RelevanceLevel.IRRELEVANT}),
    )

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/search",
        json={"query": "工时费", "gate": {"enabled": False}},
    )

    assert response.status_code == 200
    assert all(
        chunk["filtered"] is False and chunk["gate_relevance"] is None
        for chunk in response.json()["chunks"]
    )
    # 不落盘：库默认裁判配置仍在。
    assert "relevance_gate" in (kb_dir / "kb.yaml").read_text(encoding="utf-8")


def test_search_missing_kb_returns_404(tmp_path: Path) -> None:
    """检索不存在的库返回 404。"""
    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/ghost/search",
        json={"query": "工时费"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 批量检索测试
# ---------------------------------------------------------------------------


async def _fake_coverage_judge(
    *,
    config: Any,
    question: str,
    reference_answer: str,
    content: str,
) -> tuple[float, str]:
    del config, question, content
    if "无答案可覆盖" in reference_answer:
        raise RuntimeError("judge down")
    return 0.75, "覆盖大部分要点"


def test_batch_search_metrics_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批测返回逐题片段、doc_id 指标（含全库排名诊断）、覆盖分与分类汇总。"""
    _write_global_kb(tmp_path, "pricing", _DOCS, top_k=1)
    monkeypatch.setattr(platform_kb, "judge_answer_coverage", _fake_coverage_judge)

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={
            "questions": [
                {"question": "工时费如何收取", "expected_doc_ids": ["fee"]},
                {
                    "question": "开户需要什么材料",
                    "expected_doc_ids": ["fee"],
                    "reference_answer": "开户需要身份证明与手机号",
                },
                {"question": "工时费"},
            ],
            "judge_llm": {
                "model": "openai/fake",
                "api_key": "fake-key",
                "base_url": "https://example.invalid",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    first, second, third = body["results"]

    # top_k=1 命中期望文档：Top1/Top3/集合召回全部成立。
    assert first["doc_metrics"]["top1_hit"] is True
    assert first["doc_metrics"]["set_recall"] is True
    assert first["doc_metrics"]["missing"] == []

    # top_k=1 下 fee 未进候选：诊断给出全库实际排名（2），非裁判误杀。
    assert second["doc_metrics"]["set_recall"] is False
    assert second["doc_metrics"]["gate_false_kills"] == []
    missing = second["doc_metrics"]["missing"]
    assert missing == [{"doc_id": "fee", "rank": 2, "gate_filtered": False}]
    assert second["answer_metrics"]["coverage"] == 0.75
    assert second["answer_metrics"]["rationale"] == "覆盖大部分要点"
    assert second["answer_metrics"]["diff_note"]
    assert "份证" in second["answer_metrics"]["diff"]["matched"]

    assert third["doc_metrics"] is None
    assert third["answer_metrics"] is None
    assert third["error"] is None

    summary = body["summary"]
    assert summary["doc_questions"]["count"] == 2
    assert summary["doc_questions"]["top1_rate"] == 0.5
    assert summary["doc_questions"]["set_recall_rate"] == 0.5
    assert summary["answer_questions"]["count"] == 1
    assert summary["answer_questions"]["average_coverage"] == 0.75


def test_batch_search_gate_false_kill_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期望文档被裁判剔除计入误杀，并在 missing 中标注 gate_filtered。"""
    _write_global_kb(tmp_path, "pricing", _DOCS, gate=True)
    monkeypatch.setattr(
        platform_kb,
        "RelevanceGate",
        _fake_gate_class({"fee": RelevanceLevel.IRRELEVANT}),
    )

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={
            "questions": [
                {"question": "工时费如何收取", "expected_doc_ids": ["fee"]},
            ]
        },
    )

    assert response.status_code == 200
    metrics = response.json()["results"][0]["doc_metrics"]
    assert metrics["gate_false_kills"] == ["fee"]
    assert metrics["missing"] == [{"doc_id": "fee", "rank": 1, "gate_filtered": True}]
    assert response.json()["summary"]["doc_questions"]["gate_false_kill_count"] == 1


def test_batch_search_question_failure_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单题检索失败记 error 不炸整批，且不计入指标汇总。"""

    class _ExplodingGate:
        def __init__(self, config: Any) -> None:
            del config

        async def judge(self, query: str, documents: Any) -> list[GateVerdict]:
            if "爆炸" in query:
                raise RuntimeError("gate exploded")
            return [
                GateVerdict(
                    doc_id=document.doc_id,
                    relevance=RelevanceLevel.RELEVANT,
                    reason="",
                )
                for document in documents
            ]

    _write_global_kb(tmp_path, "pricing", _DOCS, gate=True)
    monkeypatch.setattr(platform_kb, "RelevanceGate", _ExplodingGate)

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={
            "questions": [
                {"question": "爆炸", "expected_doc_ids": ["fee"]},
                {"question": "工时费", "expected_doc_ids": ["fee"]},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "题目执行失败" in body["results"][0]["error"]
    assert body["results"][1]["error"] is None
    assert body["summary"]["doc_questions"]["count"] == 1


def test_batch_search_requires_judge_llm_for_reference_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """含 reference_answer 且无可用的裁判模型配置时返回 400。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    for variable in (
        "EVAL_JUDGE_LLM_MODEL",
        "EVAL_JUDGE_LLM_API_KEY",
        "EVAL_JUDGE_LLM_BASE_URL",
    ):
        monkeypatch.delenv(variable, raising=False)

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={"questions": [{"question": "q", "reference_answer": "a"}]},
    )

    assert response.status_code == 400
    assert "EVAL_JUDGE_LLM" in response.json()["message"]


def test_batch_search_judge_failure_degrades_to_answer_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裁判调用失败时该题 answer_metrics 记 error、coverage=None，题目本身不失败。"""
    _write_global_kb(tmp_path, "pricing", _DOCS)
    monkeypatch.setattr(platform_kb, "judge_answer_coverage", _fake_coverage_judge)

    response = _client(tmp_path).post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={
            "questions": [
                {"question": "工时费", "reference_answer": "无答案可覆盖"},
            ],
            "judge_llm": {
                "model": "openai/fake",
                "api_key": "fake-key",
                "base_url": "https://example.invalid",
            },
        },
    )

    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["error"] is None
    assert item["answer_metrics"]["coverage"] is None
    assert "裁判评分失败" in item["answer_metrics"]["error"]
    summary = response.json()["summary"]["answer_questions"]
    assert summary["count"] == 1
    assert summary["judged_count"] == 0
    assert summary["average_coverage"] is None
