"""时间文本解析的离线测试（不依赖真机 QQ）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from social_secretary.collectors.qq.timeparse import parse_time_text
from social_secretary.contracts import TimestampSource

NOW = datetime(2026, 10, 31, 21, 30, 0, tzinfo=timezone(timedelta(hours=8)))  # 星期六


def parsed(text: str, *, now: datetime = NOW, base_day: datetime | None = None):
    return parse_time_text(text, now=now, base_day=base_day)


def test_plain_hhmm_is_today() -> None:
    result = parsed("21:05")
    assert result is not None
    assert result.timestamp == datetime(2026, 10, 31, 21, 5, tzinfo=NOW.tzinfo)
    assert result.source is TimestampSource.UI
    assert result.raw_text == "21:05"


def test_plain_hhmm_in_future_rolls_to_yesterday() -> None:
    result = parsed("23:59")
    assert result is not None
    assert result.timestamp == datetime(2026, 10, 30, 23, 59, tzinfo=NOW.tzinfo)


def test_plain_hhmm_within_future_tolerance_stays_today() -> None:
    result = parsed("21:31")
    assert result is not None
    assert result.timestamp == datetime(2026, 10, 31, 21, 31, tzinfo=NOW.tzinfo)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天 09:12", datetime(2026, 10, 31, 9, 12, tzinfo=NOW.tzinfo)),
        ("昨天 21:03", datetime(2026, 10, 30, 21, 3, tzinfo=NOW.tzinfo)),
        ("前天 08:00", datetime(2026, 10, 29, 8, 0, tzinfo=NOW.tzinfo)),
        ("星期三 10:24", datetime(2026, 10, 28, 10, 24, tzinfo=NOW.tzinfo)),
        ("周六 12:00", datetime(2026, 10, 31, 12, 0, tzinfo=NOW.tzinfo)),
        ("2026-10-25 14:30", datetime(2026, 10, 25, 14, 30, tzinfo=NOW.tzinfo)),
        ("2026年10月25日 14:30", datetime(2026, 10, 25, 14, 30, tzinfo=NOW.tzinfo)),
        ("10月30日 08:01", datetime(2026, 10, 30, 8, 1, tzinfo=NOW.tzinfo)),
        ("下午 3:20", datetime(2026, 10, 31, 15, 20, tzinfo=NOW.tzinfo)),
        ("上午 11:00", datetime(2026, 10, 31, 11, 0, tzinfo=NOW.tzinfo)),
        ("中午 12:30", datetime(2026, 10, 31, 12, 30, tzinfo=NOW.tzinfo)),
        ("晚上 7:05", datetime(2026, 10, 31, 19, 5, tzinfo=NOW.tzinfo)),
    ],
)
def test_absolute_variants(text: str, expected: datetime) -> None:
    result = parsed(text)
    assert result is not None, text
    assert result.timestamp == expected
    assert result.source is TimestampSource.UI
    assert result.date_only is False


def test_relative_times() -> None:
    just_now = parsed("刚刚")
    assert just_now is not None and just_now.source is TimestampSource.RELATIVE
    assert just_now.timestamp == NOW

    minutes = parsed("5分钟前")
    assert minutes is not None and minutes.source is TimestampSource.RELATIVE
    assert minutes.timestamp == NOW - timedelta(minutes=5)

    hours = parsed("2小时前")
    assert hours is not None and hours.timestamp == NOW - timedelta(hours=2)

    days = parsed("3天前")
    assert days is not None and days.timestamp == NOW - timedelta(days=3)


def test_date_only_divider() -> None:
    result = parsed("10月31日")
    assert result is not None
    assert result.timestamp is None
    assert result.date_only is True

    full_date = parsed("2026-10-31")
    assert full_date is not None and full_date.date_only is True


def test_short_date_without_year_rolls_back_across_year() -> None:
    january = datetime(2027, 1, 2, 10, 0, tzinfo=NOW.tzinfo)
    result = parse_time_text("12月31日 23:00", now=january)
    assert result is not None
    assert result.timestamp == datetime(2026, 12, 31, 23, 0, tzinfo=NOW.tzinfo)


def test_base_day_context_for_history_dividers() -> None:
    base_day = datetime(2026, 10, 1, 0, 0, tzinfo=NOW.tzinfo)
    result = parsed("09:15", base_day=base_day)
    assert result is not None
    assert result.timestamp == datetime(2026, 10, 1, 9, 15, tzinfo=NOW.tzinfo)


@pytest.mark.parametrize(
    "text",
    ["", "你好", "10:24 在吗", "21:5", "2026-13-45 25:00", "x" * 60, "===="],
)
def test_non_time_text_returns_none(text: str) -> None:
    assert parsed(text) is None


def test_naive_now_is_treated_as_local() -> None:
    naive = datetime(2026, 10, 31, 21, 30)
    result = parse_time_text("20:00", now=naive)
    assert result is not None
    assert result.timestamp is not None
    assert result.timestamp.tzinfo is not None
    assert (result.timestamp.hour, result.timestamp.minute) == (20, 0)
