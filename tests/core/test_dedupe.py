"""去重测试：哈希公式、时间桶、同批次流式去重、跨批次幂等。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from social_secretary.contracts import TimestampSource
from social_secretary.core.dedupe import (
    UNKNOWN_TIMESTAMP_BUCKET,
    MessageDeduplicator,
    compute_content_hash,
    content_hash_for,
    timestamp_bucket,
    with_occurrence,
)
from social_secretary.core.storage import Storage, now_local
from tests.core.fakes import (
    make_account,
    make_conversation,
    make_message,
    make_raw,
)

TZ = timezone(timedelta(hours=8))
TS = datetime(2026, 10, 31, 9, 5, 30, tzinfo=TZ)


def _hash(text: str = "你好", conversation_id: str = "conv-1", **kwargs) -> str:
    params = {
        "sender": "张三",
        "is_self": False,
        "timestamp": TS,
        "timestamp_source": TimestampSource.UI,
    }
    params.update(kwargs)
    return compute_content_hash(conversation_id, params.pop("sender"), params.pop("is_self"), text, **params)


def test_content_hash_formula_is_deterministic() -> None:
    first = _hash()
    assert first == _hash()
    assert first != _hash(text="你好呀")
    assert first != _hash(conversation_id="conv-2")
    assert first != _hash(sender="李四")
    assert first != _hash(is_self=True)
    assert len(first) == 64  # sha256 hex


def test_timestamp_bucket_minute_precision() -> None:
    assert timestamp_bucket(TS, TimestampSource.UI) == "2026-10-31T09:05"
    # 同分钟内秒数不同 → 相同时间桶 → 相同哈希（同一消息重复采集）
    same_minute = TS + timedelta(seconds=25)
    assert _hash() == _hash(timestamp=same_minute)
    # 跨分钟 → 视为不同消息
    next_minute = TS + timedelta(minutes=1)
    assert _hash() != _hash(timestamp=next_minute)
    # 无时间 / 采集时间统一为未知桶
    assert timestamp_bucket(None, TimestampSource.COLLECTED) == UNKNOWN_TIMESTAMP_BUCKET
    assert _hash(timestamp=None, timestamp_source=TimestampSource.COLLECTED) == _hash(
        timestamp=now_local(), timestamp_source=TimestampSource.COLLECTED
    )
    # 有 UI 时间与无时间必须区分
    assert _hash() != _hash(timestamp=None)


def test_occurrence_suffix_is_stable() -> None:
    base = _hash()
    assert with_occurrence(base, 1) == base
    assert with_occurrence(base, 2) == with_occurrence(base, 2)
    assert with_occurrence(base, 2) != base


def test_batch_dedup_by_locator_and_external_id() -> None:
    deduper = MessageDeduplicator()
    raw = make_raw("ext-conv-1", "同一气泡", source_locator="row-3")
    first = deduper.decide("conv-1", raw)
    assert first.duplicate_in_batch is False
    second = deduper.decide("conv-1", raw)
    assert second.duplicate_in_batch is True

    deduper.reset_batch()
    mail_raw = make_raw("ext-conv-1", "邮件消息", external_id="uid-1")
    assert deduper.decide("conv-1", mail_raw).duplicate_in_batch is False
    assert deduper.decide("conv-1", mail_raw).duplicate_in_batch is True

    deduper.reset_batch()
    # 同文案、不同位置 → 两条真实消息都要保留（哈希追加出现序号）
    first_real = deduper.decide("conv-1", make_raw("ext", "好的", source_locator="row-1"))
    second_real = deduper.decide("conv-1", make_raw("ext", "好的", source_locator="row-2"))
    assert first_real.duplicate_in_batch is False
    assert second_real.duplicate_in_batch is False
    assert first_real.content_hash != second_real.content_hash


def test_cross_batch_idempotent_via_storage(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account()
    conversation = make_conversation()
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)

    message = make_message(conversation, "重复扫描不应重复入库", timestamp=TS)
    assert storage.insert_message(message) is True
    # 新 batch：同内容、同时间桶（只差秒）→ 同一 content_hash → 跳过
    same_message = make_message(
        conversation, "重复扫描不应重复入库", timestamp=TS + timedelta(seconds=20)
    )
    assert same_message.content_hash == message.content_hash
    assert storage.insert_message(same_message) is False
    assert storage.count_messages() == 1

    # 不同时间桶 → 新消息
    later = make_message(
        conversation, "重复扫描不应重复入库", timestamp=TS + timedelta(minutes=2)
    )
    assert storage.insert_message(later) is True
    assert storage.count_messages() == 2


def test_content_hash_for_matches_raw_message() -> None:
    raw = make_raw("ext", "你好", timestamp=TS)
    expected = compute_content_hash(
        "conv-1", raw.sender, raw.is_self, raw.text, raw.timestamp, raw.timestamp_source
    )
    assert content_hash_for("conv-1", raw) == expected
