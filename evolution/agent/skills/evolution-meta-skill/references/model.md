# 模型配置

## 优化策略

依据请求、结束原因和任务行为调整模型参数，在质量、延迟与预算之间作出可验证的选择。
正常结束但答错时，先检查信息、规则与工具，不默认扩大预算或更换模型。

### 1. 区分失败机制

| 发现问题类型 | 建议操作内容 |
| --- | --- |
| 生成因 length 或停止字符串提前结束 | 检查实际结束原因、输出用量与 stop 命中，调整对应参数并重算输入余量 |
| 请求超时或服务报错 | 区分延迟、连接、参数不支持等原因；timeout 是单次请求限时，不是重试次数 |
| 输入超限或长对话遗忘 | 联动 [压缩](compaction.md) 核算预算，检查实际上下文，不能只提高输出上限 |
| 工具或媒体能力不兼容 | 核对目标模型与 adapter 支持情况，修正请求或选择允许的兼容模型 |
| 参数合法但质量不稳定 | 固定任务与其他条件重复比较，检查具体行为差异，不用一次分数判断优劣 |

### 2. 设计参数实验

明确“参数变化 → 预计影响 → 可观察结果”，每轮围绕一个原因假设调整。
需要共同满足预算或兼容约束的字段联动计算，其余条件保持一致。
temperature 等生成参数只在模型确实支持时使用；不能假定降低温度即可修复错误推理。

增加 max_output_tokens 会减少输入空间；增大 context_window 不会扩大服务端容量。
延长 timeout 只在正常请求确实需要更多时间时有意义，不解决循环或错误请求。
更换模型时比较同一批任务，并核对工具、多模态、窗口、费用和部署可用性。

### 3. 根据行为保留配置

在现有 Changelog 中记录参数差异、目标错误、成功率、输入输出用量与延迟等相关证据。
比较目标失败场景与已有正确场景；发现波动时增加重复验证，而非选择最好的一次结果。
实际请求未验证时明确标注，不将本地 schema 接受参数当作供应商支持。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 输出、窗口或超时逐轮上调，错误没有变化 | 回查结束原因与执行轨迹，撤回无效放宽；不以消耗增加替代原因定位 |
| provider_options 覆盖正式字段，配置与预算不一致 | 使用正式字段并核对最终请求映射，保留单一有效值 |
| stop 或 tool_choice 提高局部得分却破坏正常结束 | 验证文本完成、必要工具调用与工具循环，删除阻碍收敛的约束 |
| 并发加速导致依赖错误 | 核对 [Tool](tools.md) 的并发声明和调用依赖，等前次结果后再构造依赖参数 |
| 更换模型后仅报告分数上涨 | 同时核对成本、延迟、能力兼容与重复结果，注明未控制的差异 |

## 系统修改说明

llm_model 配置模型连接、生成策略、本地上下文预算和供应商参数。

### 配置字段

字段全部平铺在 llm_model 下，由 ModelParamsConfig 和 LLMGenerationConfig 共同定义。

| 字段 | 默认值 | 规则 / 影响 |
| --- | --- | --- |
| `type` | `litellm` | 配置模型支持的 adapter 类型 |
| `model` | 必填 | 非空模型名；由部署方提供可用型号 |
| `api_key` | `null` | 通过环境变量引用凭证 |
| `base_url` | `null` | 通过环境变量引用自定义模型服务地址 |
| `timeout_seconds` | `60.0` | 正数，单次模型请求有限超时 |
| `context_window` | `128000` | 正整数，本地上下文预算，不会扩大服务端窗口 |
| `temperature` | `null` | `0..2`；`null` 不显式传值，具体模型未必支持该参数 |
| `max_output_tokens` | `16384` | 正整数或 `null`；整数从输入预算扣除并映射为 `max_tokens`；显式 `null` 时不传输出上限 |
| `stop` | `[]` | 停止字符串列表，非空时传给 provider |
| `tool_choice` | `null` | JSON 值，具体语义由目标 provider 支持情况决定 |
| `parallel_tool_calls` | `null` | 有工具且非 null 时传递模型并行调用偏好；Runtime 仅在 true 时启用工具并发分组，false/null 时串行 |
| `provider_options` | `{}` | provider 专属 JSON 参数，不能当成 Runtime 通用开关 |

输入预算为 context_window - (max_output_tokens or 0) - compaction.safety_margin_tokens，
必须大于 0；安全余量默认 2048。显式 `null` 取消客户端输出上限，供应商默认限制仍可能生效。

LiteLLM adapter 显式禁止 `provider_options` 覆盖：`model`、`messages`、`stream`、
`stream_options`、`timeout`、`tools`，该检查在请求构造时发生。
其他 key 在普通请求参数后合并，由供应商解释。已有正式字段的参数应使用正式字段，
保持请求参数与本地预算一致。

### 配置示例

合并到 config/agent.yaml：

```yaml
llm_model:
  model: ${EXAMPLE_LLM_MODEL}
  api_key: ${EXAMPLE_LLM_API_KEY}
  temperature: 0
  max_output_tokens: 8192
  context_window: 128000
  timeout_seconds: 60
  stop: []
  parallel_tool_calls: false
  provider_options: {}
```

省略字段采用默认值；凭证和地址由部署环境提供。
LiteLLM 内部始终请求 stream=true，再转换为 Runtime 事件，不受 output_mode=complete 影响。
provider_options 中的 reasoning_effort 由 adapter 转换为对应请求参数及 allowed_openai_params；
是否可用取决于所选模型，通过 llm_model.provider_options 配置。

### 修改范围

只调整目标应用支持的模型字段及 provider_options；模型、服务和凭证由允许的部署环境提供。
新增 adapter、注入 LLMProvider 或修改宿主装配不属于本 Skill 的应用优化范围。
已有字段优先通过正式配置修改，不能用 provider_options 绕过本地预算。

### 验证要求

- 更换 model：核对真实窗口、多模态、工具能力、供应商参数和成本。
- 调整输出：检查 length 截断及任务终止条件，并联动验证输入预算。
- 修改 stop/tool_choice：覆盖正常结束、必须调用工具、工具循环继续的场景，避免让模型无法收敛。
- parallel_tool_calls 同时控制执行分组；并发不安全的工具声明 is_concurrency_safe=false。
  串行执行不解决参数依赖：需要前次结果才能构造的调用必须等结果返回后再发起。
- request 参数映射、保留 key、流式拼接和 timeout 用本地模拟测试验证；真实模型明确 opt-in。
  有效参数范围由当前供应商文档与实际请求验证，不能仅凭本地 JSON 校验推断。
