"""记忆存储协议及共用的身份校验、文本编辑规则。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from agentsystem.common.models import validate_identifier

MAX_CONTENT_CHARS = 1_000_000


class MemoryScope(StrEnum):
    """记忆归属范围，不参与 session 或 run 生命周期。

    Attributes:
        USER_GLOBAL: 同一应用内当前用户跨 Agent 共享的正文。
        USER_AGENT: 同一应用内当前用户与指定 Agent 独享的正文。
    """

    USER_GLOBAL = "user_global"
    USER_AGENT = "user_agent"


@dataclass(frozen=True, slots=True)
class MemoryEditResult:
    """一次原子文本编辑的结果，供 Service 计算额度或映射工具错误。

    Attributes:
        status: 修改、无变化、匹配失败、匹配歧义或超额的结果分类。
        content: 成功时为本次操作对应的完整正文；失败时为 None。
    """

    status: Literal[
        "changed", "unchanged", "text_not_found", "text_ambiguous", "capacity_exceeded"
    ]
    content: str | None


class MemoryStore(Protocol):
    """跨会话用户记忆的唯一权威存储协议。

    实现按应用、用户和范围隔离正文，将读改写作为单键原子操作；
    额度由调用方传入，存储不负责 Agent 配置、工具输出或 SYSTEM 注入。
    连接和实例生命周期由 Application 管理，不要求跨范围事务快照。
    """

    async def read(
        self, *, app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
    ) -> str:
        """读取完整正文，不存在返回空字符串。

        Args:
            app_id: 应用命名空间。
            scope: 用户记忆范围，Service 会校验是否启用。
            user_id: 当前用户身份。
            agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。

        Returns:
            完整记忆原文，记录不存在时为空字符串。
        """
        ...

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
        """原子创建或追加完整正文。

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
        ...

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
        """原子替换匹配内容，空新正文表示删除。

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
        ...


def validate_identity(
    app_id: str, scope: MemoryScope, user_id: str, agent_id: str | None
) -> None:
    """拒绝空身份、非法 Unicode 和不一致的 scope。

    Args:
        app_id: 应用命名空间。
        scope: 用户记忆范围，Service 会校验是否启用。
        user_id: 当前用户身份。
        agent_id: 用户与 Agent 范围的 Agent ID；全局记忆必须为 None。

    Raises:
        ValueError: 身份为空、编码不合法或不满足定位规则。
    """
    for value in (app_id, user_id):
        if not value:
            raise ValueError("Memory identity must not be empty")
        validate_identifier(value)
        value.encode("utf-8")
    if scope == MemoryScope.USER_GLOBAL and agent_id is None:
        return
    if scope == MemoryScope.USER_AGENT and agent_id:
        validate_identifier(agent_id)
        agent_id.encode("utf-8")
        return
    raise ValueError("Invalid memory scope or agent identity")


def append_content(
    content: str,
    new_content: str,
    *,
    max_chars: int = MAX_CONTENT_CHARS,
) -> MemoryEditResult:
    """保留原文，以单个换行追加并检查硬上限。

    Args:
        content: 编辑前的完整原文。
        new_content: 含非空白字符的追加原文，已有正文时用单个换行分隔。
        max_chars: 最终完整正文的字符上限，按 Python len 计数。

    Returns:
        编辑结果；成功含完整正文，匹配或额度失败时正文为 None。

    Raises:
        ValueError: 追加内容为空白、编码无效，或所检查的额度无效。
    """
    if len(new_content) > max_chars:
        return MemoryEditResult("capacity_exceeded", None)
    new_content.encode("utf-8")
    if not new_content.strip():
        raise ValueError("new_content must contain non-whitespace text")
    if len(content) + bool(content) + len(new_content) > max_chars:
        return MemoryEditResult("capacity_exceeded", None)
    candidate = content + "\n" + new_content if content else new_content
    return _result(content, candidate, max_chars)


def replace_content(
    content: str,
    old_content: str,
    new_content: str,
    *,
    replace_all: bool = False,
    max_chars: int = MAX_CONTENT_CHARS,
) -> MemoryEditResult:
    """精确匹配后一次性替换，不递归匹配生成内容。

    Args:
        content: 编辑前的完整原文。
        old_content: 精确匹配的非空原文，区分大小写和空白。
        new_content: 新正文片段；替换时空字符串表示删除。
        replace_all: 是否替换全部非重叠匹配；否则要求唯一匹配起点。
        max_chars: 最终完整正文的字符上限，按 Python len 计数。

    Returns:
        编辑结果；成功含完整正文，匹配或额度失败时正文为 None。

    Raises:
        ValueError: 原文为空、文本编码无效、replace_all 非布尔值或额度无效。
    """
    if len(new_content) > max_chars:
        return MemoryEditResult("capacity_exceeded", None)
    old_content.encode("utf-8")
    new_content.encode("utf-8")
    if not old_content or type(replace_all) is not bool:
        raise ValueError("old_content must not be empty and replace_all must be bool")
    first = content.find(old_content)
    if first < 0:
        return MemoryEditResult("text_not_found", None)
    if not replace_all and content.find(old_content, first + 1) >= 0:
        return MemoryEditResult("text_ambiguous", None)
    count = content.count(old_content) if replace_all else 1
    if len(content) + count * (len(new_content) - len(old_content)) > max_chars:
        return MemoryEditResult("capacity_exceeded", None)
    return _result(
        content,
        content.replace(old_content, new_content, -1 if replace_all else 1),
        max_chars,
    )


def _result(content: str, candidate: str, max_chars: int) -> MemoryEditResult:
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_CONTENT_CHARS:
        raise ValueError("Invalid memory character limit")
    if len(candidate) > max_chars:
        return MemoryEditResult("capacity_exceeded", None)
    return MemoryEditResult(
        "unchanged" if content == candidate else "changed", candidate
    )
