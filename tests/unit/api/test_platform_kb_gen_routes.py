"""问题集生成 Job、审核与沉淀 API 的路由合约测试。"""

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.errors import register_error_handlers
from server.platform.catalog import CatalogSnapshot, PlatformCatalog
from server.platform.knowledge.generation import KbGenJobRegistry
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


def _write_kb(root: Path, name: str) -> Path:
    kb_dir = root / "knowledge" / name
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        f"name: {name}\ndescription: {name} 描述\n"
        "retrieval:\n  strategy: bm25\n  top_k: 5\n",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text(
        json.dumps(_DOCS, ensure_ascii=False), encoding="utf-8"
    )
    return kb_dir


def _fake_complete(prompt: str) -> str:
    """按 Prompt 类型分发固定 JSON 输出的确定性 LLM 替身。"""
    if "再生成" in prompt:
        return "[]"
    if "库外问题" in prompt:
        return json.dumps([{"question": "这车能帮我订外卖吗"}], ensure_ascii=False)
    if "边界混淆" in prompt:
        return json.dumps(
            [
                {
                    "question": "工时费 200 元一小时够吗",
                    "expected_doc_ids": ["fee"],
                    "forbidden": ["200 元/小时"],
                    "reference_answer": "工时费按标准收取",
                }
            ],
            ensure_ascii=False,
        )
    if "组合问题" in prompt:
        return json.dumps(
            [
                {
                    "question": "工时费和开户流程分别是什么",
                    "expected_doc_ids": ["fee", "account"],
                    "reference_answer": "分别说明",
                }
            ],
            ensure_ascii=False,
        )
    match = re.search(r"文档 ID：(\S+)", prompt)
    doc_id = match.group(1) if match else "fee"
    return json.dumps(
        {"question": f"{doc_id} 相关问题", "reference_answer": f"{doc_id} 答案"},
        ensure_ascii=False,
    )


def _client(root: Path) -> TestClient:
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
            kb_gen_registry=KbGenJobRegistry(store=store, complete=_fake_complete),
        )
    )
    register_error_handlers(app)
    return TestClient(app)


def _expected_case_id(scope: str, kb_name: str, set_name: str, index: int) -> str:
    """按规格独立重算期望 case id：可读前缀 + 结构化 JSON 的 sha256 截断摘要。"""
    payload = json.dumps([scope, kb_name, set_name, index], ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{scope}-{kb_name}-{set_name}-{index:03d}-{digest}"


def _poll_job(client: TestClient, job_id: str) -> dict[str, Any]:
    """轮询 Job 直至终态；超时失败。"""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/v1/platform/kb-gen-jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"Job 未在限定时间内结束：{job_id}")


def _generate(tmp_path: Path) -> tuple[TestClient, dict[str, Any]]:
    client = _client(tmp_path)
    response = client.post(
        "/v1/platform/knowledge-bases/generate",
        json={"sources": ["pricing"], "guidance": "语气口语化", "count": 10},
    )
    assert response.status_code == 202
    job = _poll_job(client, response.json()["job_id"])
    return client, job


def test_generate_job_writes_pending_question_set(tmp_path: Path) -> None:
    """生成 Job 完成：逐库写问题集，候选全部 pending，统计如实。"""
    _write_kb(tmp_path, "pricing")
    client, job = _generate(tmp_path)

    assert job["status"] == "succeeded"
    result = job["results"][0]
    assert result["source"] == "pricing"
    assert result["status"] == "succeeded"
    set_name = result["question_set"]
    assert result["kept"] > 0
    assert result["type_counts"]["single_doc"] == 3

    detail = client.get(
        f"/v1/platform/knowledge-bases/global/pricing/question_sets/{set_name}"
    )
    assert detail.status_code == 200
    question_set = detail.json()["question_set"]
    assert question_set["name"] == set_name
    assert question_set["source"] == "generated"
    assert question_set["guidance"] == "语气口语化"
    assert all(item["status"] == "pending" for item in question_set["items"])
    assert len(question_set["items"]) == result["kept"]

    listed = client.get("/v1/platform/knowledge-bases/global/pricing/question_sets")
    summary = listed.json()["question_sets"][0]
    assert summary["pending"] == summary["total"] == result["kept"]
    assert summary["accepted"] == 0


