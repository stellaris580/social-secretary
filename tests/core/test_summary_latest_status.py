"""v0.2.3：摘要 scan_status 按“每个已启用账号当天最新一次 run”聚合 + 缓存自愈。"""

from __future__ import annotations

import json

import pytest

from social_secretary.contracts import (
    AccountKind,
    CollectionResult,
    ConversationKind,
    ScanStatus,
)
from social_secretary.core.storage import Storage, now_local
from social_secretary.core.summary import generate_daily_summary, get_cached_summary
from tests.core.fakes import make_account, make_conversation, make_message


def _make_storage(tmp_path, accounts) -> Storage:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    for account_id, kind in accounts:
        storage.upsert_account(make_account(account_id, kind=kind))
    return storage


def _insert_run(
    storage: Storage,
    account_id: str,
    status: ScanStatus,
    scan_id: str,
    *,
    error: str | None = None,
) -> None:
    moment = now_local()
    storage.insert_scan_run(
        CollectionResult(
            scan_id=scan_id,
            account_id=account_id,
            status=status,
            messages_seen=0,
            messages_new=0,
            conversations_scanned=1,
            error=error,
            started_at=moment,
            finished_at=moment,
        )
    )


def _today():
    return now_local().date()


def test_latest_ok_after_earlier_partial_makes_summary_ok(tmp_path) -> None:
    storage = _make_storage(tmp_path, [("acct-1", AccountKind.EMAIL)])
    _insert_run(storage, "acct-1", ScanStatus.PARTIAL, "scan-early", error="窗口不可用")
    _insert_run(storage, "acct-1", ScanStatus.OK, "scan-late")
    assert storage.scan_status_for_day(_today()) is ScanStatus.OK

    summary = generate_daily_summary(storage, day=_today())
    assert summary.scan_status is ScanStatus.OK
    joined = "；".join(summary.caveats)
    assert "今天的采集存在失败/部分失败记录" not in joined
    assert "今天早些时候有采集失败/部分失败记录" in joined
    assert "历史记录仍保留，不代表本次整理失败" in joined
    # 最新采集 OK 且今天 0 消息：保留“今天没有采集到新消息”
    assert "今天没有采集到新消息" in joined
    # 历史 run 仍保留（不删除）
    assert storage.count_scan_runs(_today()) == 2
    # 日常摘要生成不写审计/证据正文
    assert "窗口不可用" not in json.dumps(storage.list_audit(), ensure_ascii=False)


def test_latest_partial_keeps_current_failure_caveat(tmp_path) -> None:
    storage = _make_storage(tmp_path, [("acct-1", AccountKind.EMAIL)])
    _insert_run(storage, "acct-1", ScanStatus.OK, "scan-early")
    _insert_run(storage, "acct-1", ScanStatus.PARTIAL, "scan-late", error="最新失败")
    summary = generate_daily_summary(storage, day=_today())
    assert summary.scan_status is ScanStatus.PARTIAL
    joined = "；".join(summary.caveats)
    assert "今天的采集存在失败/部分失败记录" in joined
    assert "今天早些时候有采集失败/部分失败记录" not in joined
    assert "本次采集不完整" in joined  # 当天无消息 + 最新 partial


def test_enabled_account_without_latest_run_marks_partial(tmp_path) -> None:
    storage = _make_storage(
        tmp_path,
        [("acct-1", AccountKind.EMAIL), ("acct-2", AccountKind.EMAIL)],
    )
    _insert_run(storage, "acct-1", ScanStatus.OK, "scan-1")
    # acct-2 当天没有任何 run → 覆盖不完整
    assert storage.scan_status_for_day(_today()) is ScanStatus.PARTIAL
    summary = generate_daily_summary(storage, day=_today())
    assert summary.scan_status is ScanStatus.PARTIAL


def test_no_scan_runs_defaults_ok_and_keeps_existing_caveat(tmp_path) -> None:
    storage = _make_storage(tmp_path, [("acct-1", AccountKind.EMAIL)])
    assert storage.scan_status_for_day(_today()) is ScanStatus.OK
    summary = generate_daily_summary(storage, day=_today())
    assert summary.scan_status is ScanStatus.OK
    joined = "；".join(summary.caveats)
    assert "今天的采集存在失败/部分失败记录" not in joined
    assert "今天还没有采集记录" in joined


def test_get_cached_summary_refreshes_stale_partial_cache(tmp_path) -> None:
    storage = _make_storage(tmp_path, [("acct-1", AccountKind.EMAIL)])
    _insert_run(storage, "acct-1", ScanStatus.PARTIAL, "scan-early", error="早先失败")
    stale = generate_daily_summary(storage, day=_today())
    assert stale.scan_status is ScanStatus.PARTIAL

    _insert_run(storage, "acct-1", ScanStatus.OK, "scan-late")
    refreshed = get_cached_summary(storage, _today())
    assert refreshed is not None
    assert refreshed.scan_status is ScanStatus.OK
    joined = "；".join(refreshed.caveats)
    assert "今天的采集存在失败/部分失败记录" not in joined
    assert "今天早些时候有采集失败/部分失败记录" in joined
    # 缓存已刷新为 OK
    cached_payload = storage.load_summary(_today())["payload"]
    assert cached_payload["scan_status"] == "ok"
    # 历史 run 未删除
    assert storage.count_scan_runs(_today()) == 2


def test_historical_qq_failure_keeps_visible_coverage_caveat(tmp_path) -> None:
    storage = _make_storage(tmp_path, [("qq-1", AccountKind.QQ)])
    storage.upsert_conversation(
        make_conversation(
            "conv-qq", account_id="qq-1", kind=ConversationKind.PRIVATE, title="会话A"
        )
    )
    _insert_run(storage, "qq-1", ScanStatus.PARTIAL, "scan-early", error="qq_window_unavailable")
    _insert_run(storage, "qq-1", ScanStatus.OK, "scan-late")
    summary = generate_daily_summary(storage, day=_today())
    assert summary.scan_status is ScanStatus.OK
    joined = "；".join(summary.caveats)
    assert "今天早些时候有采集失败/部分失败记录" in joined
    assert "QQ 只读采集仅覆盖聊天窗口当前可见的消息" in joined
    assert "更早/未加载的历史消息仍可能缺失" in joined


def test_latest_ok_with_message_and_audit_has_no_body(tmp_path) -> None:
    storage = _make_storage(tmp_path, [("acct-1", AccountKind.EMAIL)])
    conversation = make_conversation(
        "conv-mail",
        account_id="acct-1",
        kind=ConversationKind.MAIL,
        title="INBOX",
        external_key="INBOX",
    )
    storage.upsert_conversation(conversation)
    message = make_message(
        conversation, "绝密正文标记LATEST9Q7", timestamp=now_local()
    )
    storage.insert_message(message)
    _insert_run(storage, "acct-1", ScanStatus.PARTIAL, "scan-early")
    _insert_run(storage, "acct-1", ScanStatus.OK, "scan-late")
    summary = generate_daily_summary(storage, day=_today())
    assert summary.scan_status is ScanStatus.OK
    assert summary.counts["total"] == 1
    audit = json.dumps(storage.list_audit(), ensure_ascii=False)
    assert "LATEST9Q7" not in audit
    assert "INBOX" not in audit
    # 历史失败说明不出现“当前失败”措辞
    joined = "；".join(summary.caveats)
    assert "今天的采集存在失败/部分失败记录" not in joined
