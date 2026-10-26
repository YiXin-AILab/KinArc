"""kb_search 内置 Plugin 的注册、配置校验与工具输出测试。"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import litellm
import pytest
from agentsystem.common.errors import ToolError
from agentsystem.common.models import JsonValue
from agentsystem.knowledge.plugins.knowledge_plugin import register
from agentsystem.knowledge.store import tokenize
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginContribution, PluginOrigin
from agentsystem.plugin.services import PluginSetupServices
from agentsystem.service.agents import ApplicationRoot

PLUGIN_ID = "builtin.knowledge.knowledge-plugin"


def _write_docs(path: Path, items: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _fake_vector(text: str) -> list[float]:
    vector = [0.0] * 512
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:2]) % 512] += 1.0
    return vector


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有 Plugin 测试都不触网；embedding 使用确定性 hash 向量。"""

    async def fake_embed(**kwargs: Any) -> dict[str, object]:
        texts = kwargs["input"]
        assert isinstance(texts, list)
        return {
            "data": [{"embedding": _fake_vector(text)} for text in texts],
        }

    monkeypatch.setattr(litellm, "aembedding", fake_embed)


def _register(
    options: Mapping[str, JsonValue] | None,
    *,
    application_root: Path | None,
) -> PluginContribution:
    services = PluginSetupServices()
    if application_root is not None:
        services.register(ApplicationRoot, ApplicationRoot(application_root))
    services.freeze()
    context = PluginSetupContext(
        plugin_id=PLUGIN_ID,
        agent_id="agent",
        origin=PluginOrigin.BUILTIN,
        options={PLUGIN_ID: dict(options)} if options is not None else {},
        services=services,
    )
    register(context)
    return context._seal_and_extract()


def _options(docs_path: str = "data/kb/pricing.json") -> dict[str, JsonValue]:
    return {
        "bases": [
            {
                "name": "pricing",
                "description": "价格/工时费咨询",
                "docs_path": docs_path,
                "retrieval": {
                    "strategy": "vector",
                    "top_k": 8,
                    "embedding": {"model": "jina_ai/jina-embeddings-v3"},
                },
            }
        ]
    }


def _app_root(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    _write_docs(
        root / "data" / "kb" / "pricing.json",
        [
            {"doc_id": "fee", "title": "工时费标准", "content": "工时费按小时收取"},
            {"doc_id": "account", "title": "开户流程", "content": "开户需要身份证明"},
        ],
    )
    return root


def test_register_without_bases_registers_no_tool(tmp_path: Path) -> None:
    """Agent 未配置知识库时 Plugin 不注册任何 Tool。"""
    contribution = _register(None, application_root=_app_root(tmp_path))
    assert contribution.tools == ()

    contribution = _register({}, application_root=_app_root(tmp_path))
    assert contribution.tools == ()


async def test_register_with_bases_builds_kb_search_tool(tmp_path: Path) -> None:
    """配置 bases 后注册 kb_search，kb 参数枚举与输出 metadata 稳定。"""
    contribution = _register(_options(), application_root=_app_root(tmp_path))

    assert len(contribution.tools) == 1
    definition = contribution.tools[0]
    assert definition.name == "kb_search"
    properties = cast(dict[str, Any], definition.parameters["properties"])
    kb_schema = cast(dict[str, Any], properties["kb"])
    assert kb_schema["enum"] == ["pricing"]
    assert "pricing（价格/工时费咨询）" in definition.description

    output = await definition.handler.invoke(
        ctx=None,
        arguments={"query": "工时费怎么收", "kb": "pricing"},
    )

    assert output.metadata["retrieved_doc_ids"] == ["fee", "account"]
    assert output.metadata["no_relevant_docs"] is False
    assert isinstance(output.metadata["scores"], dict)
    assert output.content.startswith("## [fee] 工时费标准")
    assert "## [account] 开户流程" in output.content


async def test_kb_search_rejects_unknown_kb_and_blank_query(tmp_path: Path) -> None:
    """未知知识库与空 query 都是可预期 ToolError。"""
    contribution = _register(_options(), application_root=_app_root(tmp_path))
    handler = contribution.tools[0].handler

    with pytest.raises(ToolError) as unknown:
        await handler.invoke(ctx=None, arguments={"query": "问题", "kb": "unknown"})
    assert unknown.value.code == "knowledge_base_not_found"

    with pytest.raises(ToolError) as blank:
        await handler.invoke(ctx=None, arguments={"query": "  ", "kb": "pricing"})
    assert blank.value.code == "invalid_kb_search_arguments"


async def test_kb_search_embedding_failure_becomes_tool_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引构建的 embedding 失败让工具调用报 ToolError，不静默降级。"""

    async def failing_embed(**kwargs: Any) -> dict[str, object]:
        raise RuntimeError("provider down")

    # LiteLLMEmbedder 在 register 时绑定 callable，先替换再注册。
    monkeypatch.setattr(litellm, "aembedding", failing_embed)
    contribution = _register(_options(), application_root=_app_root(tmp_path))

    with pytest.raises(ToolError) as captured:
        await contribution.tools[0].handler.invoke(
            ctx=None,
            arguments={"query": "工时费", "kb": "pricing"},
        )

    assert captured.value.code == "knowledge_search_failed"


def test_invalid_options_fail_with_plugin_identity(tmp_path: Path) -> None:
    """配置非法、路径逃逸都在注册期失败并指明 Plugin ID。"""
    invalid = _options()
    bases = cast(list[dict[str, JsonValue]], invalid["bases"])
    bases[0]["unknown_field"] = True
    with pytest.raises(ValueError, match="knowledge-plugin"):
        _register(invalid, application_root=_app_root(tmp_path))

    _write_docs(tmp_path / "outside.json", [])
    with pytest.raises(ValueError, match="knowledge-plugin"):
        _register(
            _options(docs_path="../outside.json"),
            application_root=_app_root(tmp_path),
        )


def test_relative_docs_path_requires_application_root(tmp_path: Path) -> None:
    """缺少 ApplicationRoot capability 时相对 docs_path 注册期失败。"""
    _app_root(tmp_path)
    with pytest.raises(ValueError, match="knowledge-plugin"):
        _register(_options(), application_root=None)
