# Plugin

## 优化策略

让已实现的能力通过正确入口、参数和宿主依赖完成装配。
Plugin 解决注册与配置消费问题，能力本身的执行错误转到对应组件；
按需知识放入 [Skill](skills.md)，不通过注册阶段全部塞入上下文。

### 1. 沿装配链定位

| 发现问题类型 | 建议操作内容 |
| --- | --- |
| 入口未执行 | 核对 plugins/ 直属文件、发现规则与同步 register(context) |
| 注册发生但能力缺失 | 核对注册调用、条件分支、名称冲突与最终筛选集合 |
| 配置修改无效 | 对照 plugin_options 的 key、context.options(key) 与参数传递，确认实际消费 |
| 工厂或处理器缺少服务 | 核对宿主按精确类型提供的依赖，不用配置 key 假装创建服务 |
| 资源文件存在但未被使用 | 建立显式读取、校验和消费路径，确认内容最终交给哪个组件 |

### 2. 组织注册与参数

将相关能力放在职责明确的入口，register 负责装配，将业务执行留给 handler 或实例。
参数从读取、严格校验到传入实际消费者形成完整链路；
只暴露确有变化需求的参数，不为单次 case 增加开关。

必需服务缺失应明确失败；可选服务才允许缺失，并定义不提供该能力时的行为。
复用宿主提供的服务与连接生命周期，不在插件中另建同职责的连接池或依赖装配体系。
共享参数改变时，检查该入口注册的全部能力；筛选发生在注册之后，不能假设禁用即跳过校验。

### 3. 验证装配到调用

按“入口被发现 → 参数被消费 → 依赖可得 → 能力被选中 → 实际调用”验证，
在现有 Changelog 中记录断点与修复证据。
同时检查缺失参数、缺失依赖与名称冲突；注册成功不能替代组件行为测试。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 新增多个入口注册同一能力 | 归并注册责任，核对名称和筛选引用；每项能力有明确来源 |
| 参数越来越多但效果不明 | 跟踪每项消费者，移除未消费或失效项；不靠静默默认值掩盖拼写错误 |
| 修改共享配置破坏其他能力 | 对照所有消费者验证参数与初始化条件，保留原有合法装配路径 |
| 文件复制完成就宣称能力生效 | 验证读取、注册、筛选与实际调用；资源目录本身没有自动加载语义 |
| 为缺失服务新增临时连接或兜底实现 | 核对宿主边界，记录本轮无法满足的依赖；不伪造结果或绕过生命周期管理 |

## 系统修改说明

Plugin 在 Application 装配时注册 Tool、Middleware 和 ContextProvider，参数由 YAML 提供。

### 发现、配置与启用

1. 将入口放在 Application 的 plugins/ 直属 .py 文件中。
2. 导出同步 register(context)，通过 PluginSetupContext 注册能力，返回 None。
3. 使用 context.options(key) 读取 plugin_options 中的同名参数组，并以专属模型校验。
4. 通过 enabled_tools、enabled_middlewares 或 enabled_context_providers 选择注册能力。

发现器忽略 __init__.py 及以 . / _ 开头的模块，不递归扫描子目录。
无 register 的模块不贡献能力；文件名后缀不决定能力类型。
筛选发生在注册之后，禁用能力不会跳过插件 import、注册及其参数校验。

plugin_options 的 key 由插件自行约定，与文件名、Plugin ID、工具名无隐式关联。
缺失 key 返回空 mapping；重复一级 key 拒绝，未消费或共享 key 允许。

### PluginSetupContext 完整接口

| 接口 | 返回 / 用途 |
| --- | --- |
| plugin_id | 当前插件的只读内部身份 |
| agent_id | 当前装配 Agent 的只读 ID |
| options(key) | 该参数组的只读 mapping |
| get(service_type) | 按精确类型查询宿主服务，缺失返回 None |
| require(service_type) | 查询必需服务，缺失报错 |
| register_tool(handler, ...) | 注册工具，返回 None；全部参数见 [Tool](tools.md) |
| register_middleware(handler, ...) | 注册中间件，返回 None；全部参数见 [Middleware](middlewares.md) |
| register_context_provider(provider_type, create) | 注册 Provider 工厂，返回 None；见 [ContextProvider](context-providers.md) |

PluginSetupContext 在 register 结束后密封；运行时使用注册阶段提取的参数和服务。
同一插件内 Tool 名、Provider type、Middleware handler_id 不可重复；
跨插件选择后的 Tool/Middleware 名不可重复，Provider type 定义重复直接失败。

标准装配提供 SkillIndex、ContextProviderRunner，宿主额外注入 SkillRoots 供 skill_view 拼接路径。MessageManager、ScheduleService
及其他服务由宿主按精确类型注入；MemoryService、TodoService 和 TodoReminder 也是
宿主能力，内置插件依据服务是否可用注册候选；连接池生命周期由宿主管理。
外部工具连接由宿主代码创建并注入，应用插件复用宿主提供的依赖。

### Plugin 的 references 如何使用

Plugin 附带资源由代码显式读取；references/ 无标准加载协议，不自动进入模型上下文。
相对路径基准由插件定义，不默认相对于 agent.yaml 或进程工作目录。

| 目标 | 接入方式 |
| --- | --- |
| 短的工具调用规则 | 读取文本后传给 register_tool 的 rules |
| 业务模板或配置数据 | 插件读取、校验，并由 handler 使用 |
| 模型按需查询资料 | 注册返回资料的 Tool，或将其组织为独立 Skill |
| 每次请求的动态信息 | 注册 ContextProvider |

skill_view 按根目录/name/file_path 直接读取，不查询 SkillIndex 或文件索引；实际可读范围由进程权限决定。
配置允许选择文件时，实现应校验相对路径、允许的根目录、文件类型及内容；
外部资源读取还须设置有限 timeout。

### 内置插件参数

| key | 全部子字段 | 默认与范围 | 校验时机 |
| --- | --- | --- | --- |
| turn-reminder | remind_at | 256；整数 ≥ 1 或 null | 注册时 |
| repeat-tool-reminder | remind_at | 3；整数 ≥ 2 或 null | 注册时 |
| current-datetime | 无 | {}，拒绝未知子字段 | Provider 被选择、创建时 |

自定义 key 的字段、默认值和失败条件由消费模型定义。

### 验证要求

验证 import/register、专属参数、资源路径与内容、能力筛选及实际调用。
至少覆盖正常文件、缺失文件、越界路径和未知参数。
长资料宜按需提供，避免全部复制到 rules 或 Prompt。
