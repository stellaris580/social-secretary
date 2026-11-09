"""tests/importing 公共夹具：固定时钟 + 选项工厂。

所有样例均为**合成数据**（非真实聊天），不联网、不访问 QQ。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from social_secretary.importing import ImportOptions

#: 固定“当前时间”（+08:00），保证相对时间/日期推断类断言与机器时区无关。
FIXED_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))

TZ8 = timezone(timedelta(hours=8))


@pytest.fixture
def fixed_now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def make_options():
    def _make(**kwargs: Any) -> ImportOptions:
        kwargs.setdefault("now", FIXED_NOW)
        kwargs.setdefault("source_name", "test_chat.txt")
        return ImportOptions(**kwargs)

    return _make


def assert_local_aware(timestamp: datetime) -> None:
    assert timestamp is not None
    assert timestamp.tzinfo is not None, "导入时间必须是 aware datetime（本地时区）"
    assert timestamp.utcoffset() == timedelta(hours=8)
