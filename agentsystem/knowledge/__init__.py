"""配置驱动的多知识库检索模块。

知识库是平台级独立资源，目录布局对齐 Skill：

```text
knowledge/<kb-name>/kb.yaml            # 全局库清单：name/description/检索配置
knowledge/<kb-name>/documents.json     # 文档数组
application/<app>/knowledge/<kb-name>/ # 应用级库，同名覆盖全局
```

Agent 在 agent.yaml 的 ``knowledge_bases`` 中按名引用，可整体覆盖
retrieval/relevance_gate：

```yaml
knowledge_bases:
  - name: pricing                 # 跟随库默认
  - name: tech_faq                # 引用级覆盖
    retrieval: {strategy: bm25, top_k: 10}
    relevance_gate: {enabled: false}
```

装配层（`agentsystem/knowledge/registry.py` + ServiceHost）把引用解析
合并为内置 Plugin `builtin.knowledge.knowledge-plugin` 现有的
plugin_options 结构注入，Plugin 注册机制不变；kb_search Tool 的检索
管线为：向量/BM25/混合检索 → 可选重排序 → 可选相关性裁判 → 返回。

kb.yaml 与 agent.yaml 一样支持 ``${NAME}`` 环境变量插值；未配置
knowledge_bases 的 Agent 不会注册任何知识库 Tool。
"""
