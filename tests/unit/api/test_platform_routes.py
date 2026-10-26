"""配置平台 HTTP 路由（/v1/platform）合约测试。"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.errors import register_error_handlers
from server.platform.catalog import CatalogEntry, CatalogSnapshot, PlatformCatalog
from server.platform.routes import create_platform_router
from server.platform.store import PlatformStore


class _FakeCatalog(PlatformCatalog):
    """固定快照的目录替身，避免在路由测试中执行影子发现。"""

    def __init__(self) -> None:
        self._snapshot = CatalogSnapshot(
            tools=(
                CatalogEntry(
                    name="kb_search",
                    source="builtin",
                    description="检索知识库",
                ),
                CatalogEntry(
                    name="get_user_information_by_id",
                    source="application",
                    description="按 ID 查用户",
                    agent_id="banking-agent",
                ),
                CatalogEntry(
                    name="demo_echo",
                    source="application",
                    description="回显",
                    agent_id="demo-agent",
                ),
            ),
            middlewares=(
                CatalogEntry(name="provider-writeback", source="builtin"),
                CatalogEntry(
                    name="banking-guard",
                    source="application",
                    agent_id="banking-agent",
                ),
            ),
            context_providers=(CatalogEntry(name="datetime", source="builtin"),),
            outbounds=("log", "stream"),
        )


def _client(
    root: Path,
    *,
    active: tuple[str, ...] = (),
) -> TestClient:
    store = PlatformStore(
        project_root=root,
        applications_root=root / "application",
    )
    app = FastAPI()
    app.include_router(
        create_platform_router(
            store=store,
            catalog=_FakeCatalog(),
            active_agent_ids_provider=lambda: active,
        )
    )
    register_error_handlers(app)
    return TestClient(app)


def _write_agent(
    root: Path,
    directory: str,
    agent_id: str,
    *,
    enabled_skills: list[str] | None = None,
) -> Path:
    agent_dir = root / "application" / directory
    (agent_dir / "config").mkdir(parents=True)
    (agent_dir / "prompt").mkdir()
    (agent_dir / "prompt" / "system.md").write_text("你是助手。\n", encoding="utf-8")
    skills_line = ""
    if enabled_skills is not None:
        skills_line = f"enabled_skills: [{', '.join(enabled_skills)}]\n"
    (agent_dir / "config" / "agent.yaml").write_text(
        (
            f"agent_id: {agent_id}\n"
            f"agent_name: {agent_id} name\n"
            "prompt: prompt/system.md\n"
            f"{skills_line}"
            "outbound:\n  type: log\n"
            "llm_model:\n  type: litellm\n  model: openai/test-model\n"
        ),
        encoding="utf-8",
    )
    return agent_dir


def _config(agent_id: str, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_name": "演示 Agent",
        "prompt": "prompt/system.md",
        "outbound": {"type": "log"},
        "llm_model": {"type": "litellm", "model": "openai/test-model"},
    }
    config.update(overrides)
    return config


_PLAINTEXT_API_KEY = "sk-plaintext-platform-secret-0001"


def _write_agent_with_api_key(
    root: Path,
    directory: str,
    agent_id: str,
    api_key_yaml: str,
) -> Path:
    agent_dir = root / "application" / directory
    (agent_dir / "config").mkdir(parents=True)
    (agent_dir / "prompt").mkdir()
    (agent_dir / "prompt" / "system.md").write_text("你是助手。\n", encoding="utf-8")
    (agent_dir / "config" / "agent.yaml").write_text(
        (
            f"agent_id: {agent_id}\n"
            f"agent_name: {agent_id} name\n"
            "prompt: prompt/system.md\n"
            "outbound:\n  type: log\n"
            "llm_model:\n  type: litellm\n  model: openai/test-model\n"
            f"  api_key: {api_key_yaml}\n"
        ),
        encoding="utf-8",
    )
    return agent_dir


def _write_skill(scope_root: Path, name: str) -> Path:
    skill_dir = scope_root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\n---\n\n正文。\n",
        encoding="utf-8",
    )
    return skill_dir


def test_list_agents_returns_disk_view_with_status(tmp_path: Path) -> None:
    """磁盘列表包含待重启与配置无效的 Agent。"""
    _write_agent(tmp_path, "active_dir", "active-agent")
    _write_agent(tmp_path, "pending_dir", "pending-agent")
    broken = tmp_path / "application" / "broken_dir" / "config"
    broken.mkdir(parents=True)
    (broken / "agent.yaml").write_text("agent_id: [\n", encoding="utf-8")
    client = _client(tmp_path, active=("active-agent",))

    response = client.get("/v1/platform/agents")

    assert response.status_code == 200
    agents = {item["agent_id"]: item for item in response.json()["agents"]}
    assert agents["active-agent"]["status"] == "active"
    assert agents["pending-agent"]["status"] == "pending"
    assert agents["broken_dir"]["status"] == "invalid"
    assert agents["broken_dir"]["error"]
    assert agents["pending-agent"]["model"] == "openai/test-model"


def test_create_agent_persists_files_without_interpolating_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建只保存 `${VAR}` 占位符原文，即使环境变量存在也不回显其值。"""
    monkeypatch.setenv("PLATFORM_ROUTE_SECRET", "secret-value-must-not-leak")
    client = _client(tmp_path)
    config = _config(
        "created-agent",
        llm_model={
            "type": "litellm",
            "model": "openai/x",
            "api_key": "${PLATFORM_ROUTE_SECRET}",
        },
    )

    response = client.post(
        "/v1/platform/agents",
        json={"config": config, "prompt_text": "你是新 Agent。"},
    )

    assert response.status_code == 201
    assert response.json()["agent"]["status"] == "pending"
    saved = (
        tmp_path / "application" / "created-agent" / "config" / "agent.yaml"
    ).read_text(encoding="utf-8")
    assert "${PLATFORM_ROUTE_SECRET}" in saved
    assert "secret-value-must-not-leak" not in saved
    assert "secret-value-must-not-leak" not in response.text


