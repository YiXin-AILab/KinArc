"""会话基本信息、标题校验和存储中立的游标分页规则。"""

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from agentsystem.common.errors import AdapterError
from agentsystem.common.models import FrozenModel, SessionContext

type SessionTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
_POSITION = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}:[0-9a-f]{64}")


class SessionInfo(FrozenModel):
    """保存对话目录的不可变元数据，不包含执行状态或消息正文。

    Attributes:
        ctx: 完整应用、用户、Agent 和会话身份。
        title: 去除首尾空白后的非空标题，最多 200 字符。
        created_at: 带时区的创建时间。
        updated_at: 带时区的更新时间，不早于创建时间。
        title_version: 从 1 开始的标题版本，供条件更新使用。
        is_deleted: 删除标记，用于隐藏目录并禁止同 ID 重新写入。
    """

    ctx: SessionContext
    title: SessionTitle
    created_at: AwareDatetime
    updated_at: AwareDatetime
    title_version: int = Field(default=1, ge=1)
    is_deleted: bool = False

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        """更新时间不得早于创建时间。

        Returns:
            通过时间顺序校验的当前实例。

        Raises:
            ValueError: 更新时间早于创建时间。
        """
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        return self


class SessionListQuery(FrozenModel):
    """定义用户范围内的实时游标分页查询。

    游标绑定查询身份和筛选条件，不提供跨页一致性快照。

    Attributes:
        app_id: 应用隔离标识。
        user_id: 用户隔离标识。
        agent_id: 可选 Agent 筛选条件，None 表示该用户全部 Agent。
        limit: 一页返回条数，范围为 1 到 100。
        cursor: 上一页返回的可选游标，在构造时校验。
    """

    app_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)

    @property
    def scope(self) -> str:
        """返回绑定筛选条件的摘要；None 和空字符串不混用。

        Returns:
            绑定应用、用户和可选 Agent 的十六进制 SHA-256 摘要。
        """
        parts = (self.app_id, self.user_id)
        return (
            identity_digest(*parts, self.agent_id)
            if self.agent_id
            else identity_digest(*parts)
        )

    @property
    def before(self) -> str | None:
        """返回已验证游标的位置。

        Returns:
            倒序分页的排他上界位置；没有游标时为 None。

        Raises:
            ValueError: 游标格式、版本、日期或查询范围不合法。
        """
        if self.cursor is None:
            return None
        try:
            raw = base64.b64decode(self.cursor, altchars=b"-_", validate=True)
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"v", "scope", "position"}
                or value["v"] != 1
                or value["scope"] != self.scope
                or not isinstance(value["position"], str)
                or _POSITION.fullmatch(value["position"]) is None
            ):
                raise ValueError("invalid cursor")
            datetime.fromisoformat(value["position"][:26])
            return str(value["position"])
        except (ValueError, TypeError, binascii.Error, UnicodeError) as error:
            raise ValueError("非法会话游标或查询范围不匹配") from error

    @model_validator(mode="after")
    def validate_cursor(self) -> Self:
        """在查询进入存储前验证游标。

        Returns:
            游标通过校验的当前实例。

        Raises:
            ValueError: 游标非法或与当前查询范围不匹配。
        """
        _ = self.before
        return self


class SessionPage(FrozenModel):
    """返回一页不可变对话目录结果及继续查询位置。

    Attributes:
        sessions: 创建时间倒序的对话；时间相同按身份摘要排序。
        next_cursor: 下一页游标；没有更多记录时为 None。
    """

    sessions: tuple[SessionInfo, ...] = ()
    next_cursor: str | None = None


def identity_digest(*parts: str) -> str:
    """对长度前缀编码的完整身份求摘要，不限制原始 ID 长度。

    Args:
        parts: 按顺序参与摘要的身份分段，使用长度前缀消除拼接歧义。

    Returns:
        64 字符的 SHA-256 十六进制摘要。
    """
    digest = sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def session_identity(ctx: SessionContext) -> str:
    """返回完整四段会话身份摘要。

    Args:
        ctx: 完整会话身份，限定应用、用户、Agent 和会话的存储范围。

    Returns:
        完整四段会话身份的 SHA-256 十六进制摘要。
    """
    return identity_digest(ctx.app_id, ctx.user_id, ctx.agent_id, ctx.session_id)


def session_sort_key(info: SessionInfo) -> str:
    """UTC 固定宽度时间与身份构成确定性分页次序。

    Args:
        info: 待检查或转换的完整对话元数据。

    Returns:
        UTC 微秒时间与会话身份摘要组成的固定宽度排序键。
    """
    stamp = info.created_at.astimezone(UTC).isoformat(timespec="microseconds")[:26]
    return f"{stamp}:{session_identity(info.ctx)}"


def page_from_sessions(
    query: SessionListQuery, sessions: tuple[SessionInfo, ...]
) -> SessionPage:
    """将已经倒序读取的 limit+1 条记录变成分页结果。

    Args:
        query: 包含用户范围、可选 Agent、条数和游标的已校验查询。
        sessions: 已按倒序读取的最多 limit + 1 条对话记录。

    Returns:
        最多 limit 条记录；存在额外记录时附带绑定查询范围的游标。
    """
    items = sessions[: query.limit]
    cursor = None
    if len(sessions) > query.limit:
        value = {"v": 1, "scope": query.scope, "position": session_sort_key(items[-1])}
        cursor = base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode()
    return SessionPage(sessions=items, next_cursor=cursor)


def default_title(text: str) -> str:
    """取用户文本前 16 字，超长追加三个英文句点；空文本使用默认值。

    Args:
        text: 用户输入文本。

    Returns:
        截断后的非空标题；空白输入返回“新对话”。
    """
    value = text.strip()
    return (value[:16] + "..." if len(value) > 16 else value) or "新对话"


def require_active_session(info: SessionInfo) -> SessionInfo:
    """禁止删除后的会话 ID 被消息写入重新创建。

    Args:
        info: 待检查或转换的完整对话元数据。

    Returns:
        未删除的原实例。

    Raises:
        AdapterError: 会话已被标记删除。
    """
    if info.is_deleted:
        raise AdapterError(
            operation="session.write",
            code="session_deleted",
            message="对话已删除，请使用新的会话 ID",
        )
    return info


def renamed_session(
    previous: SessionInfo,
    *,
    title: str,
    now: datetime,
    expected_version: int | None = None,
) -> SessionInfo:
    """原子边界内应用标题更新；先校验版本，防止 ABA 覆盖。

    Args:
        previous: 原有对话元数据，调用方应在存储原子边界内读取。
        title: 待应用的标题，将按 SessionTitle 规则规范化。
        now: 调用方提供的带时区时间，用于创建或更新时间。
        expected_version: 预期标题版本；None 表示不做版本条件校验。

    Returns:
        标题改变时返回递增版本的新快照；标题相同时返回原实例。

    Raises:
        AdapterError: 指定的标题版本与原快照不一致。
        ValueError: 新标题或时间不满足 SessionInfo 校验。
    """
    if expected_version is not None and expected_version != previous.title_version:
        raise AdapterError(
            operation="session.rename",
            code="session_title_conflict",
            message="标题已修改，请重新生成",
        )
    current = SessionInfo(
        ctx=previous.ctx,
        title=title,
        created_at=previous.created_at,
        updated_at=max(previous.updated_at, now),
        title_version=previous.title_version + 1,
    )
    return previous if current.title == previous.title else current
