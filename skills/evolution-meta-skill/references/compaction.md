# 上下文压缩

## 优化策略

在输入预算内保留后续决策所需的信息。以关键信息是否可用、任务是否正确完成衡量效果，
不以压缩次数减少或摘要更短衡量改进。

### 1. 找到信息丢失的位置

对照错误决策前的模型上下文、工具剪枝与摘要事件，确认所需信息原本是否存在，
在哪次处理后消失或变义。未发生摘要也可能已发生工具剪枝；历史中存在不等于当前可见。
信息从未进入上下文时，转查来源或工具；内容仍完整时，转查使用规则。

### 2. 按信息损失机制调整

先计算 hard_input_limit、触发线和近端预算，统计 SYSTEM、工具 schema、
历史及大结果的主要占用，避免只凭对话轮数猜测。

| 设计问题 | 优化方法 | 如何修改 |
| --- | --- | --- |
| 长工具结果的关键状态在前缀外被剪掉 | 保留决策信息 | 调整相关剪枝长度，或在 [Tool](tools.md) 中将必要状态与摘要清晰组织，并按需恢复详情 |
| 近期依赖尚未完成就进入摘要 | 调整近端保留 | 依据完整工具批次及任务跨度调整 recent_message_ratio，同时检查总输入预算 |
| 压缩过晚导致余量不足 | 提前触发 | 核对实际用量与估算偏差，再调整触发比例或安全余量；保留新增工具结果所需空间 |
| 压缩过频导致远端条件反复丢失 | 减少重复占用 | 清理冗余注入与过大结果，再评估触发比例；不直接关闭压缩 |
| 偶尔需要已剪枝的精确内容 | 按需恢复 | 验证 tool_content_view 可用，依据已剪枝标记读取目标调用的相关部分 |
| 媒体成本低估或资产引用丢失 | 联动媒体配置 | 核对估算成本与 [媒体](media.md) 句柄提示，分别验证预算和引用可用性 |

一次实验围绕一种损失机制调整，预算相关字段需要联动时一起核算。
摘要遗漏时结合实际上下文核对保留策略；本轮权限内无法解决时，记录运行时能力限制。

### 3. 验证保留与恢复

比较修改前后的实际上下文：任务目标、有效约束、未完成事项、工具结果状态与必要标识
是否仍可用于后续决策。恢复历史内容不等于刷新业务状态，执行前的最新状态仍由工具查询。
覆盖长对话与长单条结果，核对预算边界、工具批次和失败路径。
在现有 Changelog 中记录预算变化、关键内容去向、恢复开销与任务结果。

## 优化质量控制

| 风险信号 | 修正动作与完成条件 |
| --- | --- |
| 不断放大窗口或保留比例 | 核对真实模型容量和主要占用，移除无关内容；新配置必须满足有效输入预算 |
| 压缩变短但条件、例外或状态消失 | 对照原始证据恢复必要信息，验证依赖该信息的后续决策 |
| 每轮重复恢复同一大结果 | 检查结果组织与保留策略，减少反复读取；不能用恢复工具替代合理上下文设计 |
| 关闭压缩后认为旧信息已恢复 | 使用明确的测试会话验证；关闭不撤销已保存的剪枝或摘要 |
| 提醒或拦截压缩掩盖预算问题 | 回到内容占用和阈值计算，不以阻止压缩代替满足模型容量 |

## 系统修改说明

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
近端消息预算取 `floor(hard_input_limit * recent_message_ratio)`，不是"保留 N 条消息"；
工具调用/结果批次不拆开，因此实际保留量可能超过比例。工具剪枝仅在完整候选低于触发线时
提交，否则尝试摘要。

max_output_tokens 越大，可用输入预算越小；context_window 应匹配模型真实容量。

### 配置示例与预算计算

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

### 扩展接口

摘要复用 llm_model 的客户端、模型与生成配置，包括输出预算。
before_compaction Middleware 仅允许 continue 或 block，分别继续或阻止压缩。
本轮优化使用已有压缩配置与可用工具，不修改 Runtime 摘要算法或消息保存协议。
即使 enabled=false，构造时仍检查 hard_input_limit 正数，历史 CompactionBlock 也仍重放。

### 优化与回归检查

降低触发比例会更早压缩，可能增加摘要调用并丢失远端细节；提高则减少余量。
缩小两项 max_chars 可能剪掉关键数据。宿主提供 MessageManager 时可启用 tool_content_view，
仅在上下文明示目标已剪枝或上一页未读完时调用，以 tool_call_id 标识目标，
part 选择 arguments 或 result，offset 默认 0、limit 默认及上限 8000 字符。
多模态任务同时检查 media_block_tokens 估算和 media.include_asset_id_in_context 句柄提示。

覆盖：预算刚好触发/未触发、无可压缩内容、工具批次边界、剪枝不够则摘要、
历史重放、长工具结果、摘要失败与取消。
