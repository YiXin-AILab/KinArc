# ContextProvider：动态注入与成功回写

ContextProvider 为用户消息注入动态上下文，并可回写成功运行的最终结果。

## 全部配置和接口

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

## 发现、选择与数据流

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

## 内置 current-datetime

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

## 自定义 Provider 示例

保存为 plugins/source_provider.py：

```python
"""注册一个应用来源上下文 Provider。"""

from agentsystem.common.models import SessionContext
from agentsystem.message.models import InjectedItem, Message
from agentsystem.plugin.context import PluginSetupContext


class SourceProvider:
    """通过 Provider 合约向当前消息提供来源说明。"""

    @property
    def provider_name(self) -> str:
        """返回配置选择名称。"""
        return "source-context"

    async def get(
        self, ctx: SessionContext, *, message: Message
    ) -> InjectedItem:
        """返回当前应用标识，供模型理解请求来源。"""
        return InjectedItem(
            provider_name=self.provider_name,
            content=f"Application: {ctx.app_id}",
        )

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        """该示例无需回写。"""
        return None


def register(context: PluginSetupContext) -> None:
    """注册延迟构造函数。"""
    context.register_context_provider("source-context", SourceProvider)
```

选择 enabled_context_providers: [source-context] 后才启用。
如需参数，在 register 中以 context.options(key) 读取 plugin_options 的同名对象，
用严格参数模型校验，再由 create 工厂将参数传入实例；缺失 key 返回空 mapping。

## 验证要求

覆盖被选择/未选择、工厂失败、名称不一致、非法异步实现、None、超长内容、
并发顺序、timeout/异常隔离、取消、成功回写和 Session 数据隔离。