def test_generate_unknown_source_and_missing_job(tmp_path: Path) -> None:
    """源知识库不存在 404；轮询不存在的 Job 404。"""
    client = _client(tmp_path)

    response = client.post(
        "/v1/platform/knowledge-bases/generate",
        json={"sources": ["ghost"], "count": 5},
    )
    assert response.status_code == 404

    missing = client.get("/v1/platform/kb-gen-jobs/gen-nope")
    assert missing.status_code == 404


def test_generate_requires_llm_env(tmp_path: Path) -> None:
    """未注入 complete 且缺少 EVAL_JUDGE_LLM_* 时启动即 400。"""
    _write_kb(tmp_path, "pricing")
    store = PlatformStore(
        project_root=tmp_path,
        applications_root=tmp_path / "application",
    )
    app = FastAPI()
    app.include_router(
        create_platform_router(
            store=store,
            catalog=_FakeCatalog(),
            active_agent_ids_provider=lambda: (),
            kb_gen_registry=KbGenJobRegistry(store=store, environ={}),
        )
    )
    register_error_handlers(app)

    response = TestClient(app).post(
        "/v1/platform/knowledge-bases/generate",
        json={"sources": ["pricing"], "count": 5},
    )

    assert response.status_code == 400
    assert "EVAL_JUDGE_LLM" in response.json()["message"]


def test_review_state_machine(tmp_path: Path) -> None:
    """采纳/丢弃/编辑状态机：编辑置 edited，非法状态与越界下标报错。"""
    _write_kb(tmp_path, "pricing")
    client, job = _generate(tmp_path)
    set_name = job["results"][0]["question_set"]
    base = f"/v1/platform/knowledge-bases/global/pricing/question_sets/{set_name}"

    accepted = client.patch(f"{base}/items/0", json={"status": "accepted"})
    assert accepted.status_code == 200
    assert accepted.json()["item"]["status"] == "accepted"
    assert accepted.json()["item"]["edited"] is False

    discarded = client.patch(f"{base}/items/1", json={"status": "discarded"})
    assert discarded.json()["item"]["status"] == "discarded"

    edited = client.patch(
        f"{base}/items/2",
        json={"question": "编辑后的问题", "expected_doc_ids": ["account"]},
    )
    assert edited.json()["item"]["edited"] is True
    assert edited.json()["item"]["question"] == "编辑后的问题"

    bad_status = client.patch(f"{base}/items/0", json={"status": "maybe"})
    assert bad_status.status_code == 400

    unknown_doc = client.patch(f"{base}/items/0", json={"expected_doc_ids": ["ghost"]})
    assert unknown_doc.status_code == 400

    out_of_range = client.patch(f"{base}/items/999", json={"status": "accepted"})
    assert out_of_range.status_code == 404

    accepted_all = client.post(f"{base}/accept-all")
    assert accepted_all.status_code == 200
    summary = accepted_all.json()
    assert summary["pending"] == 0
    assert summary["discarded"] == 1


def test_batch_search_with_question_set_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """batch-search 的 question_set 输入源：丢弃题排除，指标正常汇总。"""
    _write_kb(tmp_path, "pricing")

    async def fake_judge(**kwargs: object) -> tuple[float, str]:
        del kwargs
        return 0.9, "覆盖良好"

    monkeypatch.setattr(
        "server.platform.knowledge.routes.judge_answer_coverage", fake_judge
    )
    client, job = _generate(tmp_path)
    set_name = job["results"][0]["question_set"]
    base = f"/v1/platform/knowledge-bases/global/pricing/question_sets/{set_name}"
    # 第一题（fee 相关问题）丢弃，其余采纳。
    client.patch(f"{base}/items/0", json={"status": "discarded"})
    client.post(f"{base}/accept-all")

    response = client.post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={
            "question_set": set_name,
            "judge_llm": {
                "model": "openai/fake",
                "api_key": "fake-key",
                "base_url": "https://example.invalid",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    questions = [item["question"] for item in body["results"]]
    assert "fee 相关问题" not in questions
    assert len(body["results"]) == job["results"][0]["kept"] - 1
    assert all(item["error"] is None for item in body["results"])
    # 生成题的 reference_answer 自动带入文本题指标。
    answer_items = [
        item for item in body["results"] if item["answer_metrics"] is not None
    ]
    assert answer_items
    assert all(item["answer_metrics"]["coverage"] == 0.9 for item in answer_items)


def test_batch_search_question_source_mutually_exclusive(tmp_path: Path) -> None:
    """questions 与 question_set 同时给或都不给返回 422。"""
    _write_kb(tmp_path, "pricing")
    client = _client(tmp_path)

    both = client.post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={"questions": [{"question": "q"}], "question_set": "s"},
    )
    assert both.status_code == 422

    neither = client.post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={},
    )
    assert neither.status_code == 422

    missing_set = client.post(
        "/v1/platform/knowledge-bases/global/pricing/batch-search",
        json={"question_set": "ghost"},
    )
    assert missing_set.status_code == 404


