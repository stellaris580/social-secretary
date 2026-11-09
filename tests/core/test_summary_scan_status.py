"""摘要 caveat 修正：采集不完整时不得说“今天没有采集到新消息”。"""

from __future__ import annotations

import pytest

from social_secretary.contracts import (
    AccountKind,
    CollectionResult,
    ScanStatus,
)
from social_secretary.core.storage import Storage, now_local
from social_secretary.core.summary import generate_daily_summary
from tests.core.fakes import make_account, make_conversation


def _make_storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-scan", account_id=account.id, title="INBOX", external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    return storage


def _insert_scan_run(storage: Storage, status: ScanStatus, *, error: str | None = None) -> None:
    now = now_local()
    result = CollectionResult(
        scan_id=f"scan-{status.value}-{now.timestamp()}",
        account_id=storage.list_accounts()[0].id,
        status=status,
        messages_seen=0,
        messages_new=0,
        conversations_scanned=0,
        error=error,
        warnings=(),
        started_at=now,
        finished_at=now,
    )
    storage.insert_scan_run(result, trigger="one_click")


def _today():
    return now_local().date()


@pytest.mark.parametrize("status", [ScanStatus.PARTIAL, ScanStatus.FAILED])
def test_partial_failed_no_messages_does_not_claim_no_new_messages(tmp_path, status) -> None:
    storage = _make_storage(tmp_path)
    _insert_scan_run(storage, status, error="qq_window_unavailable")
    summary = generate_daily_summary(storage, day=_today())
    joined = "；".join(summary.caveats)
    assert "今天没有采集到新消息" not in joined
    assert "本次采集不完整，无法确认今天是否有新消息；已保存的历史消息不会丢失。" in joined
    assert summary.counts["total"] == 0
    assert summary.scan_status is status


def test_ok_no_messages_keeps_no_new_message_caveat(tmp_path) -> None:
    storage = _make_storage(tmp_path)
    _insert_scan_run(storage, ScanStatus.OK)
    summary = generate_daily_summary(storage, day=_today())
    joined = "；".join(summary.caveats)
    assert "今天没有采集到新消息。" in joined
    assert "本次采集不完整" not in joined
    assert summary.scan_status is ScanStatus.OK


def test_blocked_no_messages_says_unknown_not_no_new(tmp_path) -> None:
    storage = _make_storage(tmp_path)
    _insert_scan_run(storage, ScanStatus.BLOCKED, error="未授权")
    summary = generate_daily_summary(storage, day=_today())
    joined = "；".join(summary.caveats)
    assert "今天没有采集到新消息" not in joined
    assert "无法确认今天是否有新消息" in joined
