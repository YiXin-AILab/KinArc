"""平台知识库 registry 的双层解析、覆盖合并与 Plugin 配置转换测试。"""

import json
from pathlib import Path
from typing import cast

import pytest
import yaml
from agentsystem.common.models import JsonValue
from agentsystem.knowledge.models import (
    KnowledgeBaseConfig,
    KnowledgeBaseRef,
    KnowledgePluginOptions,
    RelevanceGateConfig,
    RelevanceLevel,
    RetrievalStrategy,
)
from agentsystem.knowledge.registry import (
    KNOWLEDGE_PLUGIN_ID,
    build_knowledge_plugin_options,
    resolve_knowledge_base,
)
from agentsystem.plugin.assembler import assemble_agent_capabilities
from agentsystem.plugin.discovery import discover_builtin_plugins
from agentsystem.skill.index import SkillIndex

_DOCS = [
    {"doc_id": "fee", "title": "工时费标准", "content": "工时费按小时收取"},
    {"doc_id": "account", "title": "开户流程", "content": "开户需要身份证明"},
    {"doc_id": "workshop", "title": "营业时间", "content": "门店每日九点营业"},
]


def _write_kb(
    root: Path,
    name: str,
    *,
    manifest: dict[str, object] | None = None,
    docs: list[dict[str, str]] | None = None,
) -> Path:
    """在指定根目录写入一个完整知识库（kb.yaml + documents.json）。"""
    base = root / name
    base.mkdir(parents=True)
    manifest_data: dict[str, object] = {
        "name": name,
        "description": f"{name} 库",
        "retrieval": {"strategy": "bm25", "top_k": 5},
        **(manifest or {}),
    }
    base.joinpath("kb.yaml").write_text(
        yaml.safe_dump(manifest_data, allow_unicode=True),
        encoding="utf-8",
    )
    if docs is not None:
        base.joinpath("documents.json").write_text(
            json.dumps(docs, ensure_ascii=False),
            encoding="utf-8",
        )
    return base


def _resolve(
    ref: KnowledgeBaseRef,
    tmp_path: Path,
    *,
    with_app_root: bool = False,
    environ: dict[str, str] | None = None,
) -> KnowledgeBaseConfig:
    return resolve_knowledge_base(
        ref,
        application_root=(tmp_path / "app") if with_app_root else None,
        knowledge_root=tmp_path / "knowledge",
        environ=environ or {},
    )


def test_resolve_global_base_follows_manifest_defaults(tmp_path: Path) -> None:
    """无引用级覆盖时解析结果完全跟随全局 kb.yaml 库默认。"""
    base = _write_kb(
        tmp_path / "knowledge",
        "pricing",
        manifest={
            "relevance_gate": {"model": "openai/small", "min_relevance": "relevant"}
        },
        docs=_DOCS,
    )

    config = _resolve(KnowledgeBaseRef(name="pricing"), tmp_path)

    assert config.name == "pricing"
    assert config.description == "pricing 库"
    assert config.docs_path == str(base / "documents.json")
    assert config.retrieval.strategy is RetrievalStrategy.BM25
    assert config.retrieval.top_k == 5
    gate = config.relevance_gate
    assert gate is not None and gate.min_relevance is RelevanceLevel.RELEVANT


def test_application_level_base_overrides_global(tmp_path: Path) -> None:
    """应用级同名知识库整体覆盖全局库。"""
    _write_kb(tmp_path / "knowledge", "pricing", docs=_DOCS)
    app_base = _write_kb(
        tmp_path / "app" / "knowledge",
        "pricing",
        manifest={"description": "应用级价格库", "retrieval": {"strategy": "bm25"}},
        docs=_DOCS,
    )

    config = _resolve(KnowledgeBaseRef(name="pricing"), tmp_path, with_app_root=True)

    assert config.description == "应用级价格库"
    assert config.docs_path == str(app_base / "documents.json")


def test_ref_retrieval_override_replaces_manifest(tmp_path: Path) -> None:
    """引用级 retrieval 整体替换库默认，不做字段级合并。"""
    _write_kb(tmp_path / "knowledge", "pricing", docs=_DOCS)
    ref = KnowledgeBaseRef.model_validate(
        {"name": "pricing", "retrieval": {"strategy": "bm25", "top_k": 10}}
    )

    config = _resolve(ref, tmp_path)

    assert config.retrieval.top_k == 10


def test_ref_gate_override_replaces_or_disables_manifest(tmp_path: Path) -> None:
    """引用级 relevance_gate 可整体替换，也可用 enabled=false 关闭库默认。"""
    _write_kb(
        tmp_path / "knowledge",
        "pricing",
        manifest={"relevance_gate": {"model": "openai/small"}},
        docs=_DOCS,
    )

    replaced = _resolve(
        KnowledgeBaseRef.model_validate(
            {"name": "pricing", "relevance_gate": {"model": "openai/other"}}
        ),
        tmp_path,
    )
    gate = replaced.relevance_gate
    assert isinstance(gate, RelevanceGateConfig)
    assert gate.model == "openai/other"

    disabled = _resolve(
        KnowledgeBaseRef.model_validate(
            {"name": "pricing", "relevance_gate": {"enabled": False}}
        ),
        tmp_path,
    )
    assert disabled.relevance_gate is None


