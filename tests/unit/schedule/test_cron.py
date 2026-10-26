"""Schedule Cron 匹配的单元测试。"""

from datetime import UTC, datetime

import pytest
from agentsystem.schedule.scheduler import CronSchedule, scheduled_occurrence_key


def test_matches_current_minute_in_asia_shanghai() -> None:
    """系统时区中的当前分钟应匹配五段 Cron。"""
    schedule = CronSchedule("Asia/Shanghai")
    now = datetime(2026, 8, 18, 1, 0, 37, tzinfo=UTC)

    assert schedule.matches("0 9 * * *", now) is True
    assert schedule.scheduled_for(now) == datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    assert scheduled_occurrence_key(schedule.scheduled_for(now)) == (
        "scheduled:2026-08-18T01:00Z"
    )


def test_day_of_month_and_week_use_unix_or_semantics() -> None:
    """day-of-month 与 day-of-week 同时受限时应使用 OR 语义。"""
    schedule = CronSchedule("Asia/Shanghai")

    assert schedule.matches(
        "0 9 18 * 1",
        datetime(2026, 8, 18, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "expression",
    (
        "0 9 * *",
        "0 0 9 * * *",
        "0 0 9 * * * 2026",
        "@daily",
        "0 9 ? * *",
        "0 9 L * *",
        "0 9 1W * *",
        "0 9 * * 1#2",
        "60 9 * * *",
    ),
)
def test_rejects_non_v1_cron_syntax(expression: str) -> None:
    """V1 应拒绝非标准五段语法。"""
    with pytest.raises(ValueError, match="五段 Cron"):
        CronSchedule("Asia/Shanghai").validate(expression)


@pytest.mark.parametrize(
    "expression",
    (
        "*/15 * * * *",
        "0 9,18 * * *",
        "0 9 * 1-6 *",
    ),
)
def test_accepts_standard_ranges_lists_and_steps(expression: str) -> None:
    """标准 Cron 的步长、列表和范围应可用。"""
    CronSchedule("Asia/Shanghai").validate(expression)


def test_requires_timezone_aware_datetime() -> None:
    """匹配输入必须是含时区的 datetime。"""
    with pytest.raises(ValueError, match="时区"):
        CronSchedule("Asia/Shanghai").matches(
            "0 9 * * *",
            datetime(2026, 8, 18, 9),
        )


def test_dst_skipped_minute_never_matches() -> None:
    """DST 跳过的本地分钟不应命中。"""
    schedule = CronSchedule("America/New_York")

    assert (
        schedule.matches(
            "30 2 * * *",
            datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        )
        is False
    )


def test_dst_repeated_minute_has_distinct_utc_occurrences() -> None:
    """DST 重复的本地分钟可以产生两个 UTC occurrence。"""
    schedule = CronSchedule("America/New_York")
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)

    assert schedule.matches("30 1 * * *", first) is True
    assert schedule.matches("30 1 * * *", second) is True
    assert scheduled_occurrence_key(schedule.scheduled_for(first)) != (
        scheduled_occurrence_key(schedule.scheduled_for(second))
    )
