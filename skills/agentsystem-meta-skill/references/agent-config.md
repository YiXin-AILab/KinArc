# AgentConfig：完整顶层配置

AgentConfig 声明单个 Agent 的身份、资产、模型策略和能力选择。

## 顶层字段

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

## 内置能力配置

内置能力先核对宿主服务、配置和 enabled_* 筛选，避免重复实现已有能力。

### Memory

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

### 知识库

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

### Todo

标准宿主提供 TodoService；Runtime.todo.store.type 可选 redis（默认）、db、local，
db 复用宿主数据库。
Agent 通过 enabled_tools 选择 todo_read、todo_write，通过 enabled_middlewares
选择 todo-reminder。工具与提醒独立筛选；显式名单须保留其他所需能力。
完整替换清单及提醒条件见 [Tool](tools.md) 和 [Middleware](middlewares.md)。

## 完整配置形态示例

以下示例展示全部顶层及子模型字段；能力列表按应用需要填写。
部署前提供非空 prompt/system.md 和三个 EXAMPLE_LLM 环境变量。

```yaml
agent_id: example-agent
agent_name: Example Agent
prompt: prompt/system.md
enabled_skills: []
skill_roots: []
output_mode: complete
outbound:
  type: log
  options: {}
enabled_tools: []
enabled_middlewares: []
enabled_context_providers: []
knowledge_bases: []
plugin_options: {}
llm_model:
  type: litellm
  model: ${EXAMPLE_LLM_MODEL}
  api_key: ${EXAMPLE_LLM_API_KEY}
  base_url: ${EXAMPLE_LLM_BASE_URL}
  timeout_seconds: 60
  context_window: 128000
  temperature: null
  max_output_tokens: 16384
  stop: []
  tool_choice: null
  parallel_tool_calls: null
  provider_options: {}
compaction:
  enabled: true
  safety_margin_tokens: 2048
  trigger_ratio: 0.80
  recent_message_ratio: 0.20
  media_block_tokens: 2048
  tool_argument_max_chars: 512
  tool_result_max_chars: 2048
media:
  llm_transport: auto
  include_asset_id_in_context: false
  archive_rules: []
memory:
  scopes: [user_global, user_agent]
  max_chars:
    user_global: 1000
    user_agent: 2000
metadata: {}
```

## 配置校验与生效

load_yaml_config 校验结构和环境变量；装配校验路径、注册、筛选及插件参数。
provider_options 保留 key 在请求构造时检查，供应商参数需实际请求验证。

## 自定义配置与公开输出

plugin_options 的参数组由插件模型定义；outbound.options 由所选投影器 Factory 定义。

自定义公开输出使用 PublicProjector 协议，并通过 ServiceHost.create 的 outbound_projectors
提供名称到 Factory 的映射。Factory 接收 Mapping[str, JsonValue] 并返回具体 PublicProjector。
实现负责参数校验、鉴权、目标系统协议及有限 I/O；Runtime 按统一投影流程调用它。
协议方法为 `async project(self, ctx: SessionContext, event: AgentEventEnvelope) -> None`。
输入是完整内部事件，实现需显式映射可公开内容。Public 阶段异常或超时停止后续运行，
已完成的 Message → State → Trace 投影不回滚。
多目标输出由所选具体实现负责组合。
