"""绑定 Runtime 身份、管理记忆额度和工具错误。"""

from dataclasses import dataclass

from agentsystem.common.errors import ToolError
from agentsystem.common.models import SessionContext
from agentsystem.config.models import AgentMemoryConfig
from agentsystem.memory.store import MemoryEditResult, MemoryScope, MemoryStore


@dataclass(frozen=True, slots=True)
class MemoryLengthInfo:
    """本次成功编辑后的字符额度快照，不额外读取存储。

    Attributes:
        length_chars: 完整正文的 Unicode code point 数。
        limit_chars: 当前 Agent 对该范围配置的写入上限。
        remaining_chars: 上限减正文长度，最小为零。
    """

    length_chars: int
    limit_chars: int
    remaining_chars: int


@dataclass(frozen=True, slots=True)
class MemoryReadResult:
    """完整记忆读取结果，不截断正文，也不附带写入额度。

    Attributes:
        scope: 读取的记忆范围。
        content: 完整原文；记录不存在时为空字符串。
    """

    scope: MemoryScope
    content: str


@dataclass(frozen=True, slots=True)
class MemoryUpdateResult:
    """记忆编辑成功的服务结果，不重复携带正文。

    Attributes:
        scope: 本次编辑的记忆范围。
        changed: 本次编辑是否改变正文。
        length: 根据本次成功提交正文计算的额度信息。
    """

    scope: MemoryScope
    changed: bool
    length: MemoryLengthInfo


class MemoryService:
    """绑定单个 Agent 的记忆范围和额度策略。

    依赖 Application 共享的 MemoryStore 和已验证配置；从可信上下文
    绑定身份，将存储编辑失败转换为 ToolError，不缓存或截断正文。
    服务不拥有底层连接，也不负责存储关闭或自动提取记忆。
    """

    def __init__(self, store: MemoryStore, *, config: AgentMemoryConfig) -> None:
        """保存存储和已验证的配置。

        Args:
            store: Application 共享的权威存储，不转移生命周期所有权。
            config: 已验证的 Agent 记忆范围与额度配置。
        """
        self._store = store
        self._config = config

    @property
    def enabled(self) -> bool:
        """是否有启用的记忆范围。

        Returns:
            当前 Agent 是否启用至少一种记忆范围。
        """
        return bool(self._config.scopes)

    def _agent_id(self, ctx: SessionContext, scope: MemoryScope) -> str | None:
        if scope not in self._config.scopes:
            raise ToolError(code="memory_scope_disabled", message="Memory scope 未启用")
        return ctx.agent_id if scope == MemoryScope.USER_AGENT else None

    def _limit(self, scope: MemoryScope) -> int:
        return (
            self._config.max_chars.user_global
            if scope == MemoryScope.USER_GLOBAL
            else self._config.max_chars.user_agent
        )

    def _length(self, scope: MemoryScope, content: str) -> MemoryLengthInfo:
        limit = self._limit(scope)
        length = len(content)
        return MemoryLengthInfo(
            length,
            limit,
            max(limit - length, 0),
        )

    async def read(self, ctx: SessionContext, scope: MemoryScope) -> MemoryReadResult:
        """从可信上下文绑定身份，返回完整正文。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            scope: 用户记忆范围，Service 会校验是否启用。

        Returns:
            指定范围的完整正文，不存在时为空字符串。

        Raises:
            ToolError: 指定范围未启用。
        """
        content = await self._store.read(
            app_id=ctx.app_id,
            scope=scope,
            user_id=ctx.user_id,
            agent_id=self._agent_id(ctx, scope),
        )
        return MemoryReadResult(scope, content)

    async def add(
        self, ctx: SessionContext, scope: MemoryScope, new_content: str
    ) -> MemoryUpdateResult:
        """追加正文并返回本次写入对应的额度。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            scope: 用户记忆范围，Service 会校验是否启用。
            new_content: 含非空白字符的追加原文，已有正文时用单个换行分隔。

        Returns:
            本次提交是否改变正文，以及按提交结果计算的额度。

        Raises:
            ToolError: 范围未启用，或编辑因匹配和额度规则被拒绝。
        """
        result = await self._store.add(
            app_id=ctx.app_id,
            scope=scope,
            user_id=ctx.user_id,
            agent_id=self._agent_id(ctx, scope),
            new_content=new_content,
            max_chars=self._limit(scope),
        )
        return self._updated(scope, result)

    async def update(
        self,
        ctx: SessionContext,
        scope: MemoryScope,
        old_content: str,
        new_content: str,
        replace_all: bool = False,
    ) -> MemoryUpdateResult:
        """精确替换正文，空字符串表示删除。

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            scope: 用户记忆范围，Service 会校验是否启用。
            old_content: 精确匹配的非空原文，区分大小写和空白。
            new_content: 新正文片段；替换时空字符串表示删除。
            replace_all: 是否替换全部非重叠匹配；否则要求唯一匹配起点。

        Returns:
            本次提交是否改变正文，以及按提交结果计算的额度。

        Raises:
            ToolError: 范围未启用，或编辑因匹配和额度规则被拒绝。
        """
        result = await self._store.replace(
            app_id=ctx.app_id,
            scope=scope,
            user_id=ctx.user_id,
            agent_id=self._agent_id(ctx, scope),
            old_content=old_content,
            new_content=new_content,
            max_chars=self._limit(scope),
            replace_all=replace_all,
        )
        return self._updated(scope, result)

    def _updated(
        self, scope: MemoryScope, result: MemoryEditResult
    ) -> MemoryUpdateResult:
        if result.content is None:
            messages = {
                "text_not_found": "未找到 old_content，请先 memory_read 确认",
                "text_ambiguous": "old_content 匹配多处，请缩小范围或使用 replace_all",
                "capacity_exceeded": (
                    f"Memory 正文超过 {self._limit(scope)} 字符上限，"
                    "本次未写入，请精简后重试"
                ),
            }
            raise ToolError(
                code=f"memory_{result.status}", message=messages[result.status]
            )
        return MemoryUpdateResult(
            scope, result.status == "changed", self._length(scope, result.content)
        )

    async def context(self, ctx: SessionContext, scope: MemoryScope) -> str | None:
        """读取完整原文供 SYSTEM 注入

        Args:
            ctx: 可信会话上下文，提供应用、用户、Agent 和会话身份。
            scope: 用户记忆范围，Service 会校验是否启用。

        Returns:
            供 SYSTEM 注入的完整原文；范围未启用或正文全为空白时为 None。
        """
        if scope not in self._config.scopes:
            return None
        result = await self.read(ctx, scope)
        if not result.content.strip():
            return None
        return result.content
