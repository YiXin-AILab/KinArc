"""对话元数据与分页领域规则测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.common.models import SessionContext
from agentsystem.message.sessions import (
    SessionInfo,
    SessionListQuery,
    default_title,
    page_from_sessions,
    session_sort_key,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 24, tzinfo=UTC)
CTX = SessionContext(app_id="app", user_id="user", agent_id="agent", session_id="s")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "新对话"),
        ("短标题", "短标题"),
        ("中" * 16, "中" * 16),
        ("中" * 17, "中" * 16 + "..."),
        ("😀" * 17, "😀" * 16 + "..."),
    ],
)
def test_default_title(value: str, expected: str) -> None:
    assert default_title(value) == expected


def test_title_validation_and_normalization() -> None:
    info = SessionInfo(ctx=CTX, title="  标题  ", created_at=NOW, updated_at=NOW)
    assert info.title == "标题"
    for title in ("   ", "x" * 201):
        with pytest.raises(ValidationError):
            SessionInfo(ctx=CTX, title=title, created_at=NOW, updated_at=NOW)


def test_page_cursor_is_bound_to_query_scope() -> None:
    query = SessionListQuery(app_id="app", user_id="user", limit=1)
    items = tuple(
        SessionInfo(
            ctx=CTX.model_copy(update={"session_id": str(index)}),
            title="title",
            created_at=NOW,
            updated_at=NOW,
        )
        for index in range(2)
    )
    ordered = tuple(sorted(items, key=session_sort_key, reverse=True))
    page = page_from_sessions(query, ordered)
    assert page.sessions == ordered[:1]
    assert page.next_cursor is not None
    next_query = SessionListQuery(
        app_id="app", user_id="user", limit=10, cursor=page.next_cursor
    )
    assert next_query.before == session_sort_key(ordered[0])
    for changes in ({"user_id": "other"}, {"app_id": "other"}, {"agent_id": "agent"}):
        with pytest.raises(ValidationError):
            SessionListQuery(
                **(query.model_dump() | changes | {"cursor": page.next_cursor})
            )


@pytest.mark.parametrize("cursor", ["bad", "e30=", "A" * 1025])
def test_invalid_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(ValidationError):
        SessionListQuery(app_id="app", user_id="user", cursor=cursor)
