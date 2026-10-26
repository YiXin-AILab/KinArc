# 上下文压缩

compaction 控制上下文预算、工具内容剪枝和历史摘要。

| `compaction` 字段 | 默认值 | 约束 / 用途 |
| --- | --- | --- |
| `enabled` | true | 是否发起新剪枝/摘要；false 不撤销已经保存的压缩历史 |
| `safety_margin_tokens` | 2048 | 正整数，输入安全余量 |
| `trigger_ratio` | 0.80 | 严格在 0 与 1 之间 |
| `recent_message_ratio` | 0.20 | 大于 0 且小于 trigger_ratio |
| `media_block_tokens` | 2048 | 正整数，多模态 Block 估算成本 |
| `tool_argument_max_chars` | 512 | 正整数，每个字符串参数的保留前缀长度，不限制整个参数对象；剪枝标记额外追加 |
| `tool_result_max_chars` | 2048 | 正整数，工具结果剪枝阈值及保留前缀长度 |

`hard_input_limit = context_window - (max_output_tokens or 0) - safety_margin_tokens > 0`。
`max_output_tokens: null` 不预留固定输出额度，仍保留输入安全余量。
触发线取 `floor(hard_input_limit * trigger_ratio)`。
近端消息预算取 `floor(hard_input_limit * recent_message_ratio)`，不是“保留 N 条消息”；
工具调用/结果批次不拆开，因此实际保留量可能超过比例。工具剪枝仅在完整候选低于触发线时
提交，否则尝试摘要。

max_output_tokens 越大，可用输入预算越小；context_window 应匹配模型真实容量。

## 配置示例与预算计算

合并到现有 AgentConfig 的局部示例：

```yaml
compaction:
  enabled: true
  safety_margin_tokens: 2048
  trigger_ratio: 0.80
  recent_message_ratio: 0.20
  media_block_tokens: 2048
  tool_argument_max_chars: 512
  tool_result_max_chars: 2048
```

配合 context_window=128000、max_output_tokens=16384：
hard_input_limit=109568，触发线为 87654，近端消息预算为 21913。
总预算包含 SYSTEM、summary、工具 schema；近端预算仅统计历史 messages。
这些数值是本地估算，不保证与服务端 token 计数一致。

## 扩展接口

摘要复用 llm_model 的客户端、模型与生成配置，包括输出预算。
before_compaction Middleware 仅允许 continue 或 block，分别继续或阻止压缩。
自定义摘要算法或边界策略属于 Runtime 代码扩展，
应保持工具批次完整、消息权威和事件投影合约。
即使 enabled=false，构造时仍检查 hard_input_limit 正数，历史 CompactionBlock 也仍重放。

## 优化与回归检查

降低触发比例会更早压缩，可能增加摘要调用并丢失远端细节；提高则减少余量。
缩小两项 max_chars 可能剪掉关键数据。宿主提供 MessageManager 时可启用 tool_content_view，
仅在上下文明示目标已剪枝或上一页未读完时调用，以 tool_call_id 标识目标，
part 选择 arguments 或 result，offset 默认 0、limit 默认及上限 8000 字符。
多模态任务同时检查 media_block_tokens 估算和 media.include_asset_id_in_context 句柄提示。

覆盖：预算刚好触发/未触发、无可压缩内容、工具批次边界、剪枝不够则摘要、
历史重放、长工具结果、摘要失败与取消。