def test_to_eval_dataset_roundtrip(tmp_path: Path) -> None:
    """accepted 条目沉淀为 EvalCase；重复沉淀按 id 跳过；无采纳报 400。"""
    _write_kb(tmp_path, "pricing")
    task_dir = tmp_path / "eval_tasks" / "task1"
    task_dir.mkdir(parents=True)
    (task_dir / "config.yaml").write_text("agent_id: demo\n", encoding="utf-8")
    client, job = _generate(tmp_path)
    set_name = job["results"][0]["question_set"]
    base = f"/v1/platform/knowledge-bases/global/pricing/question_sets/{set_name}"

    no_accepted = client.post(
        f"{base}/to-eval-dataset", json={"task_id": "task1", "dataset": "gen"}
    )
    assert no_accepted.status_code == 400

    client.patch(f"{base}/items/0", json={"status": "accepted"})
    client.patch(f"{base}/items/1", json={"status": "accepted"})

    converted = client.post(
        f"{base}/to-eval-dataset", json={"task_id": "task1", "dataset": "gen"}
    )
    assert converted.status_code == 200
    assert converted.json() == {
        "task_id": "task1",
        "dataset": "gen",
        "appended": 2,
        "skipped": 0,
        "total_cases": 2,
    }

    again = client.post(
        f"{base}/to-eval-dataset", json={"task_id": "task1", "dataset": "gen"}
    )
    assert again.json()["appended"] == 0
    assert again.json()["skipped"] == 2

    cases = json.loads((task_dir / "datasets" / "gen.json").read_text("utf-8"))
    assert [case["id"] for case in cases] == [
        _expected_case_id("global", "pricing", set_name, 0),
        _expected_case_id("global", "pricing", set_name, 1),
    ]

    missing_task = client.post(
        f"{base}/to-eval-dataset", json={"task_id": "ghost", "dataset": "gen"}
    )
    assert missing_task.status_code == 404


def test_to_eval_dataset_batched_accept_keeps_every_item(tmp_path: Path) -> None:
    """分批采纳：先沉淀 A/C 再沉淀 B，最终三条齐全且各只出现一次。"""
    _write_kb(tmp_path, "pricing")
    task_dir = tmp_path / "eval_tasks" / "task1"
    task_dir.mkdir(parents=True)
    (task_dir / "config.yaml").write_text("agent_id: demo\n", encoding="utf-8")
    client, job = _generate(tmp_path)
    set_name = job["results"][0]["question_set"]
    base = f"/v1/platform/knowledge-bases/global/pricing/question_sets/{set_name}"

    detail = client.get(base).json()["question_set"]
    questions = [item["question"] for item in detail["items"][:3]]

    client.patch(f"{base}/items/0", json={"status": "accepted"})
    client.patch(f"{base}/items/2", json={"status": "accepted"})
    first = client.post(
        f"{base}/to-eval-dataset", json={"task_id": "task1", "dataset": "gen"}
    )
    assert first.json()["appended"] == 2

    client.patch(f"{base}/items/1", json={"status": "accepted"})
    second = client.post(
        f"{base}/to-eval-dataset", json={"task_id": "task1", "dataset": "gen"}
    )
    assert second.json()["appended"] == 1
    assert second.json()["total_cases"] == 3

    cases = json.loads((task_dir / "datasets" / "gen.json").read_text("utf-8"))
    assert {case["id"] for case in cases} == {
        _expected_case_id("global", "pricing", set_name, 0),
        _expected_case_id("global", "pricing", set_name, 1),
        _expected_case_id("global", "pricing", set_name, 2),
    }
    assert [case["input"] for case in cases] == [
        questions[0],
        questions[2],
        questions[1],
    ]


