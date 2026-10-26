"""与存储技术无关的异步 MessageManager 领域端口。"""

from datetime import datetime
from typing import Protocol

from agentsystem.common.models import SessionContext
from agentsystem.message.models import ContextMessage, Message
from agentsystem.message.sessions import SessionInfo, SessionListQuery, SessionPage


class MessageManager(Protocol):
    """定义消息内容与对话目录的权威存储协议。

    实现负责批次幂等、压缩指针条件提交及一致性读取，不负责 run 调度。
    存储连接由应用管理；调用方通过完整 SessionContext 隔离消息。
    """

    async def delete_session(
        self, ctx: SessionContext, *, hard_delete: bool = False
    ) -> None:
        """幂等隐藏对话；硬删除同时清空历史，保留禁止复活的删除标记。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            hard_delete: 是否同时清空消息历史；两种删除均保留目录删除标记。

        Raises:
            AdapterError: 删除操作失败或已有目录数据非法。
        """
        ...

    async def ensure_session(
        self, ctx: SessionContext, *, title: str, now: datetime
    ) -> SessionInfo:
        """幂等创建对话信息；已存在时不覆盖标题。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            title: 候选标题；创建已有会话时不覆盖其标题。
            now: 调用方提供的带时区时间，用于创建或更新时间。

        Returns:
            已有的活动目录记录或本次创建的记录；已有标题保持不变。

        Raises:
            AdapterError: 会话已删除或目录存储数据非法。
            ValueError: 标题或时间不满足目录模型约束。
        """
        ...

    async def get_session(self, ctx: SessionContext) -> SessionInfo | None:
        """读取对话信息，不存在时返回 None。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            活动目录记录；不存在或已删除时为 None。

        Raises:
            AdapterError: 存储记录格式或完整会话身份校验失败。
        """
        ...

    async def list_sessions(self, query: SessionListQuery) -> SessionPage:
        """按用户和可选 Agent 返回创建时间倒序的一页对话。

        Args:
            query: 包含用户范围、可选 Agent、条数和游标的已校验查询。

        Returns:
            创建时间倒序的一页对话及可选下一页游标。

        Raises:
            AdapterError: 目录记录或索引与查询范围不匹配。
        """
        ...

    async def update_session_title(
        self,
        ctx: SessionContext,
        *,
        title: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> SessionInfo | None:
        """修改标题；可选版本校验失败时抛出 AdapterError。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            title: 要设置的标题，将按 SessionTitle 规则规范化。
            now: 调用方提供的带时区时间，用于创建或更新时间。
            expected_version: 预期标题版本；None 表示不做版本条件校验。

        Returns:
            改名后的目录记录；目标不存在或已删除时为 None。

        Raises:
            AdapterError: 预期标题版本冲突，或存储数据非法。
            ValueError: 标题非法或 now 不包含时区。
        """
        ...

    async def commit(
        self,
        ctx: SessionContext,
        messages: tuple[Message, ...],
    ) -> None:
        """按输入顺序原子、幂等提交一批定稿 Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            messages: 按期望追加顺序排列的定稿消息；空批次不写入。

        Raises:
            ValueError: 消息 session_id 与 ctx 不一致。
            AdapterError: 同一消息 ID 内容冲突，或目标会话已删除。
        """
        ...

    async def commit_compaction(
        self,
        ctx: SessionContext,
        message: Message,
        *,
        expected_latest_compaction_id: str | None,
    ) -> None:
        """按预期上一版 ID 原子提交新的 Compaction Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            message: 仅包含一个 CompactionBlock 的定稿消息。
            expected_latest_compaction_id: 预期上一版压缩消息 ID；None 表示尚无压缩。

        Raises:
            ValueError: 消息会话不匹配或不是压缩消息。
            AdapterError: 预期压缩指针冲突、同 ID 内容冲突或会话已删除。
        """
        ...

    async def get(self, ctx: SessionContext, message_id: str) -> Message | None:
        """按 ID 获取一条 Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。
            message_id: 目标会话内的消息标识。

        Returns:
            已解码的目标消息；不存在时为 None。

        Raises:
            AdapterError: 消息封套或存储内容校验失败。
        """
        ...

    async def list_messages(
        self,
        ctx: SessionContext,
    ) -> tuple[Message, ...]:
        """按稳定存储顺序列出全部 Message。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            按首次提交顺序排列的全部已存消息；空会话返回空元组。

        Raises:
            AdapterError: 快照中的消息无法通过存储校验。
        """
        ...

    async def read_context_source(self, ctx: SessionContext) -> ContextMessage:
        """返回一次内部一致性读取的上下文源。

        Args:
            ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

        Returns:
            同一次存储读取组装的模型上下文源。

        Raises:
            AdapterError: 消息存储数据非法，或基础 SYSTEM 消息不是恰好一条。
        """
        ...
