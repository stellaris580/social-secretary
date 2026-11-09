"""时间解析测试：绝对时间、相对时间、日期推断、无效时间。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from social_secretary.importing import WarningCode
from social_secretary.importing.parser import parse_chat_text

from .conftest import FIXED_NOW, assert_local_aware


def test_absolute_timestamp_is_local_aware(make_options) -> None:
    result = parse_chat_text("2024-01-01 12:30:00 张三: 你好", options=make_options())
    stamp = result.messages[0].raw.timestamp
    assert_local_aware(stamp)
    assert (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute) == (2024, 1, 1, 12, 30)
    assert result.messages[0].raw.raw_time_text == "2024-01-01 12:30:00"
    assert result.stats.timestamped_messages == 1


def test_relative_times_use_local_clock(make_options) -> None:
    text = "刚刚 张三: 在吗\n5分钟前 李四: 在的\n2小时前 王五: 早\n3天前 赵六: 好久不见"
    result = parse_chat_text(text, options=make_options())
    stamps = [message.raw.timestamp for message in result.messages]
    assert len(stamps) == 4
    assert all(stamp is not None and stamp.utcoffset() == timedelta(hours=8) for stamp in stamps)
    assert stamps[0] == FIXED_NOW
    assert stamps[1] == FIXED_NOW - timedelta(minutes=5)
    assert stamps[2] == FIXED_NOW - timedelta(hours=2)
    assert stamps[3] == FIXED_NOW - timedelta(days=3)
    assert all(message.raw.timestamp_source.value == "relative" for message in result.messages)


def test_yesterday_and_weekday(make_options) -> None:
    text = "昨天 09:00 张三: 昨天的消息\n星期天 10:00 李四: 周日消息"
    result = parse_chat_text(text, options=make_options())
    first, second = result.messages
    assert first.raw.timestamp == datetime(2026, 10, 31, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    # 2026-11-01 是周日，同一天
    assert second.raw.timestamp.date() == FIXED_NOW.date()
    assert second.raw.timestamp.hour == 10
    assert first.raw.timestamp_source.value == "relative"


def test_month_day_year_rollover(make_options) -> None:
    # 当前 2026-11-01，“12月31日”应视为未来 → 回退到上一年 2025-12-31
    result = parse_chat_text("12月31日 23:00 张三: 跨年", options=make_options())
    stamp = result.messages[0].raw.timestamp
    assert stamp is not None
    assert stamp.year == 2025
    assert (stamp.month, stamp.day) == (12, 31)


def test_invalid_timestamp_becomes_untimed(make_options) -> None:
    result = parse_chat_text("2024-13-45 25:99 张三: 坏时间", options=make_options())
    # 该行无法被识别为时间头 → 走 sender: text 兜底（发送者包含时间文本），
    # 不应崩溃；若解析为消息，则时间必须为 None/COLLECTED 或有明确 warning。
    assert result.warning_codes
    for message in result.messages:
        if message.raw.timestamp is None:
            assert message.raw.timestamp_source.value == "collected"


def test_date_only_header_then_clock(make_options) -> None:
    text = "2024-01-01\n12:30 张三: 早"
    result = parse_chat_text(text, options=make_options())
    assert result.messages[0].raw.timestamp.date().isoformat() == "2024-01-01"
    assert result.messages[0].raw.timestamp.hour == 12


def test_clock_only_without_date_context_uses_recent_date(make_options) -> None:
    result = parse_chat_text("09:00 张三: 很早", options=make_options())
    stamp = result.messages[0].raw.timestamp
    assert stamp is not None
    assert stamp.date() == FIXED_NOW.date()
    assert WarningCode.TIMESTAMP_ASSUMED_DATE in result.warning_codes


def test_future_clock_only_rolls_back_one_day(make_options) -> None:
    # 当前 12:00，消息显示 20:00 → 更可能是昨天
    result = parse_chat_text("20:00 张三: 晚上", options=make_options())
    stamp = result.messages[0].raw.timestamp
    assert stamp is not None
    assert stamp.date() == (FIXED_NOW - timedelta(days=1)).date()
    assert stamp.hour == 20


def test_iso8601_with_offset(make_options) -> None:
    result = parse_chat_text("2024-01-01T12:30:00+08:00 张三: 你好", options=make_options())
    stamp = result.messages[0].raw.timestamp
    assert stamp is not None
    assert stamp.utcoffset() == timedelta(hours=8)
    assert stamp.hour == 12


def test_noon_period_words(make_options) -> None:
    result = parse_chat_text(
        "2024-01-01 下午 1:05 张三: 下午好\n2024-01-01 晚上 9:30 李四: 晚上好",
        options=make_options(),
    )
    assert result.messages[0].raw.timestamp.hour == 13
    assert result.messages[1].raw.timestamp.hour == 21
