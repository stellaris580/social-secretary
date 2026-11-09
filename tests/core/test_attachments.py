"""P0-20 邮件附件文件名持久化/传递测试。

只保存附件**文件名**（不下载/不保存内容）：
- `pipeline` 从 `RawMessage.extras["attachment_names"]` 清洗后写入 `Message.attachment_names`；
- `storage` v3 迁移新增 `attachment_names_json`，插入/读取往返，旧库升级为空元组；
- `summary` 缓存序列化兼容该字段；收件箱 API 可读取。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from social_secretary.contracts import (
    AccountKind,
    ConnectionCheck,
    ConversationKind,
    RawMessage,
    ScanStatus,
    TimestampSource,
)
from social_secretary.core.pipeline import (
    MAX_ATTACHMENT_NAME_CHARS,
    MAX_ATTACHMENT_NAMES,
    ScanPipeline,
    sanitize_attachment_names,
)
from social_secretary.core.storage import MIGRATIONS, SCHEMA_VERSION, Storage, now_local
from social_secretary.core.summary import (
    generate_daily_summary,
    get_cached_summary,
    message_from_dict,
    message_to_dict,
)
from tests.core.fakes import (
    FakeMessageSource,
    make_account,
    make_conversation,
    make_message,
    make_raw,
    make_service,
)


# ---------------------------------------------------------------------------
# 迁移 v2 -> v3 / 读写往返 / 旧库空值
# ---------------------------------------------------------------------------

def _make_legacy_v2_db(path) -> None:
    """构造一个 schema v2（无 attachment_names_json）的旧库，并写入一条旧消息。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statement in MIGRATIONS[1]:
            conn.execute(statement)
        for statement in MIGRATIONS[2]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (2, '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO accounts(id, kind, display_name, enabled, created_at, identifier)"
            " VALUES ('legacy-acct', 'email', '旧邮箱', 1, NULL, 'old@example.com')"
        )
        conn.execute(
            "INSERT INTO conversations(id, account_id, kind, title, external_key, enabled, priority)"
            " VALUES ('legacy-conv', 'legacy-acct', 'mail', 'INBOX', 'INBOX', 1, 'normal')"
        )
        conn.execute(
            """
            INSERT INTO messages(
                id, account_id, conversation_id, conversation_title, conversation_kind,
                sender, is_self, text, collected_at, sort_ts, local_day, content_hash,
                subject
            ) VALUES (
                'legacy-msg', 'legacy-acct', 'legacy-conv', 'INBOX', 'mail',
                '旧发件人', 0, '旧邮件正文', '2026-01-01T09:00:00+08:00', 0,
                '2026-01-01', 'legacy-hash', '旧主题'
            )
            """
        )


def test_migration_v2_to_v3_adds_attachment_column_and_empty_default(tmp_path) -> None:
    db_path = tmp_path / "legacy-v2.db"
    _make_legacy_v2_db(db_path)

    storage = Storage(db_path)
    storage.initialize()
    assert storage.schema_version == SCHEMA_VERSION

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        value = conn.execute(
            "SELECT attachment_names_json FROM messages WHERE id='legacy-msg'"
        ).fetchone()[0]
    assert "attachment_names_json" in columns
    assert value == "[]"

    message = storage.get_message("legacy-msg")
    assert message is not None
    assert message.attachment_names == ()
    assert message.subject == "旧主题"
    assert message.text == "旧邮件正文"


