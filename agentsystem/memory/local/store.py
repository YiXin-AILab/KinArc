"""Application 内共享、无 await 临界区的内存存储。"""

from agentsystem.memory.store import (
    MemoryEditResult,
    MemoryScope,
    append_content,
    replace_content,
    validate_identity,
)


class LocalMemoryStore:
    """在实例内保存完整记忆正文的 MemoryStore 实现。

    读改写临界区不包含 await，因此同一事件循环中的操作不会交错。
    实例由 Application 共享；销毁或进程退出后数据丢失，不提供跨进程
    协调、磁盘持久化或跨线程同步。
    """

    def __init__(self) -> None:
        """初始化唯一正文映射。"""
        self._contents: dict[tuple[str, MemoryScope, str, str | None], str] = {}

    async def read(
        self, *, app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
    ) -> str:
        """读取完整正文。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。

        Returns:
            完整记忆原文，记录不存在时为空字符串。
        """
        validate_identity(app_id, scope, user_id, agent_id)
        return self._contents.get((app_id, scope, user_id, agent_id), "")

    async def add(
        self,
        *,
        app_id: str,
        scope: MemoryScope,
        user_id: str,
        agent_id: str | None,
        new_content: str,
        max_chars: int,
    ) -> MemoryEditResult:
        """无调度切换地追加正文。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。
            new_content: 含非空白字符的追加原文，已有正文时用单个换行分隔。
            max_chars: 最终完整正文的字符上限，按 Python len 计数。

        Returns:
            编辑结果；成功含完整正文，匹配或额度失败时正文为 None。
        """
        validate_identity(app_id, scope, user_id, agent_id)
        key = (app_id, scope, user_id, agent_id)
        result = append_content(
            self._contents.get(key, ""), new_content, max_chars=max_chars
        )
        if result.content is not None:
            self._contents[key] = result.content
        return result

    async def replace(
        self,
        *,
        app_id: str,
        scope: MemoryScope,
        user_id: str,
        agent_id: str | None,
        old_content: str,
        new_content: str,
        max_chars: int,
        replace_all: bool = False,
    ) -> MemoryEditResult:
        """无调度切换地替换正文。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。
            old_content: 精确匹配的非空原文，区分大小写和空白。
            new_content: 新正文片段；替换时空字符串表示删除。
            max_chars: 最终完整正文的字符上限，按 Python len 计数。
            replace_all: 是否替换全部非重叠匹配；否则要求唯一匹配起点。

        Returns:
            编辑结果；成功含完整正文，匹配或额度失败时正文为 None。
        """
        validate_identity(app_id, scope, user_id, agent_id)
        key = (app_id, scope, user_id, agent_id)
        result = replace_content(
            self._contents.get(key, ""),
            old_content,
            new_content,
            replace_all=replace_all,
            max_chars=max_chars,
        )
        if result.content is not None:
            self._contents[key] = result.content
        return result
