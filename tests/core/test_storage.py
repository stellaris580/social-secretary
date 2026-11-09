"""存储层测试：schema/迁移、CRUD、级联删除、时间/时区、审计脱敏。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from social_secretary.contracts import (
    Category,
    CollectionResult,
    ConversationKind,
    DraftKind,
    ReplyDraft,
    ScanStatus,
    TimestampSource,
    TodoItem,
    TodoStatus,
)
from social_secretary.core.storage import (
    SCHEMA_VERSION,
    Storage,
    from_iso,
    local_day_of,
    now_local,
    to_iso,
)
from tests.core.fakes import (
    make_account,
    make_conversation,
    make_message,
)


@pytest.fixture()
def store(tmp_path) -> Storage:
    storage = Storage(tmp_path / "db" / "secretary.db")
    storage.initialize()
    return storage


def _seed_account_conversation(store: Storage):
    account = make_account()
    conversation = make_conversation()
    store.upsert_account(account)
    store.upsert_conversation(conversation)
    return account, conversation


# ---------------------------------------------------------------------------
# schema / 迁移
# ---------------------------------------------------------------------------

def test_initialize_schema_wal_and_idempotent(store: Storage) -> None:
    assert store.schema_version == SCHEMA_VERSION
    assert store.journal_mode.lower() == "wal"
    # 重复初始化不应报错/不应重复迁移
    store.initialize()
    assert store.schema_version == SCHEMA_VERSION
    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    for expected in (
        "accounts",
        "conversations",
        "messages",
        "scan_runs",
        "todos",
        "drafts",
        "settings",
        "consent_log",
        "audit_log",
        "summaries",
        "schema_meta",
    ):
        assert expected in tables


def test_migration_from_legacy_zero_version(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (0, '2026-01-01T00:00:00+08:00')"
        )
    storage = Storage(db_path)
    storage.initialize()
    assert storage.schema_version == SCHEMA_VERSION
    account = make_account()
    storage.upsert_account(account)
    assert storage.get_account(account.id) == account


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def test_iso_helpers_keep_timezone() -> None:
    value = datetime(2026, 10, 31, 9, 5, tzinfo=timezone(timedelta(hours=8)))
    text = to_iso(value)
    assert text is not None and text.endswith("+08:00")
    assert from_iso(text) == value
    assert local_day_of(value) == "2026-10-31"
    assert to_iso(None) is None and from_iso(None) is None


# ---------------------------------------------------------------------------
# 账号 / 会话
# ---------------------------------------------------------------------------

def test_account_crud(store: Storage) -> None:
    account = make_account()
    store.upsert_account(account)
    assert store.get_account(account.id) == account
    assert store.count_accounts() == 1
    assert store.list_accounts()[0].id == account.id

    # 更新显示名（upsert 保留 id）
    from dataclasses import replace

    renamed = replace(account, display_name="新名字")
    store.upsert_account(renamed)
    assert store.get_account(account.id).display_name == "新名字"

    assert store.set_account_enabled(account.id, False) is True
    assert store.get_account(account.id).enabled is False
    assert store.list_accounts(include_disabled=False) == []
    assert store.set_account_enabled("missing", False) is False


def test_delete_account_soft_and_hard(store: Storage) -> None:
    account, conversation = _seed_account_conversation(store)
    message = make_message(conversation, "保留的消息")
    assert store.insert_message(message) is True

    # 软删除：账号隐藏，消息保留
    assert store.delete_account(account.id, delete_messages=False) is True
    assert store.get_account(account.id) is None
    assert store.get_message(message.id) is not None
    assert store.list_conversations(account.id) == []

    # 再硬删除（软删除的账号仍在库中，可被彻底清理）
    assert store.delete_account(account.id, delete_messages=True) is True
    assert store.get_message(message.id) is None
    assert store.delete_account(account.id, delete_messages=True) is False


def test_delete_account_cascade(store: Storage) -> None:
    account, conversation = _seed_account_conversation(store)
    message = make_message(conversation, "会被级联删除")
    store.insert_message(message)
    todo = TodoItem(
        id="todo-1",
        message_id=message.id,
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        text="待办",
        created_at=now_local(),
    )
    store.insert_todo(todo)
    draft = ReplyDraft(
        id="draft-1",
        message_id=message.id,
        kind=DraftKind.LOCAL_RULES,
        style="简洁",
        text="草稿",
    )
    store.insert_draft(draft)

    assert store.delete_account(account.id, delete_messages=True) is True
    assert store.get_account(account.id) is None
    assert store.get_message(message.id) is None
    assert store.get_todo(todo.id) is None
    assert store.get_draft(draft.id) is None


def test_conversation_upsert_preserves_user_flags(store: Storage) -> None:
    account, conversation = _seed_account_conversation(store)
    # 刷新会话列表时应保留用户设置的 enabled/priority
    store.update_conversation(
        conversation.id, enabled=False, priority=None
    )
    from social_secretary.contracts import Priority

    store.update_conversation(conversation.id, priority=Priority.HIGH)
    store.upsert_conversation(conversation)  # 模拟 refresh 再次写入
    current = store.get_conversation(conversation.id)
    assert current is not None
    assert current.enabled is False
    assert current.priority == Priority.HIGH
    # 按 external_key 查找
    found = store.find_conversation_by_external_key(account.id, conversation.external_key)
    assert found is not None and found.id == conversation.id


def test_conversation_last_message_updated(store: Storage) -> None:
    _, conversation = _seed_account_conversation(store)
    ts = now_local().replace(microsecond=0)
    message = make_message(conversation, "有时间", timestamp=ts)
    store.insert_message(message)
    current = store.get_conversation(conversation.id)
    assert current is not None
    assert current.last_message_at is not None
    assert abs((current.last_message_at - ts).total_seconds()) < 1


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------

def test_message_insert_dedupe_and_queries(store: Storage) -> None:
    _, conversation = _seed_account_conversation(store)
    ts = now_local().replace(microsecond=0)
    first = make_message(conversation, "第一条", timestamp=ts, category=Category.IMPORTANT)
    second = make_message(conversation, "第二条", timestamp=ts + timedelta(minutes=1))
    assert store.insert_message(first) is True
    assert store.insert_message(first) is False  # 跨批次幂等
    assert store.insert_message(second) is True
    assert store.insert_message(second) is False

    day = ts.date()
    messages = store.list_messages(day=day)
    assert [m.id for m in messages] == [second.id, first.id]  # 新→旧

    important = store.list_messages(day=day, category=Category.IMPORTANT)
    assert [m.id for m in important] == [first.id]

    self_message = make_message(conversation, "我自己", is_self=True, timestamp=ts)
    store.insert_message(self_message)
    assert all(not m.is_self for m in store.list_messages(day=day, include_self=False))
    assert len(store.list_messages(day=day, include_self=True)) == 3
    assert store.count_messages(day=day) == 3
    assert store.distinct_conversations_touched(day) == 1


def test_message_metadata_roundtrip(store: Storage) -> None:
    _, conversation = _seed_account_conversation(store)
    ts = datetime(2026, 10, 31, 9, 5, tzinfo=timezone(timedelta(hours=8)))
    message = make_message(
        conversation,
        "带时间的消息",
        timestamp=ts,
        timestamp_source=TimestampSource.UI,
        reasons=("规则A", "规则B"),
    )
    store.insert_message(message, raw_time_text="今天 09:05")
    loaded = store.get_message(message.id)
    assert loaded is not None
    assert loaded.timestamp == ts
    assert loaded.timestamp_source == TimestampSource.UI
    assert loaded.category_reasons == ("规则A", "规则B")
    row = store.get_message_row(message.id)
    assert row is not None and row["raw_time_text"] == "今天 09:05"


def test_message_external_id_lookup(store: Storage) -> None:
    _, conversation = _seed_account_conversation(store)
    message = make_message(conversation, "邮件正文")
    from dataclasses import replace

    message = replace(message, external_id="uid-42")
    store.insert_message(message)
    found = store.find_message_by_external_id(conversation.id, "uid-42")
    assert found is not None and found.id == message.id
    assert store.find_message_by_external_id(conversation.id, "uid-404") is None


# ---------------------------------------------------------------------------
# 扫描记录 / 待办 / 草稿
# ---------------------------------------------------------------------------

def test_scan_runs_and_status_for_day(store: Storage) -> None:
    account, _ = _seed_account_conversation(store)
    now = now_local()
    partial = CollectionResult(
        scan_id="scan-1",
        account_id=account.id,
        status=ScanStatus.PARTIAL,
        messages_seen=5,
        messages_new=2,
        conversations_scanned=1,
        error="某个会话失败",
        warnings=("警告A",),
        started_at=now,
        finished_at=now,
        foreground_used=True,
        duration_ms=12,
    )
    failed = CollectionResult(
        scan_id="scan-2",
        account_id=account.id,
        status=ScanStatus.FAILED,
        error="采集器不可用",
        started_at=now,
        finished_at=now,
    )
    store.insert_scan_run(partial)
    store.insert_scan_run(failed, trigger="auto")
    runs = store.list_scan_runs(day=now.date())
    assert len(runs) == 2
    assert store.last_scan_run(account.id).scan_id == "scan-2"
    assert store.scan_status_for_day(now.date()) == ScanStatus.FAILED
    assert store.count_scan_runs(now.date()) == 2


def test_todo_crud(store: Storage) -> None:
    _, conversation = _seed_account_conversation(store)
    day = now_local().date()
    message = make_message(conversation, "请明天交材料", timestamp=now_local())
    store.insert_message(message)
    todo = TodoItem(
        id="todo-1",
        message_id=message.id,
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        text="交材料",
        due_text="明天",
        confidence=0.7,
        created_at=now_local(),
    )
    store.insert_todo(todo)
    assert [item.id for item in store.list_todos(day=day)] == ["todo-1"]
    assert store.count_open_todos(day) == 1
    assert store.update_todo_status("todo-1", TodoStatus.DONE) is True
    assert store.list_todos(day=day) == []
    assert len(store.list_todos(day=day, include_done=True)) == 1
    assert store.update_todo_status("missing", TodoStatus.DONE) is False


def test_draft_crud(store: Storage) -> None:
    _, conversation = _seed_account_conversation(store)
    message = make_message(conversation, "请问几点开会？", timestamp=now_local())
    store.insert_message(message)
    draft = ReplyDraft(
        id="draft-1",
        message_id=message.id,
        kind=DraftKind.LOCAL_RULES,
        style="简洁",
        text="收到，稍后回复。",
        created_at=now_local(),
        model_label="本地模板",
    )
    store.insert_draft(draft)
    assert [item.id for item in store.list_drafts(message.id)] == ["draft-1"]
    assert store.update_draft_edit("draft-1", "编辑后的草稿") is True
    assert store.get_draft("draft-1").edited_text == "编辑后的草稿"
    assert store.update_draft_edit("missing", "x") is False
    assert store.count_drafts() == 1


# ---------------------------------------------------------------------------
# 同意 / 审计 / KV / 摘要缓存 / 全量删除
# ---------------------------------------------------------------------------

def test_consent_log(store: Storage) -> None:
    assert store.consent_is_granted("qq_read") is False
    store.record_consent("qq_read", True, "用户授权")
    assert store.consent_is_granted("qq_read") is True
    store.record_consent("qq_read", False, "用户撤回")
    assert store.consent_is_granted("qq_read") is False
    assert store.consent_states()["qq_read"] is False
    history = store.consent_history()
    assert [row["action"] for row in history] == ["revoke", "grant"]
    assert history[0]["scope"] == "qq_read"


def test_audit_sanitizes_sensitive_detail(store: Storage) -> None:
    store.append_audit(
        "test_event",
        target_kind="message",
        target_id="m1",
        detail={
            "text": "这是私密消息正文",
            "draft": "草稿正文",
            "api_key": "sk-abcdef123456",
            "message_count": 3,
            "conversation_id": "conv-1",
        },
    )
    events = store.list_audit()
    assert events[0]["action"] == "test_event"
    detail = events[0]["detail"]
    assert "私密消息正文" not in str(detail)
    assert "草稿正文" not in str(detail)
    assert "sk-abcdef123456" not in str(detail)
    assert detail["message_count"] == 3
    assert detail["conversation_id"] == "conv-1"


def test_settings_kv_roundtrip(store: Storage) -> None:
    assert store.get_setting("demo_active", False) is False
    store.set_setting("demo_active", True)
    assert store.get_setting("demo_active") is True
    store.set_setting("allowlist:acct-1", ["张三", "李四"])
    assert store.get_setting("allowlist:acct-1") == ["张三", "李四"]
    store.delete_setting("demo_active")
    assert store.get_setting("demo_active", None) is None


def test_summary_cache_roundtrip(store: Storage) -> None:
    day = now_local().date()
    store.save_summary(day, now_local(), False, {"total": 3, "narrative": "摘要"})
    cached = store.load_summary(day)
    assert cached is not None
    assert cached["payload"]["total"] == 3
    assert cached["is_demo"] is False
    store.clear_summaries()
    assert store.load_summary(day) is None


def test_delete_all_data_and_stats(store: Storage) -> None:
    account, conversation = _seed_account_conversation(store)
    store.insert_message(make_message(conversation, "消息"))
    store.record_consent("qq_read", True)
    store.append_audit("event")
    store.set_setting("demo_active", True)
    stats = store.stats()
    assert stats["messages"] == 1 and stats["accounts"] == 1
    store.delete_all_data()
    stats = store.stats()
    assert stats["messages"] == 0
    assert stats["accounts"] == 0
    assert stats["conversations"] == 0
    assert store.consent_is_granted("qq_read") is False
    assert store.get_setting("demo_active", None) is None
    assert store.list_audit() == []