def test_attachment_names_insert_read_roundtrip(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    assert storage.schema_version == SCHEMA_VERSION
    account = make_account(kind=AccountKind.EMAIL, identifier="a@example.com")
    conversation = make_conversation(
        "conv-mail-attach",
        account_id=account.id,
        kind=ConversationKind.MAIL,
        title="INBOX",
        external_key="INBOX",
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    names = ("报告.pdf", "图片 1.png", "数据.csv")
    message = replace(
        make_message(conversation, "带附件的邮件", timestamp=now_local()),
        attachment_names=names,
    )
    assert storage.insert_message(message) is True

    loaded = storage.get_message(message.id)
    assert loaded is not None and loaded.attachment_names == names
    assert storage.list_messages()[0].attachment_names == names
    with sqlite3.connect(storage.db_path) as conn:
        raw = conn.execute(
            "SELECT attachment_names_json FROM messages WHERE id=?", (message.id,)
        ).fetchone()[0]
    assert json.loads(raw) == list(names)

    # 重开（新 Storage 实例）仍可读
    reopened = Storage(storage.db_path)
    reopened.initialize()
    assert reopened.get_message(message.id).attachment_names == names

    # 无附件消息 → 空元组
    plain = make_message(conversation, "无附件邮件", timestamp=now_local())
    storage.insert_message(plain)
    assert storage.get_message(plain.id).attachment_names == ()


def test_corrupt_attachment_json_reads_as_empty(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-mail", account_id=account.id, kind=ConversationKind.MAIL, external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = make_message(conversation, "损坏 JSON", timestamp=now_local())
    storage.insert_message(message)
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute(
            "UPDATE messages SET attachment_names_json='not-json' WHERE id=?",
            (message.id,),
        )
    assert storage.get_message(message.id).attachment_names == ()


# ---------------------------------------------------------------------------
# pipeline 清洗
# ---------------------------------------------------------------------------

def test_sanitize_attachment_names_rules() -> None:
    assert sanitize_attachment_names(None) == ()
    assert sanitize_attachment_names("a.pdf") == ()  # 非 list/tuple
    assert sanitize_attachment_names({"a.pdf"}) == ()  # set 不接受
    cleaned = sanitize_attachment_names(
        ["报告.pdf", " 图片 1.png ", 123, "", None, "\x00bad\x01name", "报告.pdf"]
    )
    assert cleaned == ("报告.pdf", "图片 1.png", "badname")
    long_name = "长" * (MAX_ATTACHMENT_NAME_CHARS + 50)
    assert len(sanitize_attachment_names([long_name])[0]) == MAX_ATTACHMENT_NAME_CHARS
    many = [f"f{index}.pdf" for index in range(MAX_ATTACHMENT_NAMES + 5)]
    assert len(sanitize_attachment_names(many)) == MAX_ATTACHMENT_NAMES


def test_pipeline_extracts_and_sanitizes_mail_attachments(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-mail", account_id=account.id, kind=ConversationKind.MAIL, external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)

    attachment_names = [
        "报告.pdf",
        " 图片 1.png ",
        123,
        "",
        "\x00bad\x01name",
        "报告.pdf",
        "长" * (MAX_ATTACHMENT_NAME_CHARS + 20),
        *[f"f{index}.pdf" for index in range(MAX_ATTACHMENT_NAMES + 5)],
    ]
    raw = make_raw(
        "INBOX",
        "邮件正文：请查收附件",
        timestamp=now_local(),
        external_id="imap:1:1",
        source_locator="IMAP INBOX UID 1",
        extras={"subject": "带附件邮件", "attachment_names": attachment_names},
    )
    source = FakeMessageSource(
        kind=AccountKind.EMAIL, conversations=[conversation], messages={"INBOX": [raw]}
    )
    pipeline = ScanPipeline(storage, source_factory=lambda item: source)
    results = pipeline.run([account.id])
    assert results[0].status is ScanStatus.OK

    message = storage.list_messages()[0]
    names = message.attachment_names
    assert len(names) == MAX_ATTACHMENT_NAMES
    assert names[:3] == ("报告.pdf", "图片 1.png", "badname")
    assert len(names[3]) == MAX_ATTACHMENT_NAME_CHARS
    assert names[-1] == "f5.pdf"  # 4 个固定名 + f0..f5 = 10 个
    assert message.subject == "带附件邮件"


def test_qq_message_has_no_attachment_names(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.QQ)
    conversation = make_conversation(
        "conv-qq", account_id=account.id, title="张三", external_key="张三"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    raw = make_raw(
        "张三",
        "QQ 消息不应有附件名",
        timestamp=now_local(),
        source_locator="row-1",
        extras={"attachment_names": ["恶意注入.pdf"]},
    )
    source = FakeMessageSource(
        kind=AccountKind.QQ, conversations=[conversation], messages={"张三": [raw]}
    )
    pipeline = ScanPipeline(storage, source_factory=lambda item: source)
    pipeline.run([account.id])
    message = storage.list_messages()[0]
    assert message.attachment_names == ()


# ---------------------------------------------------------------------------
# summary 缓存序列化 / 收件箱 API
# ---------------------------------------------------------------------------

def test_summary_cache_serialization_keeps_attachments(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-mail", account_id=account.id, kind=ConversationKind.MAIL, external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = replace(
        make_message(conversation, "今日带附件邮件", timestamp=now_local()),
        subject="带附件主题",
        attachment_names=("合同.pdf", "报价单.xlsx"),
    )
    storage.insert_message(message)

    payload = message_to_dict(message)
    assert payload["attachment_names"] == ["合同.pdf", "报价单.xlsx"]
    restored = message_from_dict(payload)
    assert restored.attachment_names == ("合同.pdf", "报价单.xlsx")
    # 旧缓存 payload（无该字段）→ 空元组
    legacy_payload = dict(payload)
    legacy_payload.pop("attachment_names")
    assert message_from_dict(legacy_payload).attachment_names == ()

    day = message.timestamp.date()
    summary = generate_daily_summary(storage, day=day)
    assert summary.counts["total"] == 1
    cached = get_cached_summary(storage, day)
    assert cached is not None
    cached_message = cached.normal[0]
    assert cached_message.attachment_names == ("合同.pdf", "报价单.xlsx")


class _MailWithAttachmentsSource:
    kind = AccountKind.EMAIL

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(ok=True, detail="fake IMAP 连接成功")

    def list_conversations(self):
        return []

    def fetch_messages(self, conversation, *, since=None, limit=100, foreground_ack=False):
        now = now_local()
        return [
            RawMessage(
                conversation_key=conversation.id,
                sender="项目经理",
                is_self=False,
                text="请查收附件。",
                timestamp=now,
                timestamp_source=TimestampSource.UI,
                external_id="imap:1:1",
                source_locator="IMAP INBOX UID 1",
                extras={"subject": "项目材料", "attachment_names": ["方案.pdf", "排期.xlsx"]},
            )
        ]

    def close(self) -> None:
        return None


def test_inbox_api_exposes_attachment_names_after_sync() -> None:
    service = make_service(
        source_factory=lambda account: _MailWithAttachmentsSource()
    )
    try:
        account = service.add_mail_account("attach@example.com", "附件邮箱")
        service.set_mail_credential("attach@example.com", "abcdefghijklmnop")
        from social_secretary.contracts import ConsentScope

        service.grant_consent(ConsentScope.MAIL_READ)
        service.resume_collection()
        results = service.sync_mail(account.id)
        assert results[0].status is ScanStatus.OK

        inbox = service.list_inbox_messages()
        assert len(inbox) == 1
        assert inbox[0].subject == "项目材料"
        assert inbox[0].attachment_names == ("方案.pdf", "排期.xlsx")
    finally:
        service.shutdown()
