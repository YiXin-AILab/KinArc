"""平台知识库资源解析：双层目录布局、引用覆盖合并与 Plugin 配置转换。

知识库是平台级独立资源，目录布局对齐 Skill：

```text
knowledge/<kb-name>/kb.yaml            # 全局库清单（name/description/检索配置）
knowledge/<kb-name>/documents.json     # 文档数组
application/<app>/knowledge/<kb-name>/ # 应用级库，同名覆盖全局
```

Agent 在 agent.yaml 的 ``knowledge_bases`` 中按名引用；装配层调用本模块
把引用解析合并为 kb_search Plugin 现有的 plugin_options 结构注入，
Plugin 本身的注册机制不变。
"""

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import SecretStr

from agentsystem.common.models import JsonValue
from agentsystem.config.loader import load_yaml_config
from agentsystem.knowledge.models import (
    KnowledgeBaseConfig,
    KnowledgeBaseManifest,
    KnowledgeBaseRef,
    KnowledgePluginOptions,
    RelevanceGateConfig,
    RelevanceGateDisabled,
)

KNOWLEDGE_PLUGIN_ID = "builtin.knowledge.knowledge-plugin"
"""接收解析结果的 kb_search 内置 Plugin ID。"""

KB_MANIFEST_FILENAME = "kb.yaml"
KB_DOCUMENTS_FILENAME = "documents.json"


def resolve_knowledge_base(
    ref: KnowledgeBaseRef,
    *,
    application_root: Path | None,
    knowledge_root: Path,
    environ: Mapping[str, str] = os.environ,
) -> KnowledgeBaseConfig:
    """按双层布局把单个引用解析为合并后的知识库配置。

    应用级 ``<application_root>/knowledge/<name>`` 优先于全局
    ``<knowledge_root>/<name>``；引用级 retrieval/relevance_gate 覆盖
    kb.yaml 库默认，``relevance_gate: {enabled: false}`` 关闭库默认裁判。

    Args:
        ref: Agent 声明的知识库引用及覆盖配置。
        application_root: 应用根目录；为 None 时只查找全局库。
        knowledge_root: 全局知识库根目录。
        environ: 配置加载时用于环境变量插值的映射。

    Returns:
        已合并引用覆盖项、解析文档路径的知识库配置。

    Raises:
        ValueError: 知识库不存在，或 kb.yaml 的 name 与目录名不一致。
        FileNotFoundError: kb.yaml 或 documents.json 缺失。
    """
    base_dir = _locate_base_dir(
        ref.name,
        application_root=application_root,
        knowledge_root=knowledge_root,
    )
    manifest = load_yaml_config(
        base_dir / KB_MANIFEST_FILENAME,
        KnowledgeBaseManifest,
        environ=environ,
    )
    if manifest.name != ref.name:
        raise ValueError(
            f"知识库 kb.yaml name（{manifest.name}）与目录名（{ref.name}）不一致"
        )
    docs_path = (base_dir / KB_DOCUMENTS_FILENAME).resolve(strict=True)
    return KnowledgeBaseConfig(
        name=manifest.name,
        description=manifest.description,
        docs_path=str(docs_path),
        retrieval=ref.retrieval or manifest.retrieval,
        rerank=manifest.rerank,
        relevance_gate=_merge_relevance_gate(ref, manifest),
    )


def build_knowledge_plugin_options(
    refs: Sequence[KnowledgeBaseRef],
    *,
    application_root: Path | None,
    knowledge_root: Path,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, JsonValue] | None:
    """把 Agent 的全部 knowledge_bases 引用转换为 kb_search Plugin 配置。

    无引用时返回 None。返回 dict 中的密钥按 Plugin 现有契约还原为明文
    字符串，与旧 agent.yaml 内嵌写法经环境变量插值后的形态等价。

    Args:
        refs: Agent 声明的知识库引用序列。
        application_root: 应用根目录；为 None 时只查找全局库。
        knowledge_root: 全局知识库根目录。
        environ: 配置加载时用于环境变量插值的映射。

    Returns:
        知识库插件配置；无引用时返回 None。结果可能包含密钥，不应公开或记录。

    Raises:
        ValueError: 知识库解析失败（不存在、name 不一致等）。
    """
    if not refs:
        return None
    options = KnowledgePluginOptions(
        bases=tuple(
            resolve_knowledge_base(
                ref,
                application_root=application_root,
                knowledge_root=knowledge_root,
                environ=environ,
            )
            for ref in refs
        )
    )
    return {
        key: _reveal_secrets(value)
        for key, value in options.model_dump(mode="python").items()
    }


def _locate_base_dir(
    name: str,
    *,
    application_root: Path | None,
    knowledge_root: Path,
) -> Path:
    """按应用级优先的顺序定位知识库目录，都找不到时报出搜索位置。"""
    candidates: list[Path] = []
    if application_root is not None:
        candidates.append(application_root / "knowledge" / name)
    candidates.append(knowledge_root / name)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise ValueError(f"知识库不存在：{name}（已搜索：{searched}）")


def _merge_relevance_gate(
    ref: KnowledgeBaseRef,
    manifest: KnowledgeBaseManifest,
) -> RelevanceGateConfig | None:
    """按引用级覆盖计算最终 relevance_gate：替换、关闭或跟随库默认。"""
    if isinstance(ref.relevance_gate, RelevanceGateDisabled):
        return None
    if ref.relevance_gate is not None:
        return ref.relevance_gate
    return manifest.relevance_gate


def _reveal_secrets(value: object) -> JsonValue:
    """把模型 dump 中的 SecretStr 还原为明文字符串，其余结构递归原样返回。"""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Mapping):
        return {str(key): _reveal_secrets(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reveal_secrets(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"知识库配置包含非 JSON 值：{type(value).__name__}")


__all__ = [
    "KNOWLEDGE_PLUGIN_ID",
    "build_knowledge_plugin_options",
    "resolve_knowledge_base",
]
