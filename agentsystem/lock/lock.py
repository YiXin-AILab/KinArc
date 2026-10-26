"""与 Redis 无关的异步 SessionLock 领域端口。"""

from enum import StrEnum
from typing import Protocol

from pydantic import Field

from agentsystem.common.models import FrozenModel, SessionContext


class LockLease(FrozenModel):
    """持锁期间由调用方保存的不可变凭证，不自动续租或释放。

    Attributes:
        owner_token: 本次获取锁的唯一令牌，续租和释放必须原样传回。
    """

    owner_token: str = Field(min_length=1)


class ReleaseIfIdleResult(StrEnum):
    """原子关门结果，供执行者判断是否退出或继续领取输入。

    RELEASED 表示已释放，DIRTY 表示仍有输入且保持锁，NOT_OWNER 表示
    当前令牌已不拥有锁；调用方不得将后两种结果当作释放成功。
    """

    RELEASED = "released"
    DIRTY = "dirty"
    NOT_OWNER = "not_owner"


class SessionLock(Protocol):
    """以有限租约保证同一会话单写者的异步端口。

    调用方负责持有令牌、周期续租和退出释放；实现负责原子比较所有权，
    并在关门时检查输入活动。协议不创建后台心跳任务。
    """

    async def acquire(self, ctx: SessionContext) -> LockLease | None:
        """尝试获取会话锁，不等待已有持锁者释放。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            获取成功时的租约凭证；锁忙时为 None。
        """
        ...

    async def heartbeat(self, ctx: SessionContext, owner_token: str) -> bool:
        """仅为仍匹配 owner token 的锁原子续期。

        Args:
            ctx: 目标会话的完整作用域。
            owner_token: acquire 返回的所有权令牌。

        Returns:
            续期成功时为 True；过期或所有权不匹配时为 False。
        """
        ...

    async def release(self, ctx: SessionContext, owner_token: str) -> bool:
        """比较所有权后原子释放锁。

        Args:
            ctx: 目标会话的完整作用域。
            owner_token: acquire 返回的所有权令牌。

        Returns:
            本次删除匹配锁时为 True，不存在或不匹配时为 False。
        """
        ...

    async def release_if_idle(
        self,
        ctx: SessionContext,
        owner_token: str,
    ) -> ReleaseIfIdleResult:
        """原子检查所有权和待处理输入，关闭退出与入队之间的竞态窗口。

        Args:
            ctx: 目标会话的完整作用域。
            owner_token: acquire 返回的所有权令牌。

        Returns:
            RELEASED 表示空闲并已释放；DIRTY 表示有输入、继续持锁；
            NOT_OWNER 表示所有权已失效。
        """
        ...
