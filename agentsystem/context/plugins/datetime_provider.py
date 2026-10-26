"""注册服务器本地日期时间 ContextProvider。"""

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from agentsystem.common.models import SessionContext
from agentsystem.context.providers import ContextProvider
from agentsystem.message.models import InjectedItem, Message
from agentsystem.plugin.context import PluginSetupContext


class DatetimeProvider(ContextProvider):
    """把时钟返回的日期时间注入当前 USER Message。

    默认使用服务器本地时区；实例仅持有时钟函数，没有外部资源或回写状态。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        """保存可测试的本地时钟。

        Args:
            clock: 日期时间工厂；缺省使用带本地时区的当前时间。
        """
        self._clock = clock or (lambda: datetime.now().astimezone())

    @property
    def provider_name(self) -> str:
        """返回稳定的 Provider 名称。"""
        return "current-datetime"

    async def get(
        self,
        ctx: SessionContext,
        *,
        message: Message,
    ) -> InjectedItem:
        """返回精确到秒的日期时间。

        Args:
            ctx: 统一 Provider 接口的会话身份，本实现忽略。
            message: 目标 USER Message，本实现忽略正文。

        Returns:
            current-datetime 注入项，文本格式为 YYYY-MM-DD HH:MM:SS。
        """
        del ctx, message
        return InjectedItem(
            provider_name=self.provider_name,
            content=self._clock().strftime("%Y-%m-%d %H:%M:%S"),
        )

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        """日期时间 Provider 不消费成功结果。

        Args:
            ctx: 统一 Provider 接口的会话身份，本实现忽略。
            message: 已保存的最终消息，本实现忽略且不产生副作用。
        """
        del ctx, message


class CurrentDatetimePluginOptions(BaseModel):
    """日期时间插件的空配置模型，禁止未知选项并在选中 Provider 时校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


def register(context: PluginSetupContext) -> None:
    """注册只在被 Agent 选中时校验并构造的 Provider Definition。

    Args:
        context: 当前 Agent 的插件注册上下文，提供选项和延迟工厂注册入口。
    """
    raw_options = context.options("current-datetime")

    def create() -> ContextProvider:
        """在 Provider 被选中时验证插件选项并创建隔离实例。"""
        CurrentDatetimePluginOptions.model_validate(raw_options)
        return DatetimeProvider()

    context.register_context_provider("current-datetime", create)


__all__ = ["CurrentDatetimePluginOptions", "DatetimeProvider", "register"]
