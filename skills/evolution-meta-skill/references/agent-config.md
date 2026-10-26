# AgentConfig

## 优化策略

配置用于选择已实现的能力并传递参数。先确认字段由谁消费、改变什么行为，
再修改配置；YAML 合法不等于能力生效。

### 1. 定位生效链路

| 发现问题类型 | 建议操作内容 |
| --- | --- |
| 配置加载失败 | 区分 YAML 语法、模型字段校验和装配错误，修正对应层的字段或引用 |
| 能力已实现但不可用 | 核对注册名称与 enabled_* 选择规则，检查实际选中集合 |
| 参数已填写但行为不变 | 跟踪字段到消费方，核对 plugin_options 的 key、读取逻辑及实例创建 |
| 预算、生成或媒体行为异常 | 转读 [模型](model.md)、[压缩](compaction.md) 或 [媒体](media.md)，按对应机制调整 |
| 能力已选中且参数已消费，结果仍错误 | 转查对应组件实现，不继续试填配置项 |

### 2. 按行为假设调整配置

先记录当前有效值及来源，区分显式值、默认值和插件自行解释的参数。
为修改提出可观察的预期，例如“工具进入可选列表”，而非笼统地“提高得分”。

能力名单按集合差异合并，保留仍需使用的依赖；核对 null、空列表与通配符的不同语义。
参数必须有真实消费方与已支持契约，不能通过添加任意 key 创造能力。
多个字段共同保证一个条件时一起核算，如输入预算；不将无关参数混在同轮试验中。

### 3. 验证实际生效

依次验证配置解析、Agent 装配、目标行为；需要重新装配的修改在新实例中验证。
在现有 Changelog 中记录字段前后值、消费方与行为证据，敏感值仅记录环境变量名。
若字段已生效但目标问题未改善，修订原因假设，不继续扩大配置范围。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 配置不断增加，但无法指出消费代码 | 核对读取路径，删除确认未消费的新增项；每个保留项都对应实际能力或参数 |
| 复制示例覆盖已有部署配置 | 按字段合并，保留任务无关的身份、连接和能力选择；示例默认值不作为优化目标 |
| 新增一项却使其他能力失效 | 对比实际选中集合，恢复误删依赖；检查精确名称、通配符和注册条件 |
| 多轮同时调整多个无关参数 | 按行为假设拆分实验，记录必要联动关系；不能只凭总分选择参数组合 |
| 为规避校验而关闭能力或放宽字段 | 修复契约不一致，确认原任务仍可完成；启动成功不能替代行为验证 |

## 系统修改说明

AgentConfig 声明单个 Agent 的身份、资产、模型策略和能力选择。

### 顶层字段

未声明的顶层字段会被拒绝。名称列表不允许重复；`null` 与 `[]` 的含义分别定义如下。

| 字段 | 类型 / 默认值 | 含义及约束 |
| --- | --- | --- |
| `agent_id` | 非空字符串，必填 | 稳定路由身份 |
| `agent_name` | 非空字符串，必填 | 可读名称 |
| `prompt` | 非空字符串，必填 | Prompt 文件路径，不是内联正文 |
| `enabled_skills` | 字符串列表，`[]` | 按顺序激活 Skill，必须能够找到并校验 |
| `skill_roots` | 字符串列表，`[]` | 显式搜索根；同名 Skill 首个命中者获胜 |
| `output_mode` | `complete` / `stream`，`complete` | 公开输出消费模式，不是底层模型 API 的流式开关 |
| `outbound` | 对象，必填 | 见下表；每个 Agent 选择一个公开投影器 |
| `enabled_tools` | 字符串列表 / `null`，`null` | `null` 或省略启用全部候选工具，`[]` 全部关闭 |
| `enabled_middlewares` | 字符串列表 / `null`，`null` | 与工具筛选相同，筛选的是注册短名称 `name` |
| `enabled_context_providers` | 字符串列表，`[]` | 精确 type，按配置顺序创建实例，不支持通配符 |
| `knowledge_bases` | 引用列表，`[]` | 按 name 引用知识库，可整体覆盖 retrieval、relevance_gate；名称不能重复 |
| `plugin_options` | key → JSON 对象，`{}` | 插件读取并校验对应 key 的参数对象 |
| `llm_model` | 对象，必填 | 模型连接、生成参数和本地预算 |
| `compaction` | 对象，省略采用字段默认值 | 上下文预算、摘要和工具内容剪枝 |
| `media` | 对象，省略采用字段默认值 | 媒体归档、模型传输和句柄提示 |
| `memory` | 对象，省略采用字段默认值 | 当前用户记忆的作用域和写入额度；需宿主启用 Memory |
| `metadata` | JSON 对象，`{}` | 应用元数据；填写任意 key 不会自动改变 Prompt 或执行策略 |

