# Tool：定义与执行合约

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

## Handler 与模型 schema

默认 description 取 docstring 第一行；无说明且未显式传 description 时注册失败。
所有可见参数必须有类型注解，不支持 *args、**kwargs、位置专用参数。
默认值决定参数是否必填，枚举/类型描述应与真实执行一致。
parameters 必须是 object schema，properties 名称须与可见参数一致；
类型、枚举和 required 的一致性由作者验证。运行时按签名构造 invocation_model，拒绝未知参数。

普通 handler 必须返回 ToolOutput(content: str, metadata: JSON mapping = {})。
正常返回 “Error: ...” 仍是成功 ToolOutput。执行失败通过统一 ToolError 表达；
如业务协议将拒绝或未命中定义为正常查询结果，则可通过 ToolOutput 表达该业务状态。

同步 handler 由普通函数 adapter 直接调用，不能依赖外层 timeout 中断阻塞 I/O；
有阻塞操作时改用异步接口或显式线程隔离，并为 I/O 自身设置有限 timeout。

## 运行上下文、并发与授权

普通工具通过 get_tool_context() 读取 ToolInvocationContext：
session_context、run_id、runtime_trace_id、tool_call_id、tool_name、turn_index。
这些关联字段由 Runtime 提供，不属于模型可见参数；上下文仅在工具执行期间可用。

llm_model.parallel_tool_calls 同时影响模型调用偏好和 Runtime 执行分组；只有 true 才启用
并发分组，false/null 时串行；并发组仍受 is_concurrency_safe 与挂起工具约束。
后续参数依赖前次结果时，应先取得结果再发起下一次调用，不能只依赖串行执行。
工具执行期限由 RuntimeConfig.tool_timeout_seconds 统一配置，默认 30 秒、正数。
默认 ToolAuthorization 放行全部调用；授权/continuation 的策略实现由宿主注入。
can_suspend=true 声明工具支持挂起；授权、resume token 和续执行仍由对应策略与实现提供。

宿主代码可通过 ToolDefinition 的 source、handler、invocation_model、preset_args
接入底层工具定义或 MCP adapter。

## 内置工具与注册条件

| 工具名 | 注册前提 | 用途 |
| --- | --- | --- |
| skill_view | 内置注册，通过 enabled_tools 筛选 | 按 SkillRoots/name/file_path 拼接读取；不依赖 SkillIndex，支持相对或绝对路径及分页 TTL 缓存 |
| tool_content_view | 宿主提供 MessageManager | 读取当前 Session 被剪枝的工具参数/结果 |
| memory_read/add/update | 宿主提供已启用的 MemoryService | 读取、追加或精确替换当前用户启用范围的记忆 |
| kb_search | 宿主解析知识库引用或提供合法 bases 参数 | 按 query、kb 查询已配置的库 |
| todo_read/write | 宿主提供 TodoService | 读取或完整替换当前会话的任务清单 |
| schedule_task_create/list/update/delete/run/history | 宿主提供 ScheduleService | 管理定时任务，调用 schema 由六个注册定义提供 |

上述名称随后仍受 enabled_tools 筛选；无 Scheduler 时选择 schedule_task_* 会零匹配。
定时任务业务参数由工具注册定义的调用 schema 提供。

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

## 示例与验证

保存为 plugins/echo_tool.py；标准发现器加载直属 .py 模块并调用同步 register：

```python
"""注册回显工具。"""

from agentsystem.plugin.context import PluginSetupContext
from agentsystem.tool.models import ToolOutput


async def echo(text: str) -> ToolOutput:
    """按原文返回输入文本。"""
    return ToolOutput(content=text)


def register(context: PluginSetupContext) -> None:
    """注册模型可调用的 echo。"""
    context.register_tool(echo, name="echo")
```

合并到 config/agent.yaml，保留其他所需工具：

```yaml
enabled_tools: [echo]
```

调用 echo，参数 {"text":"hello"}，返回 hello。省略 enabled_tools 或设为 null
选择全部已注册工具，[] 全关；列表支持大小写敏感的 * 全名称匹配，每项至少匹配一个工具。
筛选发生在插件注册之后，禁用工具不会跳过 import/register。

验证工具名/描述/schema、必填/默认/非法参数、标准返回、ToolError、timeout、并发依赖，
以及实际启用列表和 Prompt/Skill 引用。修改 schema 后必须验证 handler 而非仅比较 JSON。
