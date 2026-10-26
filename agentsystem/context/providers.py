"""隔离失败并保持配置顺序的 ContextProvider 编排。"""

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid5

from agentsystem.common.logging import log_unexpected_exception
from agentsystem.common.models import SessionContext
from agentsystem.message.models import (
    InjectedBlock,
    InjectedItem,
    Message,
    MessageOrigin,
    MessageVisibility,
    Role,
)

_INJECTION_NAMESPACE = UUID("ad61ab6e-0175-40d5-98a9-a336775f6880")
LOGGER = logging.getLogger(__name__)


class ContextProvider(Protocol):
    """为目标 USER Message 提供动态上下文并消费成功结果的异步端口。

    实例由 Agent 初始化阶段创建，Runner 管理调用超时和普通失败隔离。
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """返回稳定且唯一的 Provider 名称。"""
        ...

    @abstractmethod
    async def get(
        self,
        ctx: SessionContext,
        *,
        message: Message,
    ) -> InjectedItem | None:
        """为一个用户输入生成动态上下文。

        Args:
            ctx: 当前会话的业务身份。
            message: 带 run_id 的目标 USER Message。

        Returns:
            带自身 provider_name 的注入项；没有内容时返回 None。
        """
        ...

    @abstractmethod
    async def put(
        self,
        ctx: SessionContext,
        *,
        message: Message,
    ) -> None:
        """接收成功 run 的最终结果，可由实现回写外部上下文来源。

        Args:
            ctx: 当前会话的业务身份。
            message: 成功终态保存后的最终 Assistant Message。
        """
        ...


class ContextProviderRunner:
    """并发调用独立 Provider，并生成一条定稿 Injection Message。

    每个 Agent 独立持有实例；启动时注册并冻结，运行时保留配置顺序并隔离单项失败。
    不持久化注入消息，也不自行验证回写调用是否来自成功终态。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_content_chars: int = 20_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """创建尚未冻结的空 Runner，并校验有限 timeout。

        Args:
            timeout_seconds: 每个 Provider get/put 调用的秒数上限。
            max_content_chars: 单个注入项允许的最大字符数，超出时丢弃该项。
            clock: 注入消息的时间工厂，缺省使用 UTC 当前时间。

        Raises:
            ValueError: timeout 或字符上限不为正数。
        """
        if timeout_seconds <= 0 or max_content_chars <= 0:
            raise ValueError("ContextProvider timeout 和内容上限必须为正数")
        self._providers: tuple[ContextProvider, ...] = ()
        self._timeout = timeout_seconds
        self._max_content_chars = max_content_chars
        self._clock = clock or (lambda: datetime.now(UTC))
        self._frozen = False

    def register_many(self, providers: Sequence[ContextProvider]) -> None:
        """完整校验批次后一次性追加 Provider。

        Args:
            providers: 按期望注入顺序排列的实例。

        Raises:
            RuntimeError: Runner 已冻结。
            ValueError: Provider 名称为空或与本批次、已有实例重复。
        """
        if self._frozen:
            raise RuntimeError("ContextProviderRunner 已冻结")
        items = tuple(providers)
        combined = (*self._providers, *items)
        names = tuple(provider.provider_name for provider in combined)
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("ContextProvider 名称不能为空或重复")
        self._providers = combined

    def freeze(self) -> None:
        """永久关闭注册入口并允许运行期调用。"""
        self._frozen = True

    async def build_injection(
        self,
        ctx: SessionContext,
        target: Message,
    ) -> Message:
        """为目标 USER 并发获取注入项，失败项被脱敏隔离。

        Args:
            ctx: 用于 Provider 调用和注入消息身份的会话上下文。
            target: 已保存且带 run_id 的 USER Message。

        Returns:
            按 Provider 注册顺序收集有效项的确定性 ID 消息；无有效项时内容项为空。

        Raises:
            RuntimeError: Runner 尚未冻结。
            ValueError: 目标不是带 run_id 的 USER Message。

        普通 Provider 错误记录后丢弃该项，取消异常继续传播。
        """
        self._ensure_frozen()
        if target.role is not Role.USER or target.run_id is None:
            raise ValueError("ContextProvider 目标必须是带 run_id 的 USER Message")
        results = await asyncio.gather(
            *(self._get_one(provider, ctx, target) for provider in self._providers)
        )
        items = tuple(item for item in results if item is not None)
        identity = f"{ctx.app_id}/{ctx.agent_id}/{ctx.session_id}/{target.message_id}"
        return Message(
            message_id=str(uuid5(_INJECTION_NAMESPACE, identity)),
            session_id=ctx.session_id,
            role=Role.USER,
            content=(
                InjectedBlock(
                    target_user_message_id=target.message_id,
                    items=items,
                ),
            ),
            origin=MessageOrigin.CONTEXT_PROVIDER,
            visibility=MessageVisibility.MODEL,
            run_id=target.run_id,
            created_at=self._clock(),
        )

    async def put(self, ctx: SessionContext, *, message: Message) -> None:
        """并发向全部 Provider 回写成功 run 的最终结果。

        Args:
            ctx: 当前会话身份。
            message: 调用方确认已成功保存的最终 Assistant Message。

        Raises:
            RuntimeError: Runner 尚未冻结。

        单项超时或普通异常只记录日志，取消异常继续传播。
        """
        self._ensure_frozen()
        await asyncio.gather(
            *(self._put_one(provider, ctx, message) for provider in self._providers)
        )

    async def _get_one(
        self,
        provider: ContextProvider,
        ctx: SessionContext,
        target: Message,
    ) -> InjectedItem | None:
        try:
            item = await asyncio.wait_for(
                provider.get(ctx, message=target),
                timeout=self._timeout,
            )

        except TimeoutError:
            LOGGER.warning(
                "context_provider_timeout",
                extra={"provider_name": provider.provider_name},
            )
            return None
        except Exception as error:
            log_unexpected_exception(
                LOGGER,
                "context_provider_failed",
                error,
                operation="context.provider",
                provider_name=provider.provider_name,
            )
            return None
        if item is None:
            return None
        if (
            item.provider_name != provider.provider_name
            or len(item.content) > self._max_content_chars
        ):
            return None
        return item

    async def _put_one(
        self,
        provider: ContextProvider,
        ctx: SessionContext,
        message: Message,
    ) -> None:
        try:
            await asyncio.wait_for(
                provider.put(ctx, message=message),
                timeout=self._timeout,
            )
        except TimeoutError:
            LOGGER.warning(
                "context_provider_put_timeout",
                extra={"provider_name": provider.provider_name},
            )
        except Exception as error:
            log_unexpected_exception(
                LOGGER,
                "context_provider_put_failed",
                error,
                operation="context.provider.put",
                provider_name=provider.provider_name,
            )

    @property
    def timeout_seconds(self) -> float:
        """返回单 Provider 调用的有限 timeout。"""
        return self._timeout

    @property
    def frozen(self) -> bool:
        """返回 Runner 是否完成启动期冻结。"""
        return self._frozen

    def _ensure_frozen(self) -> None:
        if not self._frozen:
            raise RuntimeError("ContextProviderRunner 必须冻结后调用")