def test_to_eval_dataset_same_set_name_across_kbs(tmp_path: Path) -> None:
    """两个知识库共用同一 set name 沉淀到同一数据集：两边条目都保留。"""
    _write_kb(tmp_path, "pricing")
    _write_kb(tmp_path, "tech")
    task_dir = tmp_path / "eval_tasks" / "task1"
    task_dir.mkdir(parents=True)
    (task_dir / "config.yaml").write_text("agent_id: demo\n", encoding="utf-8")
    client = _client(tmp_path)

    for kb_name, question in (("pricing", "工时费怎么算"), ("tech", "支持哪些接口")):
        set_dir = tmp_path / "knowledge" / kb_name / "question_sets"
        set_dir.mkdir()
        (set_dir / "shared.json").write_text(
            json.dumps(
                {
                    "name": "shared",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "source": "manual",
                    "items": [{"question": question, "status": "accepted"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        response = client.post(
            f"/v1/platform/knowledge-bases/global/{kb_name}"
            "/question_sets/shared/to-eval-dataset",
            json={"task_id": "task1", "dataset": "gen"},
        )
        assert response.status_code == 200
        assert response.json()["appended"] == 1

    cases = json.loads((task_dir / "datasets" / "gen.json").read_text("utf-8"))
    assert [case["id"] for case in cases] == [
        _expected_case_id("global", "pricing", "shared", 0),
        _expected_case_id("global", "tech", "shared", 0),
    ]
    assert {case["input"] for case in cases} == {"工时费怎么算", "支持哪些接口"}
    assert {case["input"] for case in cases} == {"工时费怎么算", "支持哪些接口"}


def _write_app_kb(root: Path, agent_id: str, kb_name: str) -> Path:
    """创建最小应用目录（agent_id 配置 + 应用级知识库）。"""
    config_dir = root / "application" / agent_id / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "agent.yaml").write_text(f"agent_id: {agent_id}\n", encoding="utf-8")
    kb_dir = root / "application" / agent_id / "knowledge" / kb_name
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb.yaml").write_text(
        f"name: {kb_name}\ndescription: {kb_name} 描述\n"
        "retrieval:\n  strategy: bm25\n  top_k: 5\n",
        encoding="utf-8",
    )
    (kb_dir / "documents.json").write_text(
        json.dumps(_DOCS, ensure_ascii=False), encoding="utf-8"
    )
    return kb_dir


def _write_manual_set(kb_dir: Path, question: str) -> None:
    set_dir = kb_dir / "question_sets"
    set_dir.mkdir()
    (set_dir / "gen.json").write_text(
        json.dumps(
            {
                "name": "gen",
                "created_at": "2026-01-01T00:00:00+00:00",
                "source": "manual",
                "items": [{"question": question, "status": "accepted"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_to_eval_dataset_hyphenated_scope_kb_no_collision(tmp_path: Path) -> None:
    """回归：scope=sales-a/kb=pricing 与 scope=sales/kb=a-pricing 同名 set
    沉淀到同一数据集，旧拼接 id 会碰撞丢数据，修复后两条都保留。"""
    _write_app_kb(tmp_path, "sales-a", "pricing")
    _write_app_kb(tmp_path, "sales", "a-pricing")
    task_dir = tmp_path / "eval_tasks" / "task1"
    task_dir.mkdir(parents=True)
    (task_dir / "config.yaml").write_text("agent_id: demo\n", encoding="utf-8")
    client = _client(tmp_path)

    for scope, kb_name, question in (
        ("sales-a", "pricing", "来源一问题"),
        ("sales", "a-pricing", "来源二问题"),
    ):
        _write_manual_set(
            tmp_path / "application" / scope / "knowledge" / kb_name, question
        )
        response = client.post(
            f"/v1/platform/knowledge-bases/{scope}/{kb_name}"
            "/question_sets/gen/to-eval-dataset",
            json={"task_id": "task1", "dataset": "ds"},
        )
        assert response.status_code == 200
        assert response.json()["appended"] == 1
        assert response.json()["skipped"] == 0

    cases = json.loads((task_dir / "datasets" / "ds.json").read_text("utf-8"))
    assert [case["id"] for case in cases] == [
        _expected_case_id("sales-a", "pricing", "gen", 0),
        _expected_case_id("sales", "a-pricing", "gen", 0),
    ]
    assert {case["input"] for case in cases} == {"来源一问题", "来源二问题"}
