# Middleware

## 优化策略

在明确的生命周期节点执行共同检查、提醒或控制。
先说明“在何时、依据什么证据、改变哪次执行”，再选择挂点与决策；
单个工具的业务校验放在 [Tool](tools.md)，普通任务流程放在 [Skill](skills.md)。

### 1. 定位控制需求

核对失败前后的事件、现有处理器和启用项，确认需要的信息在所选挂点已经可用，
且该挂点仍来得及阻止或修正目标行为。事后改写结果不能撤销已发生的副作用。

| 设计问题 | 优化方法 | 如何修改 |
| --- | --- | --- |
| 规则存在但在关键节点容易遗漏 | 条件提醒 | 用当前运行证据触发简短、可执行的提醒，说明需要改变的下一步；提醒不等于强制上限 |
| 可程序判断的禁止条件未被执行 | 前置校验 | 在操作前且支持 block 的挂点检查，给出明确原因；无证据时不把模型猜测变成拦截条件 |
| 数据需要统一修正 | 合约内修改 | 使用该挂点允许的 modify 与匹配 payload，保留消息和调用身份，不改变真实业务结果 |
| 可恢复的模型调用错误 | 有限重试 | 核对挂点及 operation 是否允许 retry，说明重试何以可能有效，并遵守宿主预算 |
| 控制器未触发或效果被覆盖 | 核对处理链 | 检查名称筛选、作用域、优先级、输入变化与终结决策，定位实际执行到的处理器 |

### 2. 设计触发与退出

触发条件应能从当前 payload、只读状态或可信结果判断，
明确正常路径、命中路径和信息不足时的处理，避免把某道题的表象固化成通用规则。

检查同挂点已有处理器：modify 会影响后续输入，终结决策可能跳过后续处理。
依据失败后继续执行的后果选择允许的 failure_mode，不能把所有异常统一吞掉或拦截。
提醒与重试须有明确的适用范围和停止依据；不得重放结果不明的有副作用操作。

### 3. 验证完整执行

验证命中场景与相近合法场景，再检查处理链顺序、超时、异常和任务终态。
记录“挂点已执行、条件已命中、返回决策、后续行为”四层证据；
在现有 Changelog 中区分控制生效与任务改善，不以 block 或 retry 次数证明成功。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 同一问题不断叠加提醒和拦截器 | 归并触发条件与责任，删除重复处理；检查注入消息量和完整处理链 |
| 失败减少但合法任务也被阻止 | 收窄触发条件，补查相邻合法场景；不能靠提前终止提高表面正确率 |
| 重试反复执行相同失败路径 | 检查可恢复条件与预算，撤回无效重试；保留原错误而非伪造成功 |
| 修改结果后与真实操作不一致 | 恢复实际业务状态及调用身份；修改展示不等于完成或回滚操作 |
| 新处理器使回写等原能力失效 | 核对启用集合、排序与提前返回，恢复仍需执行的处理器 |
| 需要改写运行态或已完成投影才能生效 | 返回对应组件重新设计，不绕过 StateManager 或 Runtime 保存与收敛协议 |

## 系统修改说明

Middleware 在生命周期挂点处理数据并返回控制决策。

### 全部注册参数

通过同步 Plugin register 调用 context.register_middleware：

| 参数 | 默认值 | 合约 |
| --- | --- | --- |
| handler | 必填 | async callable，接收 MiddlewareContext，返回 MiddlewareDecision |
| name | 必填 | 唯一短名称，非空、无首尾空白；enabled_middlewares 按它筛选 |
| handler_id | 必填 | 当前插件内唯一，非空；最终 middleware_id 为 plugin_id.handler_id |
| point | 必填 | MiddlewarePoint，合法挂点见下表 |
| scope | global | global / app |
| priority | 0 | 同挂点数值越大越先执行；同值按 middleware_id 排序 |
| app_id | null | global 时必须为 null，app 时必填 |
| timeout_ms | 1000 | 正数，逐个 handler 限时 |
| failure_mode | fail_open | fail_open / fail_closed；后者只允许部分挂点 |

