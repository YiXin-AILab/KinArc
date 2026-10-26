"""StateManager 模块内聚的异步 Redis CAS adapter。"""

from pathlib import Path
from typing import cast

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import SessionContext
from agentsystem.redis.client import RedisClient, await_redis
from agentsystem.runtime.state import AgentState
from agentsystem.state.manager import StateWriteResult
from agentsystem.state.persistence import StateCodec
from agentsystem.state.redis.keys import StateRedisKeys

_SCRIPT_DIR = Path(__file__).with_name("scripts")
_CREATE_SCRIPT = (_SCRIPT_DIR / "state_create.lua").read_text(encoding="utf-8")
_SAVE_SCRIPT = (_SCRIPT_DIR / "state_save.lua").read_text(encoding="utf-8")


class RedisStateManager:
    """通过单个 Redis key 与 Lua CAS 保存会话最新状态。

    依赖应用注入的异步 client，不拥有连接池；规范完整快照字节决定
    幂等及冲突结果，不使用额外版本号或消息内容。
    """

    def __init__(self, client: RedisClient, *, prefix: str) -> None:
        """接收共享异步 client；不拥有连接池。

        Args:
            client: 由应用统一创建并负责关闭的异步 Redis client。
            prefix: Redis namespace 前缀，仅允许字母、数字、冒号、下划线和连字符。

        Raises:
            ValueError: Redis prefix 为空或包含非法字符。
        """
        self._client = client
        self._keys = StateRedisKeys(prefix)
        self._codec = StateCodec()

    async def get(self, ctx: SessionContext) -> AgentState | None:
        """读取、校验 schema 并验证 session identity。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            已校验的最新状态；尚未创建时为 None。

        Raises:
            AdapterError: 存储状态无法解码或与请求身份不匹配。
        """
        encoded = await await_redis(self._client.get(self._keys.state(ctx)))
        if encoded is None:
            return None
        state = self._decode(encoded)
        if state.session_context != ctx:
            raise AdapterError(
                operation="state.read",
                code="state_context_mismatch",
                message="Redis state 的 SessionContext 与 key 不一致",
            )
        return state

    async def create(self, initial_state: AgentState) -> StateWriteResult:
        """原子创建 session 初始状态。

        Args:
            initial_state: 包含完整会话身份的初始执行快照。

        Returns:
            首次生效返回 APPLIED；已存相同目标快照返回 IDEMPOTENT。

        Raises:
            AdapterError: 已存在不同内容的状态，或存储内容非法。
        """
        encoded = self._encode(initial_state)
        result = await self._eval(
            _CREATE_SCRIPT,
            initial_state.session_context,
            (encoded,),
        )
        if result == -1:
            raise AdapterError(
                operation="state.create",
                code="state_conflict",
                message="session state 已存在不同内容",
            )
        return _write_result(result)

    async def save(
        self,
        previous: AgentState,
        current: AgentState,
    ) -> StateWriteResult:
        """比较完整 previous bytes 后原子覆盖 current。

        Args:
            previous: CAS 预期的完整旧执行快照。
            current: 拟保存的完整新执行快照，必须与旧快照属于同一会话。

        Returns:
            首次生效返回 APPLIED；已存相同目标快照返回 IDEMPOTENT。

        Raises:
            ValueError: previous 与 current 的会话身份不同。
            AdapterError: 状态不存在、旧快照不匹配或存储内容非法。
        """
        if previous.session_context != current.session_context:
            raise ValueError("previous/current SessionContext 不一致")
        result = await self._eval(
            _SAVE_SCRIPT,
            current.session_context,
            (self._encode(previous), self._encode(current)),
        )
        if result in {-2, -1}:
            raise AdapterError(
                operation="state.save",
                code="state_conflict",
                message="state CAS 前置条件不成立",
            )
        return _write_result(result)

    async def _eval(
        self,
        script: str,
        ctx: SessionContext,
        arguments: tuple[bytes, ...],
    ) -> int:
        result = await await_redis(
            self._client.eval(
                script,
                1,
                self._keys.state(ctx),
                *arguments,
            )
        )
        return cast(int, result)

    def _encode(self, state: AgentState) -> bytes:
        return self._codec.encode(state)

    def _decode(self, encoded: object) -> AgentState:
        return self._codec.decode(cast(bytes | str, encoded))


def _write_result(result: int) -> StateWriteResult:
    if result == 1:
        return StateWriteResult.APPLIED
    if result == 0:
        return StateWriteResult.IDEMPOTENT
    raise AdapterError(
        operation="state.write",
        code="state_write_result_invalid",
        message=f"未知 state write result：{result}",
    )
