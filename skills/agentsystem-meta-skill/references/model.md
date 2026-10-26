# 模型配置

llm_model 配置模型连接、生成策略、本地上下文预算和供应商参数。

## 配置字段

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

## 配置示例

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

## 扩展方式

模型名由 adapter 解析；供应商扩展使用 provider_options，并验证字段、工具和媒体兼容性。
程序化宿主可通过 AgentRuntimeSpec.llm_provider 注入符合 LLMProvider 协议的实现；
该代码注入入口不意味着 llm_model.type 接受任意字符串。新增 YAML adapter 类型需扩展配置模型和宿主装配。

## 验证要求

- 更换 model：核对真实窗口、多模态、工具能力、供应商参数和成本。
- 调整输出：检查 length 截断及任务终止条件，并联动验证输入预算。
- 修改 stop/tool_choice：覆盖正常结束、必须调用工具、工具循环继续的场景，避免让模型无法收敛。
- parallel_tool_calls 同时控制执行分组；并发不安全的工具声明 is_concurrency_safe=false。
- request 参数映射、保留 key、流式拼接和 timeout 用本地模拟测试验证；真实模型明确 opt-in。
  有效参数范围由当前供应商文档与实际请求验证，不能仅凭本地 JSON 校验推断。
