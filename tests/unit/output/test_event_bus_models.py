"""公开 Stream cursor 与 record 模型测试。"""

from uuid import UUID

import pytest
from pydantic import ValidationError


def test_public_cursor_round_trips_epoch_and_redis_id() -> None:
    """Cursor 必须稳定携带 Stream epoch 与 Redis Stream ID。"""
    from agentsystem.outbound.stream_models import PublicEventCursor

    value = "12345678-1234-5678-1234-567812345678:1722768123456-7"

    cursor = PublicEventCursor.parse(value)

    assert cursor.epoch == UUID("12345678-1234-5678-1234-567812345678")
    assert cursor.stream_id == "1722768123456-7"
    assert cursor.render() == value


@pytest.mark.parametrize(
    "value",
    ["", "bad", "not-a-uuid:1-0", "12345678-1234-5678-1234-567812345678:-1-0"],
)
def test_public_cursor_rejects_invalid_values(value: str) -> None:
    """非法 cursor 不得被容错成其他读取位置。"""
    from agentsystem.outbound.stream_models import PublicEventCursor

    with pytest.raises((ValueError, ValidationError)):
        PublicEventCursor.parse(value)
