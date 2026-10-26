# ContextProvider

## 优化策略

为当前用户输入提供相关、可信且足够的动态背景，并按需要回写可复用信息。
优化“信息如何进入决策”，不以注入量或命中条数衡量效果。

### 1. 定位信息缺口

沿“用户输入 → 查询 → 返回内容 → 注入消息 → 模型决策”核对 trace。
区分未启用、未查询、查询无结果、查询失败、注入被丢弃与内容已读但未被正确使用。
信息已注入仍可能存在来源不清、冲突或无关内容过多，不能直接排除 Provider 问题。

操作时必须核验的最新状态交由 [Tool](tools.md) 查询；
需要拦截执行转到 [Middleware](middlewares.md)。输入时的背景快照不能代替执行校验。

### 2. 设计检索与注入

| 设计问题 | 优化方法 | 如何修改 |
| --- | --- | --- |
| 查询未覆盖当前问题 | 构造任务相关查询 | 从当前输入与可信会话信息提取对象和约束，明确数据范围；不要靠扩大到整库掩盖查询错误 |
| 命中很多但有效信息少 | 筛选与去重 | 按当前决策需要筛选、排序并合并重复内容，优先保留直接证据及其适用条件 |
| 内容陈旧、矛盾或来源不明 | 表达证据边界 | 按需保留来源、时间、适用对象和冲突；区分事实、用户偏好与推断，不将检索文本写成系统指令 |
| 内容超过预算 | 按决策压缩 | 保留结论依据、条件和例外，删除无关段落；先缩小选择范围，再摘要，不能只截取前缀 |
| 信息不适用于当前输入 | 明确空结果语义 | 无相关背景时返回 None；在诊断中区分无结果与查询失败，不以固定演示内容填空 |

例如，注入政策结论时同时保留适用对象与生效范围，
避免模型把某类账户的政策当成所有账户的规则。

### 3. 设计回写与隔离

只回写有依据且值得复用的信息，区分稳定事实、偏好与短期状态。
run 成功不代表最终回答中的每句话都是真实事实；不要把整段回答直接累积为记忆。
需要更新时识别同一对象和重复信息，明确覆盖或失效条件，避免旧新结论长期并存。

实例由 Agent 共享，查询、缓存与回写按用户及会话隔离；
回写依赖 provider-writeback，不能仅实现 put 就假定会执行。

### 4. 验证决策收益

对比有相关信息、无相关信息、冲突或过期信息的场景，
检查实际注入内容及其是否改变目标决策；同时验证超时隔离和跨会话数据边界。
在现有 Changelog 中记录来源、筛选变化、注入长度与行为证据，避免复制敏感原文。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 每轮注入历史全文或不断扩大检索数量 | 按当前决策重新筛选、去重，检查跨轮重复；减少无关内容同时保留必要依据 |
| 摘要丢掉适用条件，或把冲突合成确定答案 | 恢复条件和来源差异，明确未知；压缩后仍能作出正确分支判断 |
| 回写内容越来越多且相互矛盾 | 按信息对象归并，清理重复、过期和无依据内容；不能把模型上次回答当成新的独立证据 |
| 缓存命中但内容属于其他用户或已失效 | 修正隔离键与失效条件，验证交错会话；不能只验证单会话命中率 |
| 注入存在但错误持续 | 检查模型可见内容、使用规则与实际时效需求，重新选择优化方式，不继续追加背景 |

## 系统修改说明

ContextProvider 为用户消息注入动态上下文，并可回写成功运行的最终结果。

### 全部配置和接口

| 位置 | 参数 / 字段 | 默认与约束 |
| --- | --- | --- |
| AgentConfig | enabled_context_providers | []；精确名称列表，按配置顺序；不支持通配符或重复项 |
| AgentConfig | plugin_options.<作者定义的 key> | 插件读取缺失 key 得 {}；参数模型由插件定义 |
| PluginSetupContext.register_context_provider | provider_type | 必填非空字符串，定义不可重复 |
| 同上 | create | 必填同步无参工厂，不得返回 awaitable |
| 实例 | provider_name | 与 provider_type 精确一致 |
| 实例 | async get(ctx, *, message) | 返回 InjectedItem 或 None |
| 实例 | async put(ctx, *, message) | 返回 None，处理成功 run 的最终 Message |
| InjectedItem | provider_name、content | 两项均必填非空字符串 |
| RuntimeConfig | context_provider_timeout_seconds | 5.0，正数，逐 Provider 调用限时 |
| RuntimeConfig | context_provider_max_content_chars | 20000，正整数，单个 item.content 上限 |
| AgentConfig.enabled_middlewares | provider-writeback | 默认为全选时包含；显式列表要保留才会成功回写 |

两项 RuntimeConfig 参数由宿主配置，不能通过 AgentConfig 或任意 plugin_options 覆盖。

### 发现、选择与数据流

1. plugins/ 直属模块同步 register 注册延迟工厂。
2. 仅在 enabled_context_providers 选择后创建该 Agent 的实例；工厂中可延迟校验专属参数。
3. 对带 run_id 的目标 USER Message 调用 get；多个 Provider 并发运行，结果按配置顺序收集。
4. Runner 构造独立的、仅模型可见的 Injection Message，关联 target_user_message_id，
   由 Runtime 正常投影保存；Provider 不直接持久化或改变用户原文。
5. 已启用 provider-writeback 时，succeeded 的最终 Message 并发回写到各 Provider.put。
   failed/canceled 不回写；缺少 writeback Middleware 时也不会自动调用 put。

get 返回 None 时不注入；异常/timeout 隔离当前 Provider，名称不符或超长 item 丢弃。
put 异常/timeout 记录并隔离，取消继续传播。授权须在执行边界校验，不能依赖注入文本。

实例由 Agent 共享；用户数据和缓存须按 SessionContext 隔离。

### 内置 current-datetime

current-datetime 输出服务器本地日期时间 YYYY-MM-DD HH:MM:SS。
plugin_options.current-datetime 仅接受 {}，创建时拒绝未知字段；
时区、格式或数据源等定制需注册独立 Provider。

合并到现有配置的局部例子：

```yaml
enabled_context_providers: [current-datetime]
enabled_middlewares: [provider-writeback]
plugin_options:
  current-datetime: {}
```

显式列表会筛掉未列出的原 Middleware，实际应用须保留仍需使用的名称。

### 实施自定义 Provider

在当前应用插件中通过 register_context_provider(provider_type, create) 注册同步工厂，
返回实现 provider_name、async get 和 async put 的实例；provider_name 与注册名称一致。
get 按当前输入查询必要信息，返回 InjectedItem(provider_name=..., content=...)
或 None；不需要回写时 put 返回 None。

需要参数时，以 context.options(key) 读取专属配置并用严格模型校验，
由工厂将参数和宿主已有服务传入实例。合并 enabled_context_providers，
需要成功回写时保留 provider-writeback；不要用固定演示文本代替目标所需的动态事实。

### 验证要求

覆盖被选择/未选择、工厂失败、名称不一致、非法异步实现、None、超长内容、
并发顺序、timeout/异常隔离、取消、成功回写和 Session 数据隔离。
