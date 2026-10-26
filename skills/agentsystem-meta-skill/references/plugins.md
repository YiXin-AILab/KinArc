# Plugin：配置与资源使用

Plugin 在 Application 装配时注册 Tool、Middleware 和 ContextProvider，参数由 YAML 提供。

## 发现、配置与启用

1. 将入口放在 Application 的 plugins/ 直属 .py 文件中。
2. 导出同步 register(context)，通过 PluginSetupContext 注册能力，返回 None。
3. 使用 context.options(key) 读取 plugin_options 中的同名参数组，并以专属模型校验。
4. 通过 enabled_tools、enabled_middlewares 或 enabled_context_providers 选择注册能力。

发现器忽略 __init__.py 及以 . / _ 开头的模块，不递归扫描子目录。
无 register 的模块不贡献能力；文件名后缀不决定能力类型。
筛选发生在注册之后，禁用能力不会跳过插件 import、注册及其参数校验。

plugin_options 的 key 由插件自行约定，与文件名、Plugin ID、工具名无隐式关联。
缺失 key 返回空 mapping；重复一级 key 拒绝，未消费或共享 key 允许。

## SetupContext 完整接口

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

SetupContext 在 register 结束后密封；运行时使用注册阶段提取的参数和服务。
同一插件内 Tool 名、Provider type、Middleware handler_id 不可重复；
跨插件选择后的 Tool/Middleware 名不可重复，Provider type 定义重复直接失败。

标准装配提供 SkillIndex、ContextProviderRunner，宿主额外注入 SkillRoots 供 skill_view 拼接路径。MessageManager、ScheduleService
及其他服务由宿主按精确类型注入；MemoryService、TodoService 和 TodoReminder 也是
宿主能力，内置插件依据服务是否可用注册候选；连接池生命周期由宿主管理。
MCP/external Tool 由宿主代码接入并注入运行容器。

## Plugin 的 references 如何使用

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

## 配置到使用的完整示例

本例从可配置文件读取工具规则，再注册 echo。以下是 Application 内的资产：

```text
plugins/
  echo_tool.py
  references/echo-rules.txt
```

plugins/references/echo-rules.txt：

```text
仅在用户要求回显文本时调用；保持输入原文，不添加解释。
```

plugins/echo_tool.py：

```python
"""注册使用本地规则文件的回显工具。"""

from pathlib import Path

from agentsystem.common.models import StrictModel
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.tool.models import ToolOutput


class EchoOptions(StrictModel):
    """回显插件参数。"""

    prefix: str = ""
    rules_file: str = "references/echo-rules.txt"


def register(context: PluginSetupContext) -> None:
    """读取插件参数和规则，注册回显工具。"""
    options = EchoOptions.model_validate(context.options("echo"))
    root = Path(__file__).resolve().parent
    relative = Path(options.rules_file)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("rules_file 必须是插件目录内的相对路径")
    resource = (root / relative).resolve(strict=True)
    if not resource.is_relative_to(root) or not resource.is_file():
        raise ValueError("rules_file 必须指向插件目录内的普通文件")
    rules = resource.read_text(encoding="utf-8").strip()
    if not rules:
        raise ValueError("rules_file 内容不能为空")

    async def echo(text: str) -> ToolOutput:
        """返回带配置前缀的输入文本。"""
        return ToolOutput(content=options.prefix + text)

    context.register_tool(echo, name="echo", rules=rules)
```

合并到 config/agent.yaml：

```yaml
plugin_options:
  echo:
    prefix: "结果："
    rules_file: references/echo-rules.txt
enabled_tools: [echo]
```

| 示例参数 | 默认值 | 生效方式 |
| --- | --- | --- |
| prefix | 空字符串 | 拼接到 echo 返回文本前 |
| rules_file | references/echo-rules.txt | 相对于 echo_tool.py 所在目录；注册时读取 UTF-8 非空文本 |

EchoOptions 拒绝未知字段；rules_file 是本例参数，资源无需独立启用。
rules 随装配进入 SYSTEM；echo({"text":"hello"}) 返回“结果：hello”。
参数或资源更新后重新装配，用新 Session 验证。

## 内置插件参数

| key | 全部子字段 | 默认与范围 | 校验时机 |
| --- | --- | --- | --- |
| turn-reminder | remind_at | 256；整数 ≥ 1 或 null | 注册时 |
| repeat-tool-reminder | remind_at | 3；整数 ≥ 2 或 null | 注册时 |
| current-datetime | 无 | {}，拒绝未知子字段 | Provider 被选择、创建时 |

自定义 key 的字段、默认值和失败条件由消费模型定义。

## 验证要求

验证 import/register、专属参数、资源路径与内容、能力筛选及实际调用。
至少覆盖正常文件、缺失文件、越界路径和未知参数。
长资料宜按需提供，避免全部复制到 rules 或 Prompt。
