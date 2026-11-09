"""tests/intel 公共夹具：固定时间、消息工厂。

固定时间：2026-10-28 10:00 +08:00（星期三），便于断言相对日期。

注：tmp_path 的 Windows 沙箱兼容覆盖已在根 ``tests/conftest.py`` 统一处理，本目录不再重复。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from social_secretary.contracts import (
    ConversationKind,
    Message,
    RawMessage,
    TimestampSource,
)

LOCAL_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 10, 28, 10, 0, tzinfo=LOCAL_TZ)  # 周三


@pytest.fixture
def now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def make_raw():
    def _make(
        text: str,
        *,
        sender: str = "小明",
        is_self: bool = False,
        conversation_key: str = "qq:10001",
    ) -> RawMessage:
        return RawMessage(
            conversation_key=conversation_key,
            sender=sender,
            is_self=is_self,
            text=text,
            timestamp=FIXED_NOW,
            timestamp_source=TimestampSource.UI,
        )

    return _make


@pytest.fixture
def make_message():
    def _make(
        text: str,
        *,
        sender: str = "小明",
        is_self: bool = False,
        message_id: str = "msg-1",
        conversation_title: str = "小明",
        kind: ConversationKind = ConversationKind.PRIVATE,
    ) -> Message:
        return Message(
            id=message_id,
            account_id="acc-qq",
            conversation_id="conv-1",
            conversation_title=conversation_title,
            conversation_kind=kind,
            sender=sender,
            is_self=is_self,
            text=text,
            timestamp=FIXED_NOW,
            timestamp_source=TimestampSource.UI,
            collected_at=FIXED_NOW,
        )

    return _make
