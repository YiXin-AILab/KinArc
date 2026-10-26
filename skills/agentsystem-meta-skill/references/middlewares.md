# Middleware：注册、合法决策与内置提醒

Middleware 在生命周期挂点处理数据并返回控制决策。

## 全部注册参数

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

## 全部挂点、输入与可用决策

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

## 执行与失败

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

## 内置 Middleware 配置

| name | 挂点 | plugin_options | 默认 / 限制 |
| --- | --- | --- | --- |
| turn-reminder | before_turn | turn-reminder.remind_at | 256；整数 ≥ 1 或 null |
| repeat-tool-reminder | before_turn | repeat-tool-reminder.remind_at | 3；整数 ≥ 2 或 null |
| todo-reminder | before_turn | 无 | 宿主提供 TodoReminder 时注册；仅 todo_write 被选中且距上次写入和提醒均达到 10 条助手消息时注入当前清单或建单提示；fail_open |
| provider-writeback | after_run | 无 | 仅 succeeded 且存在最终 Message 时调用选中 Provider.put |
| schedule-run-start-tracker | before_run | 无 | 有 ScheduleService 才注册；fail_closed |
| schedule-run-terminal-tracker | after_run | 无 | 有 ScheduleService 才注册；fail_open |

提醒配置的未知子字段拒绝；未配置采用默认值。
turn-reminder 在指定轮次提醒；repeat-tool-reminder 在当前 run 尾部连续相同工具名与
规范化参数达到阈值时提醒。两者都是仅模型可见、持久化的 USER 消息，不公开给前端。
它们不是硬轮次/调用上限，正文目前也不能通过 YAML 修改。

## 配置联动示例

关闭 turn-reminder 注册并保留成功回写：

```yaml
enabled_middlewares: [provider-writeback]
plugin_options:
  turn-reminder:
    remind_at: null
```

不能设置 remind_at=null 却仍精确筛选 turn-reminder，否则零匹配导致装配失败。
两个提醒的参数相互独立，关闭一个不关闭另一个。

## 应用扩展

在 plugins/run_observer.py 注册运行完成日志；扩展须遵循上述决策矩阵：

```python
"""注册运行完成观察器。"""

import logging

from agentsystem.middleware.models import (
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewarePoint,
    RunResultMiddlewarePayload,
)
from agentsystem.plugin.context import PluginSetupContext

logger = logging.getLogger(__name__)


async def observe(context: MiddlewareContext) -> MiddlewareDecision:
    """记录运行终态，不改变执行结果。"""
    payload = context.payload
    if isinstance(payload, RunResultMiddlewarePayload):
        logger.info("Run finished: %s", payload.status.value)
    return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)


def register(context: PluginSetupContext) -> None:
    """注册 after_run 观察器。"""
    context.register_middleware(
        observe,
        name="run-observer",
        handler_id="observe",
        point=MiddlewarePoint.AFTER_RUN,
    )
```

合并到 config/agent.yaml；已有选择列表时保留其他所需名称：

```yaml
enabled_middlewares: [run-observer]
```

本例无自定义参数，无需 plugin_options；日志通过宿主日志配置输出。

## 验证要求

检查挂点/决策矩阵、排序和作用域，验证合法修改、非法 payload、失败模式及 timeout。
验证提醒阈值而不是假设它终止运行；检查 provider-writeback 和定时任务 tracker 是否被误筛掉。
