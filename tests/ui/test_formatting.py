"""格式化工具测试（不创建窗口）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from social_secretary.contracts import Category, ScanStatus, TimestampSource
from social_secretary.ui.formatting import (
    account_kind_label,
    category_label,
    format_bytes,
    format_date,
    format_datetime,
    format_due,
    scan_status_label,
    sender_label,
    shorten,
    timestamp_source_label,
    todo_status_label,
)

CN_TZ = timezone(timedelta(hours=8))


def test_format_datetime_relative_days():
    now = datetime(2026, 10, 31, 18, 30, tzinfo=CN_TZ)
    assert format_datetime(now - timedelta(hours=1), now=now) == "今天 17:30"
    assert format_datetime(now - timedelta(days=1), now=now).startswith("昨天")
    assert format_datetime(now - timedelta(days=2), now=now).startswith("前天")
    assert format_datetime(datetime(2026, 10, 20, 9, 5, tzinfo=CN_TZ), now=now) == "10月20日 09:05"
    assert format_datetime(datetime(2025, 12, 1, 9, 0, tzinfo=CN_TZ), now=now) == "2025年12月1日 09:00"
    assert format_datetime(None) == "时间未知"


def test_format_date_and_due():
    now = datetime(2026, 10, 31, 10, 0, tzinfo=CN_TZ)
    assert "今天" in format_date(now.date(), now=now.date())
    assert format_due(None, None) == "未识别到截止时间"
    assert "原话：明天下午六点前" in format_due(None, "明天下午六点前")
    assert "原话" in format_due(now + timedelta(days=1), "明天下午六点前", now=now)


def test_format_bytes_and_shorten():
    assert format_bytes(0) == "0 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(3 * 1024 * 1024) == "3.0 MB"
    assert format_bytes(None) == "0 B"
    assert shorten("一二三四五六七八九十", limit=5) == "一二三四…"
    assert shorten("短文本", limit=10) == "短文本"


def test_chinese_labels():
    assert category_label(Category.IMPORTANT) == "重要"
    assert category_label(Category.NORMAL) == "普通"
    assert category_label(Category.FYI) == "仅供了解"
    assert "正常" in scan_status_label(ScanStatus.OK)
    assert "部分" in scan_status_label(ScanStatus.PARTIAL)
    assert "时间" in timestamp_source_label(TimestampSource.COLLECTED)
    assert account_kind_label  # 可调用
    assert todo_status_label  # 可调用
    assert sender_label("小张", False) == "小张"
    assert sender_label("我", True) == "我"