enabled_middlewares 省略或 null 选择全部，[] 全关；仅支持大小写敏感的 * 全字符串匹配，
每项至少匹配一个候选。筛选不会取消插件 import/register 和注册期参数校验。
被选中的 name / middleware_id 不能重复。

### 全部挂点、输入与可用决策

| point | 输入 payload | 允许的 decision.type | 允许 fail_closed |
| --- | --- | --- | --- |
| before_system_prompt_build | SystemPromptBuildPayload | continue、update | 是 |
| after_system_prompt_build | SystemPromptBuildPayload | continue、update | 是 |
| before_run | RunMiddlewarePayload | continue、modify、block | 是 |
| before_turn | TurnMiddlewarePayload | continue、messages、block | 否 |
| after_turn | TurnMiddlewarePayload | continue | 否 |
| before_llm | LLMRequest | continue、replace、block | 是 |
| after_llm | LLMResponse | continue、modify、retry、block | 是 |
| before_tool_call | ToolCallMiddlewarePayload | continue、modify、replace、block | 是 |
| after_tool_call | ToolResultMiddlewarePayload | continue、modify | 否 |
| before_compaction | CompactionMiddlewarePayload | continue、block | 是 |
| after_run | RunResultMiddlewarePayload | continue | 否 |
| on_error | ErrorMiddlewarePayload | continue、retry、handled | 否 |

MiddlewareDecision：type 必填；payload、reason_code、reason 默认 null。
update、modify、replace、handled、messages 必须带 payload；continue、block、retry 不得带 payload。

| 带 payload 的决策位置 | payload 类型 / 限制 |
| --- | --- |
| before/after_system_prompt_build update | SystemPromptBuildPayload；prompt 为非空完整替换正文 |
| before_run modify | RunMiddlewarePayload |
| before_turn messages | 非空 MessagesMiddlewarePayload |
| before_llm replace | LLMResponse，直接替代此次模型调用结果，不是修改 LLMRequest |
| after_llm modify | LLMResponse |
| before_tool_call modify | ToolCallMiddlewarePayload |
| before_tool_call replace | ToolResultMiddlewarePayload |
| after_tool_call modify | ToolResultMiddlewarePayload |
| on_error handled，operation=llm | LLMResponse |
| on_error handled，operation=tool | ToolResultMiddlewarePayload |

on_error 仅 LLM 错误可 retry；其他 operation 不因枚举存在就可 handled。
Tool 决策必须保持原 tool_call_id 和 tool_name，包括替换结果的身份。
messages 中每条完整 Message 必须与当前 Session/run 一致、role=USER、visibility=MODEL、
message_id 不重复，且无 model/stop_reason/error 等 Assistant 专属字段。

MiddlewareContext 可读字段：invocation_id、point、ctx、run_id、trace_id、turn_index、
attempt（默认 1）、state_snapshot、payload。state_snapshot 是隔离的只读语义快照，
初始化挂点的 run_id、trace_id、state_snapshot、turn_index 均为 None；
其他挂点必须携带运行身份与 state_snapshot。运行态写入由 StateManager 负责；
messages 等改动由 Runtime 按保存屏障落盘。

专用 payload 字段如下，未注明默认值的字段必填。

| 类型 | 字段 |
| --- | --- |
| SystemPromptBuildPayload | prompt: str；before 输入基础 Prompt，after 输入组合后的完整 SYSTEM |
| RunMiddlewarePayload | messages: tuple[Message, ...]；output_mode: OutputMode |
| TurnMiddlewarePayload | messages: tuple[Message, ...] |
| MessagesMiddlewarePayload | messages: tuple[Message, ...]，非空 |
| ToolCallMiddlewarePayload | call: ToolCallBlock |
| ToolResultMiddlewarePayload | call: ToolCallBlock；result: ToolResultBlock |
| CompactionMiddlewarePayload | model: str；compacted_message_ids: tuple[str, ...]；before_tokens、trigger_tokens、recent_message_budget_tokens、retained_message_tokens: int |
| RunResultMiddlewarePayload | status: SessionStatus；message: Message 或 None = None；error: RunError 或 None = None |
| ErrorMiddlewarePayload | operation: ErrorOperation；error: RunError；tool_call: ToolCallBlock 或 None = None |

