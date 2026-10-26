# Tool

## 优化策略

围绕目标 Agent 需要完成的操作，改进工具的可发现性、调用契约、执行行为和结果表达。
先确认失败发生在哪一层，再选择修改方式；描述不能修复执行错误，新增工具也不等于能力已生效。

### 1. 定位失效环节

沿 trace 核对可选工具、实际参数、执行结果和后续行动，不只看最终答案。

| 发现问题类型 | 建议操作内容 |
| --- | --- |
| 所需工具未出现在可选列表 | 检查 [Plugin](plugins.md) 注册与 enabled_tools 筛选 |
| 工具可用但未调用，或调用了用途相近的错误工具 | 澄清名称、描述中的用途、触发条件和副作用 |
| 参数缺失、含义误解或字段组合错误 | 修订参数 schema、字段说明与执行校验 |
| 参数正确但计算、查询或状态变更错误 | 修正 handler 实现与业务条件校验 |
| 结果正确但后续行动错误 | 检查模型实际可见的结果是否足以区分状态；表达清楚后仍误用，再修订 [Prompt](prompt.md) 或 [Skill](skills.md) |
| 重复、并发调用或挂起恢复导致错误 | 检查副作用、顺序依赖、重复执行语义和续执行实现 |

### 2. 设计能力与调用契约

明确操作对象、必要输入、产生的副作用、结果含义与失败边界。
核对已有工具能否表达该操作，决定修订、新增或组合；不要按评测题目扩充接口。

| 设计问题 | 优化方法 | 如何修改 |
| --- | --- | --- |
| 相近工具难以区分 | 明确选择依据 | description 说明做什么、何时使用及关键副作用；仅在容易混淆时说明边界，不堆叠同义词或复制完整业务流程 |
| 参数依赖猜测 | 明确输入语义 | 标明标识来源、单位、格式、枚举含义及字段关系；区分缺省与显式值，不把题目中的值设为通用默认值 |
| 模型反复填写可由程序确定的字段 | 减少无效选择 | 将可信上下文或确定性计算可得的值交由实现获取；保留真正需要用户或模型决定的输入 |
| 单个工具混合不相关操作 | 拆分操作职责 | 按不同输入、效果和失败语义划分接口，避免用大量模式参数隐藏不同契约 |
| 固定调用组合反复遗漏或容易错序 | 封装稳定操作 | 仅在组合有明确业务含义时封装；保留必要的中间判断，并明确部分成功、失败和重复调用的处理方式 |

通用多步骤策略放入 Skill；单个工具的短调用约束放入 description、schema 或 rules。
同一约束避免多处复制。必须成立的业务条件由执行代码校验，不能依赖模型遵守文字说明。

### 3. 设计执行与结果

执行前校验参数及必要的当前业务状态；静态 schema 无法保证的字段关系和业务条件，
在 handler 中检查。校验失败不应先产生副作用；外部 I/O 设置有限 timeout。

结果围绕下一次决策组织：明确当前状态、已完成的效果、仍未完成的事项及必要的业务标识。
区分“已受理、已完成、业务拒绝、未命中、执行失败”；只保留该工具实际存在的状态，
不把工具调用成功等同于业务完成。需要模型判断的信息必须出现在模型可见的返回内容中。

例如，异步提交成功后返回“已受理，尚未完成”及可用的查询标识，
不能只返回“success”让模型推断完成。具体字段与查询方式须符合真实业务协议。

对有副作用的工具，明确重复请求会复用结果、拒绝还是再次执行；
超时后结果不明时，按业务能力查询或核对状态，不默认失败后即可重试。
并发安全声明与真实共享状态一致，挂起声明须有恢复实现。

### 4. 验证行为与反馈

按改动风险验证代表性调用、非法输入和已有正确场景；涉及副作用时，
覆盖相关的重复调用、部分失败或并发情形。schema 检查不能代替执行测试。

再通过目标 Agent 的 trace 核对是否选对工具、传对参数，并依据结果采取正确行动。
在现有 Changelog 中记录失效环节、契约变化、预期行为与验证结果；
工具自身测试通过不等于 Agent 使用效果已验证。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 工具、参数或规则随 case 增长，用途越来越重叠 | 按真实业务操作归并，删除无依据的特例与重复约束；代表任务仍能明确选工具，不靠试调用猜用途 |
| description 或 rules 堆积完整流程 | 保留选择依据和局部调用约束，将专项流程移入 Skill；核对读取时机，不以搬走文字代替解决歧义 |
| description、schema、handler 与返回结果互相矛盾 | 对照实际调用统一必填项、默认值、取值范围和状态语义；通过合法与非法调用验证，不能只修改其中一处 |
| 收紧参数或改名后破坏合法调用 | 核对 Prompt、Skill、注册项、启用列表及其他调用方，同步相关引用，验证原有合法场景 |
| 返回内容越来越长，关键状态被日志或无关字段淹没 | 保留下一步决策必需的信息；大量结果按实际能力分页或检索，不能删去完成依据或失败信息 |
| 用文字要求弥补执行缺陷，或新增替代工具伪造成功 | 修复实际实现；契约受保护或本轮不可修改时记录限制，不宣称问题已解决 |

## 系统修改说明

Tool 以名称、描述和参数 schema 向模型暴露应用功能。

`context.register_tool(handler, ...)` 可以覆盖以下注册属性：