| outbound 字段 | 默认值 | 规则 |
| --- | --- | --- |
| `type` | 必填 | 内置 `log`、`stream`，或宿主代码已注册的 Factory 名 |
| `options` | `{}` | 由具体 Factory 校验；内置两种只接受空对象 |

PublicProjector 由宿主注册，YAML 通过 outbound.type 选择已注册的实现。
stream 还受 Runtime.public_stream 约束。

工具和中间件模式只支持 `*`，大小写敏感、匹配完整名称，不支持 `?`、`[]`、反斜线。
每个模式必须至少匹配一个已注册候选；最终顺序不是 YAML 模式顺序。
筛选发生在插件注册之后，因此禁用能力不等于跳过插件 import、注册及其参数校验。
使用 `null` 或 `["*"]` 时，新注册能力可能自动进入 Agent；固定能力集合宜显式列出名称。

enabled_skills、skill_roots 和 enabled_context_providers 使用列表类型，缺省均为 []。

### 内置能力配置

内置能力先核对宿主服务、配置和 enabled_* 筛选，避免重复实现已有能力。

#### Memory

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| memory.scopes | [user_global, user_agent] | 两种当前用户记忆；前者跨 Agent 共享，后者仅当前 Agent；[] 关闭，禁止重复 |
| memory.max_chars.user_global | 1000 | 写入字符额度，严格整数 1..1000000 |
| memory.max_chars.user_agent | 2000 | 写入字符额度，严格整数 1..1000000 |

Runtime.memory.enabled 默认 false；宿主启用后复用唯一 MemoryStore，
Runtime.memory.store.type 可选 redis（默认）、db、local；db 复用宿主数据库。
Agent 的 scopes 非空且宿主提供可用 MemoryService 时才注册 memory_read/add/update，
名称仍须通过 enabled_tools 筛选。作用域与额度由 AgentConfig.memory 配置。
首次 SYSTEM 按启用范围原样读取完整记忆快照，该快照在 Session 内保持不变。
字符额度在写入时校验；读取与 SYSTEM 渲染保留完整正文。

Agent 局部配置示例，合并时保留其他所需工具：

```yaml
memory:
  scopes: [user_global, user_agent]
  max_chars:
    user_global: 1000
    user_agent: 2000
enabled_tools: [memory_read, memory_add, memory_update]
```

宿主在 Runtime 配置中启用 memory.enabled，Agent 配置决定作用域、额度与工具选择。

#### 知识库

knowledge_bases 默认为 []，元素是 KnowledgeBaseRef；name 必填，名称不得重复。
retrieval 缺省或 null 跟随库配置，提供时整体替换 RetrievalConfig；
relevance_gate 缺省或 null 跟随库配置，提供完整裁判配置时整体替换，
也可用 {enabled: false} 关闭库默认裁判。每层未知字段均拒绝。
retrieval.strategy 可选 vector（默认）、bm25、hybrid；vector/hybrid 必须配置 embedding。

标准宿主从 `application/<app>/knowledge/<name>/` 或全局 `knowledge/<name>/`
读取 kb.yaml 和 documents.json，应用级同名库优先；缺失引用在装配期失败。
宿主解析后注入内置知识库插件的 bases 参数；该插件注册一个 kb_search 工具，
仍须通过 enabled_tools 筛选。库资源由宿主提供，Agent 通过 kb_search 按需检索。

以下片段要求宿主已提供名为 faq 的库；保留其他所需工具和知识库引用：

```yaml
knowledge_bases:
  - name: faq
    relevance_gate:
      enabled: false
enabled_tools: [kb_search]
```

#### Todo

标准宿主提供 TodoService；Runtime.todo.store.type 可选 redis（默认）、db、local，
db 复用宿主数据库。
Agent 通过 enabled_tools 选择 todo_read、todo_write，通过 enabled_middlewares
选择 todo-reminder。工具与提醒独立筛选；显式名单须保留其他所需能力。
完整替换清单及提醒条件见 [Tool](tools.md) 和 [Middleware](middlewares.md)。

### 修改后的检查

沿当前配置核对字段消费方、文件路径和能力选择，避免把文档中的默认值覆盖到现有配置。
load_yaml_config 校验结构和环境变量；装配校验路径、注册、筛选及插件参数。
provider_options 的保留字段和供应商参数还需请求构造及实际请求验证。

outbound 仅在本轮明确涉及公开输出问题且允许修改时调整。
可选择宿主已注册的投影器，并遵循其 options 契约；应用插件不能自行注册投影器。
新增输出协议和宿主投影器实现不属于本 Skill 的优化范围。