LLMRequest / LLMResponse 使用 `agentsystem/llm/models.py` 的完整模型；
消息和工具内容类型见 `agentsystem/message/models.py`。构造修改结果时保留原消息和调用身份。

### 执行与失败

continue 继续，modify 更新传给后续 handler 的 payload，messages 按处理链累积；
block/replace/retry/handled 等终结决策使当前链返回。
update 也立即终止当前链；before 的 update 跳过默认分区构建（包括 Memory 读取），
但仍执行 after 挂点；after 的 update 替换最终 SYSTEM。
handler 异常/timeout 按 failure_mode 处理；fail_closed 在许可挂点生成阻止决策。
初始化挂点的 fail_closed 异常/timeout 直接传播，使初始化失败。
返回类型或决策/payload 违规时，仅 after_run/after_turn 记录并隔离，其他挂点传播异常。
取消继续传播；外部副作用须自行保证幂等，Runtime 不回滚。

RuntimeConfig.middleware_llm_retry_limit 默认 1、整数 ≥ 0，是模型中间件重试预算；
after_run 只观察已收敛的 succeeded/failed/canceled 结果。

### 内置 Middleware 配置

| name | 挂点 | plugin_options | 默认 / 限制 |
| --- | --- | --- | --- |
| turn-reminder | before_turn | turn-reminder.remind_at | 256；整数 ≥ 1 或 null |
| repeat-tool-reminder | before_turn | repeat-tool-reminder.remind_at | 3；整数 ≥ 2 或 null |
| todo-reminder | before_turn | 无 | 宿主提供 TodoReminder 时注册；仅 todo_write 被选中且距上次写入和提醒均达到 10 条助手消息时注入当前清单或建单提示；fail_open |
| provider-writeback | after_run | 无 | 仅 succeeded 且存在最终 Message 时调用选中 Provider.put |

提醒配置的未知子字段拒绝；未配置采用默认值。
turn-reminder 在指定轮次提醒；repeat-tool-reminder 在当前 run 尾部连续相同工具名与
规范化参数达到阈值时提醒。两者都是仅模型可见、持久化的 USER 消息，不公开给前端。
它们不是硬轮次/调用上限，正文目前也不能通过 YAML 修改。

### 配置联动示例

关闭 turn-reminder 注册并保留成功回写：

```yaml
enabled_middlewares: [provider-writeback]
plugin_options:
  turn-reminder:
    remind_at: null
```

不能设置 remind_at=null 却仍精确筛选 turn-reminder，否则零匹配导致装配失败。
两个提醒的参数相互独立，关闭一个不关闭另一个。

### 实施应用中间件

沿用目标应用插件风格，编写 async handler(context) 返回 MiddlewareDecision，
在同步 register 中调用 register_middleware，明确 name、handler_id、point，
仅按实际需要设置作用域、优先级、超时和失败模式。
合并 enabled_middlewares，配置筛选使用 name，而不是 handler_id。

直接复用并保留现有 payload 身份；需要修改时遵循上表的类型和挂点约束。
不要仅新增观察日志便声称修复了目标行为，也不要修改 Runtime 的保存或收敛协议。

### 验证要求

检查挂点/决策矩阵、排序和作用域，验证合法修改、非法 payload、失败模式及 timeout。
验证提醒阈值而不是假设它终止运行；检查 provider-writeback 等原有必要能力是否被误筛掉。