| 参数 | 默认值 / 要点 |
| --- | --- |
| `handler` | 必填；接收已校验业务参数并返回 ToolOutput 的处理函数 |
| `name` | 省略时由 handler 推导；对外调用身份，修改需同步引用和筛选项 |
| `description` | 省略时从函数说明推导；描述用途和触发条件 |
| `parameters` | 省略时从类型注解构造 schema；显式 schema 必须与 handler 接口一致 |
| `rules` | 可选；进入 SYSTEM 工具规则，适合短的调用约束 |
| `is_concurrency_safe` | 默认 true；有顺序依赖或共享可变状态时必须正确声明 |
| `can_suspend` | 默认 false；设为 true 只是声明，还需要真实 suspension/resume 实现 |

处理函数可同步或异步；描述、schema 和行为必须一致，并遵循统一异常、日志及授权边界。

### Handler 与模型 schema

默认 description 取 docstring 第一行；无说明且未显式传 description 时注册失败。
所有可见参数必须有类型注解，不支持 *args、**kwargs、位置专用参数。
默认值决定参数是否必填，枚举/类型描述应与真实执行一致。
parameters 必须是 object schema，properties 名称须与可见参数一致；
类型、枚举和 required 的一致性由作者验证。运行时按签名构造 invocation_model，拒绝未知参数。

普通 handler 必须返回 ToolOutput(content: str, metadata: JSON mapping = {})。
正常返回 "Error: ..." 仍是成功 ToolOutput。执行失败通过统一 ToolError 表达；
如业务协议将拒绝或未命中定义为正常查询结果，则可通过 ToolOutput 表达该业务状态。

同步 handler 由普通函数 adapter 直接调用，不能依赖外层 timeout 中断阻塞 I/O；
有阻塞操作时改用异步接口或显式线程隔离，并为 I/O 自身设置有限 timeout。

### 运行上下文、并发与授权

普通工具通过 get_tool_context() 读取 ToolInvocationContext：
session_context、run_id、runtime_trace_id、tool_call_id、tool_name、turn_index。
这些关联字段由 Runtime 提供，不属于模型可见参数；上下文仅在工具执行期间可用。

llm_model.parallel_tool_calls 同时影响模型调用偏好和 Runtime 执行分组；只有 true 才启用
并发分组，false/null 时串行。并发组仍受 is_concurrency_safe 与挂起工具约束。
串行只能保证执行顺序；后续参数依赖前次结果时，应先取得结果再发起下一次调用。
工具执行期限由 RuntimeConfig.tool_timeout_seconds 统一配置，默认 30 秒、正数。
默认 ToolAuthorization 放行全部调用；授权/continuation 的策略实现由宿主注入。
can_suspend=true 声明工具支持挂起；授权、resume token 和续执行仍由对应策略与实现提供。

### 可用于优化的内置工具

| 工具名 | 注册前提 | 用途 |
| --- | --- | --- |
| skill_view | 内置注册，通过 enabled_tools 筛选 | 按 SkillRoots/name/file_path 拼接读取；不依赖 SkillIndex，支持相对或绝对路径及分页 TTL 缓存 |
| tool_content_view | 宿主提供 MessageManager | 读取当前 Session 被剪枝的工具参数/结果 |
| memory_read/add/update | 宿主提供已启用的 MemoryService | 读取、追加或精确替换当前用户启用范围的记忆 |
| kb_search | 宿主解析知识库引用或提供合法 bases 参数 | 按 query、kb 查询已配置的库 |
| todo_read/write | 宿主提供 TodoService | 读取或完整替换当前会话的任务清单 |

上述工具仍受 enabled_tools 筛选；目标宿主没有提供依赖时，不能仅靠配置启用。

Memory 与知识库的配置和宿主条件见 [配置](agent-config.md#内置能力配置)。
memory_read(scope) 读取完整正文；memory_add(scope, new_content) 追加原文，已有正文时加一个换行。
memory_update(scope, old_content, new_content, replace_all=false) 精确替换原文；
默认要求唯一匹配，replace_all=true 替换全部非重叠匹配，new_content="" 可删除匹配内容。
scope 为 user_global 或 user_agent，须在当前 Agent 启用；写入仍受字符额度约束。

kb_search(query, kb) 要求非空查询与已注册库名；返回检索内容与相关性信息，
未知库和检索失败通过 ToolError 表达。授权与动态业务判断仍由实际执行边界负责。

todo_read() 无参数；todo_write(todos) 每次提交完整有序清单，未提交条目被移除，[] 清空。
最多 50 条，每条仅含 content（含非空白字符的 UTF-8 文本，1..500 字符）和
status（pending / in_progress / completed）。
清单按当前 app/user/agent/session 隔离，两种工具都声明 is_concurrency_safe=false。
todo-reminder 的启用与触发条件见 [Middleware](middlewares.md)。

tool_content_view 完整参数：

| 参数 | 默认值 | 范围 |
| --- | --- | --- |
| tool_call_id | 必填 | 非空字符串，目标工具调用身份 |
| part | result | arguments / result |
| offset | 0 | 非负整数，字符偏移 |
| limit | 8000 | 1..8000 字符 |

只有当前上下文明示同一目标已剪枝，或该工具上一页 truncated=true 时才使用。
该接口不用于正常完整内容读取、失败调用重试或业务工具替代。
返回 total_chars、truncated、next_offset 等分页信息；身份范围来自当前 Session，不是任意历史查询。

### 修改后的检查

验证工具名、描述与实际用途一致，schema 和 handler 对必填、默认值及非法参数的处理一致。
检查标准返回、ToolError、有限 timeout、并发依赖，以及启用列表和 Prompt/Skill 引用。
修改 schema 后需要覆盖实际 handler 行为，不能仅比较 JSON。
新工具使用目标应用现有插件风格，通过同步 register 调用 context.register_tool，
再合并 enabled_tools；不复制无关演示工具。
