"""P2 B2 基础：消息处理状态（work_status）+ 筛选/计数测试。

不依赖 QQ/网络：只使用本地 Storage/Service + fake 消息源。
覆盖 v3→v4 迁移、默认 unhandled、CRUD/持久化、筛选组合、计数、非法输入、
审计脱敏、重复采集不覆盖用户状态、summary 缓存兼容。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ConsentScope,
    ConversationKind,
    MessageWorkStatus,
    ScanStatus,
    TimestampSource,
)
from social_secretary.core.service import ServiceError, SocialSecretaryService
from social_secretary.core.storage import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Storage,
    now_local,
)
from social_secretary.core.summary import (
    generate_daily_summary,
    get_cached_summary,
    message_from_dict,
    message_to_dict,
)
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import (
    FakeMessageSource,
    make_account,
    make_conversation,
    make_message,
    make_raw,
    make_service,
)


# ---------------------------------------------------------------------------
# schema v4 迁移 / 默认值 / 持久化
# ---------------------------------------------------------------------------

def _make_legacy_v3_db(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2, 3):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (3, '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO accounts(id, kind, display_name, enabled, identifier)"
            " VALUES ('legacy-acct', 'email', '旧邮箱', 1, 'old@example.com')"
        )
        conn.execute(
            "INSERT INTO conversations(id, account_id, kind, title, external_key, enabled)"
            " VALUES ('legacy-conv', 'legacy-acct', 'mail', 'INBOX', 'INBOX', 1)"
        )
        conn.execute(
            """
            INSERT INTO messages(
                id, account_id, conversation_id, conversation_title, conversation_kind,
                sender, is_self, text, collected_at, sort_ts, local_day, content_hash,
                subject, attachment_names_json
            ) VALUES (
                'legacy-msg', 'legacy-acct', 'legacy-conv', 'INBOX', 'mail',
                '旧发件人', 0, '旧邮件正文', '2026-01-01T09:00:00+08:00', 0,
                '2026-01-01', 'legacy-hash', '旧主题', '[]'
            )
            """
        )


def test_migration_v3_to_v4_defaults_unhandled(tmp_path) -> None:
    db_path = tmp_path / "legacy-v3.db"
    _make_legacy_v3_db(db_path)

    storage = Storage(db_path)
    storage.initialize()
    assert storage.schema_version == SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        value = conn.execute(
            "SELECT work_status FROM messages WHERE id='legacy-msg'"
        ).fetchone()[0]
    assert "work_status" in columns
    assert value == "unhandled"

    message = storage.get_message("legacy-msg")
    assert message is not None
    assert message.work_status is MessageWorkStatus.UNHANDLED
    # 迁移后四种状态计数可用，旧数据归入 unhandled
    assert storage.count_messages_by_work_status() == {
        "unhandled": 1,
        "needs_reply": 0,
        "done": 0,
        "no_reply": 0,
    }


def test_work_status_roundtrip_and_restart(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    storage = Storage(db_path)
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-work", account_id=account.id, title="INBOX", external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = replace(
        make_message(conversation, "需要跟进的消息", timestamp=now_local()),
        work_status=MessageWorkStatus.NEEDS_REPLY,
    )
    storage.insert_message(message)

    assert storage.get_message(message.id).work_status is MessageWorkStatus.NEEDS_REPLY
    assert storage.list_messages()[0].work_status is MessageWorkStatus.NEEDS_REPLY
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT work_status FROM messages WHERE id=?", (message.id,)
        ).fetchone()[0]
    assert raw == "needs_reply"

    reopened = Storage(db_path)
    reopened.initialize()
    assert reopened.get_message(message.id).work_status is MessageWorkStatus.NEEDS_REPLY

    assert reopened.update_message_work_status(message.id, MessageWorkStatus.DONE) is True
    assert reopened.get_message(message.id).work_status is MessageWorkStatus.DONE
    assert reopened.update_message_work_status("missing", MessageWorkStatus.DONE) is False


def test_unknown_db_value_falls_back_to_unhandled(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    storage = Storage(db_path)
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-x", account_id=account.id, title="INBOX", external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = make_message(conversation, "x", timestamp=now_local())
    storage.insert_message(message)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE messages SET work_status='future_status' WHERE id=?", (message.id,)
        )
    loaded = storage.get_message(message.id)
    assert loaded.work_status is MessageWorkStatus.UNHANDLED


# ---------------------------------------------------------------------------
# service：设置状态 / 非法输入 / 审计
# ---------------------------------------------------------------------------

def _service_with_message():
    service = make_service(credential_store=InMemoryCredentialStore())
    account = service.add_mail_account("work@example.com", "工作邮箱")
    conversation = service.storage.list_conversations(account.id)[0]
    message = make_message(
        conversation, "请明天之前回复我，标记正文X9Q7", timestamp=now_local()
    )
    service.storage.insert_message(message)
    return service, account, conversation, message


def test_set_message_work_status_and_audit_no_body() -> None:
    service, _account, _conversation, message = _service_with_message()
    try:
        service.set_message_work_status(message.id, MessageWorkStatus.DONE)
        loaded = service.storage.get_message(message.id)
        assert loaded.work_status is MessageWorkStatus.DONE

        audit = json.dumps(service.storage.list_audit(), ensure_ascii=False)
        assert "message_work_status_changed" in audit
        assert "done" in audit
        assert message.id in audit
        assert "标记正文X9Q7" not in audit
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    "message_id,status,error_type",
    [
        ("missing-id", MessageWorkStatus.DONE, "not_found"),
        ("", MessageWorkStatus.DONE, "not_found"),
        (None, MessageWorkStatus.DONE, "not_found"),
    ],
)
def test_set_message_work_status_invalid_id(message_id, status, error_type: str) -> None:
    from social_secretary.core.service import NotFoundError

    service, *_ = _service_with_message()
    try:
        with pytest.raises(NotFoundError):
            service.set_message_work_status(message_id, status)  # type: ignore[arg-type]
    finally:
        service.shutdown()


def test_set_message_work_status_invalid_status() -> None:
    service, _account, _conversation, message = _service_with_message()
    try:
        with pytest.raises(ServiceError):
            service.set_message_work_status(message.id, "not-a-status")  # type: ignore[arg-type]
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 筛选组合 / 计数
# ---------------------------------------------------------------------------

def test_list_messages_filters_by_status_and_combinations() -> None:
    service = make_service(credential_store=InMemoryCredentialStore())
    try:
        account = service.add_mail_account("filter@example.com", "筛选邮箱")
        conversation = service.storage.list_conversations(account.id)[0]
        today = now_local()
        yesterday = today - timedelta(days=1)
        statuses = [
            MessageWorkStatus.UNHANDLED,
            MessageWorkStatus.NEEDS_REPLY,
            MessageWorkStatus.DONE,
            MessageWorkStatus.NO_REPLY,
        ]
        inserted = []
        for index, status in enumerate(statuses):
            inserted.append(
                replace(
                    make_message(
                        conversation,
                        f"消息{index}",
                        timestamp=today + timedelta(minutes=index),
                        category=Category.IMPORTANT
                        if index % 2
                        else Category.NORMAL,
                        reasons=(f"reason-{index}",),
                    ),
                    work_status=status,
                )
            )
        inserted.append(
            replace(
                make_message(
                    conversation,
                    "昨天的未处理",
                    timestamp=yesterday,
                    category=Category.NORMAL,
                ),
                work_status=MessageWorkStatus.UNHANDLED,
            )
        )
        for item in inserted:
            service.storage.insert_message(item)

        assert {
            item.id for item in service.list_messages(work_status=MessageWorkStatus.DONE)
        } == {inserted[2].id}
        # 组合 day + status
        day_results = service.list_messages(
            day=today.date(), work_status=MessageWorkStatus.UNHANDLED
        )
        assert {item.id for item in day_results} == {inserted[0].id}
        # 组合 category + status（index=1 → IMPORTANT + NEEDS_REPLY）
        important_needs_reply = service.list_messages(
            category=Category.IMPORTANT, work_status=MessageWorkStatus.NEEDS_REPLY
        )
        assert {item.id for item in important_needs_reply} == {inserted[1].id}
        # 分页 limit
        assert len(service.list_messages(work_status=MessageWorkStatus.UNHANDLED, limit=1)) == 1
        # 未指定状态 = 全部
        assert len(service.list_messages(limit=100)) == 5
    finally:
        service.shutdown()


def test_message_work_status_counts_four_keys_and_updates() -> None:
    service, _account, _conversation, message = _service_with_message()
    try:
        counts = service.message_work_status_counts()
        assert counts == {
            "unhandled": 1,
            "needs_reply": 0,
            "done": 0,
            "no_reply": 0,
        }
        service.set_message_work_status(message.id, MessageWorkStatus.NO_REPLY)
        counts = service.message_work_status_counts()
        assert counts == {
            "unhandled": 0,
            "needs_reply": 0,
            "done": 0,
            "no_reply": 1,
        }
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 重复采集不覆盖用户状态（pipeline/service 集成）
# ---------------------------------------------------------------------------

def test_rescan_does_not_overwrite_work_status() -> None:
    service = make_service(credential_store=InMemoryCredentialStore())
    try:
        account = service.add_mail_account("rescan@example.com", "重复采集邮箱")
        conversation = service.storage.list_conversations(account.id)[0]
        raw = make_raw(
            "INBOX",
            "请明天提交周报",
            timestamp=now_local(),
            external_id="imap:1:1",
            source_locator="IMAP INBOX UID 1",
        )
        source = FakeMessageSource(
            kind=AccountKind.EMAIL,
            conversations=[conversation],
            messages={"INBOX": [raw]},
        )
        service.shutdown()

        service = make_service(
            credential_store=InMemoryCredentialStore(),
            source_factory=lambda item: source,
        )
        try:
            service.grant_consent(ConsentScope.MAIL_READ)
            service.resume_collection()
            first = service.run_scan(account.id)
            assert first[0].status is ScanStatus.OK
            assert first[0].messages_new == 1
            messages = service.list_messages()
            assert len(messages) == 1
            message = messages[0]
            assert message.work_status is MessageWorkStatus.UNHANDLED

            service.set_message_work_status(message.id, MessageWorkStatus.DONE)
            second = service.run_scan(account.id)
            assert second[0].status is ScanStatus.OK
            assert second[0].messages_new == 0
            assert len(service.list_messages()) == 1
            assert (
                service.storage.get_message(message.id).work_status
                is MessageWorkStatus.DONE
            )
            assert service.message_work_status_counts()["done"] == 1
        finally:
            service.shutdown()
    finally:
        try:
            service.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# summary 缓存序列化
# ---------------------------------------------------------------------------

def test_summary_cache_serializes_work_status_and_clears_on_update(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-summary",
        account_id=account.id,
        kind=ConversationKind.MAIL,
        external_key="INBOX",
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = replace(
        make_message(conversation, "摘要状态消息", timestamp=now_local()),
        work_status=MessageWorkStatus.NEEDS_REPLY,
    )
    storage.insert_message(message)

    payload = message_to_dict(message)
    assert payload["work_status"] == "needs_reply"
    assert message_from_dict(payload).work_status is MessageWorkStatus.NEEDS_REPLY
    legacy = dict(payload)
    legacy.pop("work_status")
    assert message_from_dict(legacy).work_status is MessageWorkStatus.UNHANDLED

    day = message.timestamp.date()
    summary = generate_daily_summary(storage, day=day)
    assert summary is not None
    assert get_cached_summary(storage, day) is not None
    assert get_cached_summary(storage, day).normal[0].work_status is MessageWorkStatus.NEEDS_REPLY


def test_set_work_status_clears_summary_cache() -> None:
    service, _account, _conversation, message = _service_with_message()
    try:
        day = message.timestamp.date()
        service.generate_daily_summary(day=day)
        assert service.get_cached_summary(day) is not None
        service.set_message_work_status(message.id, MessageWorkStatus.DONE)
        # 缓存里的旧 work_status 不可再用，更新状态后清缓存
        assert service.get_cached_summary(day) is None
    finally:
        service.shutdown()