def test_create_agent_conflict_and_validation_error(tmp_path: Path) -> None:
    """重复创建返回 409；结构校验失败返回 400 且带字段级错误。"""
    _write_agent(tmp_path, "dup", "dup-agent")
    client = _client(tmp_path)

    conflict = client.post(
        "/v1/platform/agents",
        json={"config": _config("dup-agent"), "prompt_text": "x"},
    )
    invalid = client.post(
        "/v1/platform/agents",
        json={"config": {"agent_id": "x"}, "prompt_text": "x"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "agent_already_exists"
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "agent_config_invalid"
    assert "llm_model" in invalid.json()["message"]


def test_read_update_delete_agent_roundtrip(tmp_path: Path) -> None:
    """读取返回配置原文与 Prompt；更新拒绝改名；删除后 404。"""
    _write_agent(tmp_path, "demo_dir", "demo-agent")
    client = _client(tmp_path)

    detail = client.get("/v1/platform/agents/demo-agent")
    assert detail.status_code == 200
    assert detail.json()["config"]["agent_id"] == "demo-agent"
    assert detail.json()["prompt_text"] == "你是助手。\n"
    assert detail.json()["status"] == "pending"

    updated = client.put(
        "/v1/platform/agents/demo-agent",
        json={
            "config": _config("demo-agent", agent_name="新名字"),
            "prompt_text": "新。",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["agent"]["agent_name"] == "新名字"

    renamed = client.put(
        "/v1/platform/agents/demo-agent",
        json={"config": _config("other-agent"), "prompt_text": "新。"},
    )
    assert renamed.status_code == 400
    assert renamed.json()["code"] == "agent_id_immutable"

    deleted = client.delete("/v1/platform/agents/demo-agent")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert client.get("/v1/platform/agents/demo-agent").status_code == 404
    assert client.delete("/v1/platform/agents/demo-agent").status_code == 404


def test_validate_reports_structure_env_and_reference_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry-run 三层校验：结构错误、缺失环境变量名、引用问题。"""
    monkeypatch.delenv("PLATFORM_MISSING_KEY", raising=False)
    monkeypatch.setenv("PLATFORM_PRESENT_KEY", "present")
    client = _client(tmp_path)

    structure = client.post(
        "/v1/platform/agents/validate",
        json={"config": {"agent_id": "x"}, "prompt_text": None},
    )
    assert structure.status_code == 200
    fields = {issue["field"] for issue in structure.json()["errors"]}
    assert "llm_model" in fields

    config = _config(
        "demo-agent",
        llm_model={
            "type": "litellm",
            "model": "openai/x",
            "api_key": "${PLATFORM_MISSING_KEY}",
            "base_url": "https://example.com/${PLATFORM_PRESENT_KEY}",
        },
        enabled_tools=["kb_search", "no_such_*"],
        enabled_skills=["missing-skill"],
        skill_roots=["skills"],
        outbound={"type": "webhook"},
    )
    response = client.post(
        "/v1/platform/agents/validate",
        json={"config": config, "prompt_text": ""},
    )

    assert response.status_code == 200
    payload = response.json()
    error_fields = {issue["field"] for issue in payload["errors"]}
    assert {"enabled_skills", "outbound.type", "prompt_text"} <= error_fields
    warning_fields = {issue["field"] for issue in payload["warnings"]}
    assert {"llm_model.api_key", "enabled_tools", "skill_roots"} <= warning_fields
    messages = [issue["message"] for issue in payload["warnings"]]
    assert any("PLATFORM_MISSING_KEY" in message for message in messages)
    assert not any("PLATFORM_PRESENT_KEY" in message for message in messages)
    assert "present" not in response.text


def test_validate_accepts_valid_draft(tmp_path: Path) -> None:
    """合法配置通过三层校验且无错误与警告。"""
    _write_agent(tmp_path, "demo_dir", "demo-agent")
    (tmp_path / "application" / "demo_dir" / "skills").mkdir()
    _write_skill(tmp_path / "skills", "demo-skill")
    client = _client(tmp_path)
    config = _config(
        "demo-agent",
        enabled_tools=["kb_search", "demo_*"],
        enabled_middlewares=["provider-*"],
        enabled_skills=["demo-skill"],
        skill_roots=["skills"],
        outbound={"type": "log"},
    )

    response = client.post(
        "/v1/platform/agents/validate",
        json={"config": config, "prompt_text": "你是助手。"},
    )

    assert response.status_code == 200
    assert response.json() == {"errors": [], "warnings": []}


def test_validate_scopes_capability_matching_per_agent(tmp_path: Path) -> None:
    """其他 Agent 的私有工具/中间件不算匹配，warning 指出归属。"""
    client = _client(tmp_path)
    config = _config(
        "demo-agent",
        enabled_tools=["get_user_information_by_id", "get_*"],
        enabled_middlewares=["banking-guard"],
    )

    response = client.post(
        "/v1/platform/agents/validate",
        json={"config": config, "prompt_text": "你是助手。"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["errors"] == []
    warnings = payload["warnings"]
    by_field: dict[str, list[str]] = {}
    for issue in warnings:
        by_field.setdefault(issue["field"], []).append(issue["message"])
    tool_messages = by_field["enabled_tools"]
    assert len(tool_messages) == 2
    assert any(
        "banking-agent" in message and "不可跨 Agent 使用" in message
        for message in tool_messages
    )
    # 通配模式只命中其他 Agent 的私有工具时不指出具体归属。
    assert any("未匹配任何可用能力" in message for message in tool_messages)
    middleware_messages = by_field["enabled_middlewares"]
    assert len(middleware_messages) == 1
    assert "banking-agent" in middleware_messages[0]


def test_catalog_returns_capabilities_and_global_skills(tmp_path: Path) -> None:
    """能力目录返回影子发现的工具与磁盘全局 Skill。"""
    _write_skill(tmp_path / "skills", "demo-skill")
    client = _client(tmp_path)

    response = client.get("/v1/platform/catalog")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tools"] == [
        {
            "name": "kb_search",
            "source": "builtin",
            "description": "检索知识库",
            "agent_id": None,
        },
        {
            "name": "get_user_information_by_id",
            "source": "application",
            "description": "按 ID 查用户",
            "agent_id": "banking-agent",
        },
        {
            "name": "demo_echo",
            "source": "application",
            "description": "回显",
            "agent_id": "demo-agent",
        },
    ]
    assert payload["middlewares"] == [
        {"name": "provider-writeback", "source": "builtin", "agent_id": None},
        {
            "name": "banking-guard",
            "source": "application",
            "agent_id": "banking-agent",
        },
    ]
    assert payload["context_providers"] == [
        {"name": "datetime", "source": "builtin", "agent_id": None}
    ]
    assert payload["outbounds"] == [{"type": "log"}, {"type": "stream"}]
    assert payload["global_skills"] == [
        {"name": "demo-skill", "description": "demo-skill 描述"}
    ]


def test_skill_crud_global_scope(tmp_path: Path) -> None:
    """全局 Skill 的创建、读取、更新、删除全流程。"""
    client = _client(tmp_path)

    created = client.post(
        "/v1/platform/skills",
        json={
            "scope": "global",
            "name": "demo-skill",
            "description": "演示",
            "content": "正文。",
            "metadata": {"team": "platform"},
            "allowed_tools": ["kb_search"],
        },
    )
    assert created.status_code == 201
    assert created.json()["skill"]["valid"] is True

    detail = client.get("/v1/platform/skills/global/demo-skill")
    assert detail.status_code == 200
    assert detail.json()["description"] == "演示"
    assert detail.json()["content"] == "正文。"
    assert detail.json()["metadata"] == {"team": "platform"}
    assert detail.json()["allowed_tools"] == ["kb_search"]

    updated = client.put(
        "/v1/platform/skills/global/demo-skill",
        json={"description": "新描述", "content": "新正文。"},
    )
    assert updated.status_code == 200
    assert (
        client.get("/v1/platform/skills/global/demo-skill").json()["description"]
        == "新描述"
    )

    conflict = client.post(
        "/v1/platform/skills",
        json={
            "scope": "global",
            "name": "demo-skill",
            "description": "x",
            "content": "x",
        },
    )
    assert conflict.status_code == 409

    deleted = client.delete("/v1/platform/skills/global/demo-skill")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert client.get("/v1/platform/skills/global/demo-skill").status_code == 404


def test_skill_routes_reject_bad_scope_and_name(tmp_path: Path) -> None:
    """未知 Agent scope 返回 404；非法名称返回 400。"""
    client = _client(tmp_path)

    missing_scope = client.get("/v1/platform/skills", params={"scope": "ghost"})
    assert missing_scope.status_code == 404

    bad_name = client.post(
        "/v1/platform/skills",
        json={
            "scope": "global",
            "name": "Bad_Name",
            "description": "x",
            "content": "x",
        },
    )
    assert bad_name.status_code == 400
    assert bad_name.json()["code"] == "invalid_skill_name"

    missing_skill = client.put(
        "/v1/platform/skills/global/ghost-skill",
        json={"description": "x", "content": "x"},
    )
    assert missing_skill.status_code == 404


def test_list_skills_private_scope_includes_global_view(tmp_path: Path) -> None:
    """私有 scope 返回私有列表与全局库覆盖关系。"""
    _write_agent(tmp_path, "demo_dir", "demo-agent")
    _write_skill(tmp_path / "skills", "shared-skill")
    _write_skill(tmp_path / "application" / "demo_dir" / "skills", "shared-skill")
    client = _client(tmp_path)

    global_view = client.get("/v1/platform/skills", params={"scope": "global"})
    assert global_view.status_code == 200
    assert global_view.json()["global_skills"] is None
    (shared,) = global_view.json()["skills"]
    assert shared["overridden_by"] == ["demo-agent"]

    private_view = client.get("/v1/platform/skills", params={"scope": "demo-agent"})
    assert private_view.status_code == 200
    payload = private_view.json()
    assert payload["skills"][0]["source"] == "private"
    assert payload["skills"][0]["overrides_global"] is True
    assert payload["global_skills"][0]["overridden_by"] == ["demo-agent"]


def test_skill_references_endpoint(tmp_path: Path) -> None:
    """全局视角返回引用指定 Skill 的 agent_id 列表。"""
    _write_agent(tmp_path, "a_dir", "agent-a", enabled_skills=["shared"])
    _write_agent(tmp_path, "b_dir", "agent-b", enabled_skills=["shared"])
    client = _client(tmp_path)

    response = client.get("/v1/platform/skills/shared/references")

    assert response.status_code == 200
    assert response.json() == {"agent_ids": ["agent-a", "agent-b"]}


def test_skill_resource_routes_roundtrip_and_escape(tmp_path: Path) -> None:
    """资源写入、列表、删除与路径逃逸拒绝。"""
    _write_skill(tmp_path / "skills", "demo-skill")
    client = _client(tmp_path)

    written = client.put(
        "/v1/platform/skills/global/demo-skill/resources/references/a.md",
        json={"content": "资源内容"},
    )
    assert written.status_code == 200
    assert written.json() == {"path": "references/a.md"}

    listed = client.get("/v1/platform/skills/global/demo-skill/resources")
    assert listed.status_code == 200
    assert listed.json()["resources"] == [
        {"path": "references/a.md", "size": len("资源内容".encode())}
    ]

    escaped = client.put(
        "/v1/platform/skills/global/demo-skill/resources/..%2F..%2Fescape.md",
        json={"content": "x"},
    )
    assert escaped.status_code == 400
    assert escaped.json()["code"] == "invalid_resource_path"
    assert not (tmp_path / "escape.md").exists()

    bad_suffix = client.put(
        "/v1/platform/skills/global/demo-skill/resources/pic.png",
        json={"content": "x"},
    )
    assert bad_suffix.status_code == 400
    assert bad_suffix.json()["code"] == "unsupported_resource_type"

    deleted = client.delete(
        "/v1/platform/skills/global/demo-skill/resources/references/a.md"
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    missing = client.delete(
        "/v1/platform/skills/global/demo-skill/resources/references/a.md"
    )
    assert missing.status_code == 404


def test_skill_resource_read_returns_path_and_content(tmp_path: Path) -> None:
    """读取单个资源返回路径与内容；缺失 404；路径逃逸 400。"""
    _write_skill(tmp_path / "skills", "demo-skill")
    client = _client(tmp_path)
    client.put(
        "/v1/platform/skills/global/demo-skill/resources/references/a.md",
        json={"content": "资源内容"},
    )

    read = client.get("/v1/platform/skills/global/demo-skill/resources/references/a.md")
    assert read.status_code == 200
    assert read.json() == {"path": "references/a.md", "content": "资源内容"}

    missing = client.get(
        "/v1/platform/skills/global/demo-skill/resources/references/none.md"
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "resource_not_found"

    escaped = client.get(
        "/v1/platform/skills/global/demo-skill/resources/..%2F..%2Fsecret.md"
    )
    assert escaped.status_code == 400
    assert escaped.json()["code"] == "invalid_resource_path"

    listed = client.get("/v1/platform/skills/global/demo-skill/resources")
    assert listed.status_code == 200
    assert listed.json()["resources"] == [
        {"path": "references/a.md", "size": len("资源内容".encode())}
    ]


def test_create_agent_rejects_plaintext_api_key(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """创建拒绝明文 api_key：400、字段路径错误、不落盘、不泄露原值。"""
    client = _client(tmp_path)
    config = _config(
        "leak-agent",
        llm_model={
            "type": "litellm",
            "model": "openai/x",
            "api_key": _PLAINTEXT_API_KEY,
        },
    )

    response = client.post(
        "/v1/platform/agents",
        json={"config": config, "prompt_text": "你是新 Agent。"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "agent_secret_not_placeholder"
    assert "llm_model.api_key" in response.json()["message"]
    assert _PLAINTEXT_API_KEY not in response.text
    assert _PLAINTEXT_API_KEY not in caplog.text
    assert not (tmp_path / "application" / "leak-agent").exists()


def test_update_agent_rejects_plaintext_api_key(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """更新拒绝明文 api_key：400 且磁盘 YAML 保持原样。"""
    _write_agent(tmp_path, "demo_dir", "demo-agent")
    client = _client(tmp_path)
    before = (
        tmp_path / "application" / "demo_dir" / "config" / "agent.yaml"
    ).read_text(encoding="utf-8")
    config = _config(
        "demo-agent",
        llm_model={
            "type": "litellm",
            "model": "openai/x",
            "api_key": _PLAINTEXT_API_KEY,
        },
    )

    response = client.put(
        "/v1/platform/agents/demo-agent",
        json={"config": config, "prompt_text": "新。"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "agent_secret_not_placeholder"
    assert _PLAINTEXT_API_KEY not in response.text
    assert _PLAINTEXT_API_KEY not in caplog.text
    after = (tmp_path / "application" / "demo_dir" / "config" / "agent.yaml").read_text(
        encoding="utf-8"
    )
    assert after == before


@pytest.mark.parametrize(
    "api_key",
    [
        123456789,
        ["sk-array-secret-1", "sk-array-secret-2"],
        {"token": "sk-object-secret"},
    ],
    ids=["number", "array", "object"],
)
def test_create_and_update_reject_non_string_api_key(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    api_key: Any,
) -> None:
    """数字、数组、对象形式的 api_key 在创建与更新时均被拒绝且不泄露。"""
    _write_agent(tmp_path, "demo_dir", "demo-agent")
    client = _client(tmp_path)
    created = _config(
        "typed-agent",
        llm_model={"type": "litellm", "model": "openai/x", "api_key": api_key},
    )
    updated = _config(
        "demo-agent",
        llm_model={"type": "litellm", "model": "openai/x", "api_key": api_key},
    )

    create_response = client.post(
        "/v1/platform/agents",
        json={"config": created, "prompt_text": "你是新 Agent。"},
    )
    update_response = client.put(
        "/v1/platform/agents/demo-agent",
        json={"config": updated, "prompt_text": "新。"},
    )

    assert create_response.status_code == 400
    assert create_response.json()["code"] == "agent_secret_not_placeholder"
    assert update_response.status_code == 400
    assert update_response.json()["code"] == "agent_secret_not_placeholder"
    for needle in ("sk-array-secret", "sk-object-secret", "123456789"):
        assert needle not in create_response.text
        assert needle not in update_response.text
        assert needle not in caplog.text
    assert not (tmp_path / "application" / "typed-agent").exists()
    saved = (tmp_path / "application" / "demo_dir" / "config" / "agent.yaml").read_text(
        encoding="utf-8"
    )
    assert "api_key" not in saved


def test_update_agent_accepts_env_placeholder_api_key(tmp_path: Path) -> None:
    """更新可保存 `${AGENT_LLM_API_KEY}` 且 YAML 保持占位符原文。"""
    _write_agent(tmp_path, "demo_dir", "demo-agent")
    client = _client(tmp_path)
    config = _config(
        "demo-agent",
        llm_model={
            "type": "litellm",
            "model": "openai/x",
            "api_key": "${AGENT_LLM_API_KEY}",
        },
    )

    response = client.put(
        "/v1/platform/agents/demo-agent",
        json={"config": config, "prompt_text": "新。"},
    )

    assert response.status_code == 200
    saved = (tmp_path / "application" / "demo_dir" / "config" / "agent.yaml").read_text(
        encoding="utf-8"
    )
    assert "${AGENT_LLM_API_KEY}" in saved
    detail = client.get("/v1/platform/agents/demo-agent")
    assert detail.status_code == 200
    assert detail.json()["config"]["llm_model"]["api_key"] == "${AGENT_LLM_API_KEY}"


def test_read_agent_masks_historical_plaintext_api_key(tmp_path: Path) -> None:
    """历史 YAML 中的明文 api_key 在 GET 中脱敏，不回显原值。"""
    _write_agent_with_api_key(
        tmp_path, "legacy_dir", "legacy-agent", _PLAINTEXT_API_KEY
    )
    client = _client(tmp_path)

    response = client.get("/v1/platform/agents/legacy-agent")

    assert response.status_code == 200
    assert response.json()["config"]["llm_model"]["api_key"] == "********"
    assert _PLAINTEXT_API_KEY not in response.text


@pytest.mark.parametrize(
    ("api_key_yaml", "needles"),
    [
        ("123456789", ("123456789",)),
        ("[sk-array-secret-1, sk-array-secret-2]", ("sk-array-secret",)),
        ("{token: sk-object-secret}", ("sk-object-secret",)),
    ],
    ids=["number", "array", "object"],
)
def test_read_agent_masks_non_string_api_key(
    tmp_path: Path,
    api_key_yaml: str,
    needles: tuple[str, ...],
) -> None:
    """历史 YAML 中数字、数组、对象形式的 api_key 在 GET 中统一脱敏。"""
    _write_agent_with_api_key(tmp_path, "legacy_dir", "legacy-agent", api_key_yaml)
    client = _client(tmp_path)

    response = client.get("/v1/platform/agents/legacy-agent")

    assert response.status_code == 200
    assert response.json()["config"]["llm_model"]["api_key"] == "********"
    for needle in needles:
        assert needle not in response.text


def test_platform_router_isolation_from_runtime_routes(tmp_path: Path) -> None:
    """平台路由独立工作，不依赖运行时 ApplicationService。"""
    client = _client(tmp_path)

    response = client.get("/v1/platform/agents")

    assert response.status_code == 200
    assert response.json() == {"agents": []}


def test_agent_yaml_written_is_loadable_by_runtime_rules(tmp_path: Path) -> None:
    """平台写入的 agent.yaml 可被 AgentConfig 直接校验（含占位符）。"""
    client = _client(tmp_path)
    config = _config(
        "roundtrip-agent",
        llm_model={"type": "litellm", "model": "openai/x", "api_key": "${SOME_KEY}"},
    )
    client.post(
        "/v1/platform/agents",
        json={"config": config, "prompt_text": "你是助手。"},
    )

    saved = yaml.safe_load(
        (
            tmp_path / "application" / "roundtrip-agent" / "config" / "agent.yaml"
        ).read_text(encoding="utf-8")
    )

    from agentsystem.config.models import AgentConfig

    parsed = AgentConfig.model_validate(saved)
    assert parsed.agent_id == "roundtrip-agent"
    assert parsed.llm_model.api_key is not None
    assert parsed.llm_model.api_key.get_secret_value() == "${SOME_KEY}"