def test_missing_base_reports_searched_locations(tmp_path: Path) -> None:
    """引用不存在的知识库时报错并列出已搜索位置。"""
    (tmp_path / "knowledge").mkdir()
    with pytest.raises(ValueError, match="知识库不存在：ghost"):
        _resolve(KnowledgeBaseRef(name="ghost"), tmp_path, with_app_root=True)


def test_manifest_name_must_match_directory(tmp_path: Path) -> None:
    """kb.yaml 的 name 与目录名不一致时启动期失败。"""
    _write_kb(tmp_path / "knowledge", "pricing", manifest={"name": "other"}, docs=_DOCS)

    with pytest.raises(ValueError, match="不一致"):
        _resolve(KnowledgeBaseRef(name="pricing"), tmp_path)


def test_missing_documents_file_fails(tmp_path: Path) -> None:
    """知识库目录缺少 documents.json 时解析期失败。"""
    _write_kb(tmp_path / "knowledge", "pricing")

    with pytest.raises(FileNotFoundError):
        _resolve(KnowledgeBaseRef(name="pricing"), tmp_path)


def test_manifest_supports_environment_interpolation(tmp_path: Path) -> None:
    """kb.yaml 与 agent.yaml 一样支持 ${ENV} 环境变量插值。"""
    base = tmp_path / "knowledge" / "pricing"
    base.mkdir(parents=True)
    base.joinpath("kb.yaml").write_text(
        "name: pricing\n"
        "retrieval: {strategy: bm25}\n"
        "relevance_gate:\n"
        "  model: ${KB_GATE_MODEL}\n",
        encoding="utf-8",
    )
    base.joinpath("documents.json").write_text("[]", encoding="utf-8")

    config = _resolve(
        KnowledgeBaseRef(name="pricing"),
        tmp_path,
        environ={"KB_GATE_MODEL": "openai/gate"},
    )

    gate = config.relevance_gate
    assert gate is not None and gate.model == "openai/gate"


def test_build_plugin_options_returns_none_without_refs(tmp_path: Path) -> None:
    """Agent 未引用任何知识库时不向 Plugin 注入配置。"""
    assert (
        build_knowledge_plugin_options(
            (),
            application_root=None,
            knowledge_root=tmp_path / "knowledge",
        )
        is None
    )


def test_build_plugin_options_revalidates_with_plain_secrets(tmp_path: Path) -> None:
    """转换结果可被 Plugin 原样校验，密钥还原为插值后的明文字符串。"""
    _write_kb(
        tmp_path / "knowledge",
        "pricing",
        manifest={"relevance_gate": {"model": "openai/small", "api_key": "${KB_KEY}"}},
        docs=_DOCS,
    )

    options = build_knowledge_plugin_options(
        (KnowledgeBaseRef(name="pricing"),),
        application_root=None,
        knowledge_root=tmp_path / "knowledge",
        environ={"KB_KEY": "plain-secret"},
    )

    assert options is not None
    revalidated = KnowledgePluginOptions.model_validate(options)
    gate = revalidated.bases[0].relevance_gate
    assert gate is not None
    assert gate.api_key is not None
    assert gate.api_key.get_secret_value() == "plain-secret"


async def test_agent_yaml_reference_assembles_kb_search_tool(tmp_path: Path) -> None:
    """端到端：knowledge_bases 引用合并后注入装配，kb_search 可直接检索。"""
    _write_kb(tmp_path / "knowledge", "pricing", docs=_DOCS)
    _write_kb(tmp_path / "knowledge", "tech_faq", docs=_DOCS)
    options = build_knowledge_plugin_options(
        (
            KnowledgeBaseRef(name="pricing"),
            KnowledgeBaseRef.model_validate(
                {"name": "tech_faq", "retrieval": {"strategy": "bm25", "top_k": 1}}
            ),
        ),
        application_root=None,
        knowledge_root=tmp_path / "knowledge",
    )
    assert options is not None
    sources = tuple(
        source
        for source in discover_builtin_plugins(
            Path(__file__).parents[3] / "agentsystem"
        )
        if source.plugin_id == KNOWLEDGE_PLUGIN_ID
    )
    assert len(sources) == 1

    capabilities = assemble_agent_capabilities(
        agent_id="agent",
        sources=sources,
        plugin_options={KNOWLEDGE_PLUGIN_ID: options},
        context_provider_types=(),
        skill_index=SkillIndex.load(roots=(), enabled_skills=()),
        timeout_seconds=1.0,
        max_content_chars=1_000,
    )

    tools = capabilities.tool_registry
    definition = tools.get("kb_search")
    assert definition is not None
    properties = cast(dict[str, JsonValue], definition.parameters["properties"])
    kb_schema = cast(dict[str, JsonValue], properties["kb"])
    assert kb_schema["enum"] == ["pricing", "tech_faq"]
    output = await definition.handler.invoke(
        ctx=None,
        arguments={"query": "工时费", "kb": "tech_faq"},
    )
    # 引用级 top_k=1 覆盖生效：只返回一条结果。
    assert output.metadata["retrieved_doc_ids"] == ["fee"]
