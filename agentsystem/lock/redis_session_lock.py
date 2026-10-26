"""SessionLock 模块内聚的异步 Redis adapter。"""

from collections.abc import Callable
from pathlib import Path
from secrets import token_urlsafe
from typing import cast

from agentsystem.common.models import SessionContext
from agentsystem.lock.lock import LockLease, ReleaseIfIdleResult
from agentsystem.lock.redis_keys import LockRedisKeys
from agentsystem.redis.client import RedisClient, await_redis

_SCRIPT_DIR = Path(__file__).with_name("scripts")
_HEARTBEAT_SCRIPT = (_SCRIPT_DIR / "heartbeat.lua").read_text(encoding="utf-8")
_RELEASE_SCRIPT = (_SCRIPT_DIR / "release.lua").read_text(encoding="utf-8")
_RELEASE_IF_IDLE_SCRIPT = (_SCRIPT_DIR / "release_if_idle.lua").read_text(
    encoding="utf-8"
)


class RedisSessionLock:
    """以随机 owner token 和有限 TTL 实现会话锁。

    使用 Application 注入的共享 client，不管理其生命周期。心跳由调用方
    驱动；关门依赖注入的 Inbox order key 函数，不直接调用 Inbox adapter。
    """

    def __init__(
        self,
        client: RedisClient,
        *,
        prefix: str,
        inbox_order_key: Callable[[SessionContext], str],
        ttl_ms: int = 30_000,
    ) -> None:
        """保存共享 client、租约期限及用于原子关门的活动 key 函数。

        Args:
            client: Application 管理的异步 Redis client。
            prefix: 锁 key 的命名空间。
            inbox_order_key: 返回同会话 Inbox 顺序 key，须与锁处于相同 hash slot。
            ttl_ms: 获取和心跳刷新后的租约期限，单位为毫秒。

        Raises:
            ValueError: ttl_ms 非正数或 prefix 非法。
        """
        if ttl_ms <= 0:
            raise ValueError("lock ttl_ms 必须为正数")
        self._client = client
        self._keys = LockRedisKeys(prefix)
        self._inbox_order_key = inbox_order_key
        self._ttl_ms = ttl_ms

    async def acquire(self, ctx: SessionContext) -> LockLease | None:
        """尝试获取会话锁，不等待已有持锁者释放。

        Args:
            ctx: 目标会话的完整作用域。

        Returns:
            获取成功时的租约凭证；锁忙时为 None。
        """
        owner_token = token_urlsafe(32)
        acquired = await await_redis(
            self._client.set(
                self._keys.lock(ctx),
                owner_token,
                nx=True,
                px=self._ttl_ms,
            )
        )
        if not acquired:
            return None
        return LockLease(owner_token=owner_token)

    async def heartbeat(self, ctx: SessionContext, owner_token: str) -> bool:
        """仅为仍匹配 owner token 的锁原子续期。

        Args:
            ctx: 目标会话的完整作用域。
            owner_token: acquire 返回的所有权令牌。

        Returns:
            续期成功时为 True；过期或所有权不匹配时为 False。
        """
        result = await self._eval(
            _HEARTBEAT_SCRIPT,
            (self._keys.lock(ctx),),
            (owner_token, self._ttl_ms),
        )
        return result == 1

    async def release(self, ctx: SessionContext, owner_token: str) -> bool:
        """比较所有权后原子释放锁。

        Args:
            ctx: 目标会话的完整作用域。
            owner_token: acquire 返回的所有权令牌。

        Returns:
            本次删除匹配锁时为 True，不存在或不匹配时为 False。
        """
        result = await self._eval(
            _RELEASE_SCRIPT,
            (self._keys.lock(ctx),),
            (owner_token,),
        )
        return result == 1

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
        result = await self._eval(
            _RELEASE_IF_IDLE_SCRIPT,
            (self._keys.lock(ctx), self._inbox_order_key(ctx)),
            (owner_token,),
        )
        if result == 1:
            return ReleaseIfIdleResult.RELEASED
        if result == 0:
            return ReleaseIfIdleResult.DIRTY
        return ReleaseIfIdleResult.NOT_OWNER

    async def _eval(
        self,
        script: str,
        keys: tuple[str, ...],
        arguments: tuple[str | int, ...],
    ) -> int:
        result = await await_redis(
            self._client.eval(script, len(keys), *keys, *arguments)
        )
        return cast(int, result)
