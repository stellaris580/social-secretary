"""邮箱收件箱 API 测试（P0-14）：真实邮件可读/可同步、演示标记、阻断语义、持久化。

覆盖 contracts.ServiceAPI 新增方法：
``list_mail_accounts / mail_inbox_status / sync_mail / list_inbox_messages /
generate_latest_mail_summary`` 以及 messages.subject 迁移与 is_demo 回填。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ConnectionCheck,
    ConsentScope,
    ConversationKind,
    RawMessage,
    ScanStatus,
    TimestampSource,
)
from social_secretary.core.service import NotFoundError, ServiceError
from social_secretary.core.storage import Storage
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import make_account, make_conversation, make_message, make_service

TZ = timezone(timedelta(hours=8))
OLD_MAIL_TIME = datetime(2025, 3, 4, 10, 30, 0, tzinfo=TZ)


class FakeMailSource:
    """只读邮件 source fake：conversation_key 用 conversation.id（与真实采集器一致）。"""

    kind = AccountKind.EMAIL

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime | None]] = []
        self.closed = False
        self.raw_factory = self._default_raw

    def _default_raw(self, conversation, index: int) -> RawMessage:
        return RawMessage(
            conversation_key=conversation.id,
            sender="发件人",
            is_self=False,
            text=f"主题{index}：邮件正文全文 {index}",
            timestamp=OLD_MAIL_TIME + timedelta(days=index),
            timestamp_source=TimestampSource.UI,
            external_id=f"imap:1:{index}",
            source_locator=f"IMAP INBOX UID {index}",
            extras={"subject": f"历史邮件主题 {index}"},
        )

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(ok=True, detail="fake IMAP 只读连接成功")

    def list_conversations(self):
        return []

    def fetch_messages(self, conversation, *, since=None, limit=100, foreground_ack=False):
        self.calls.append((conversation.id, since))
        count = 2 if since is None else 1
        return [self.raw_factory(conversation, index) for index in range(count)]

    def close(self) -> None:
        self.closed = True


def _setup_real_mail(service, source: FakeMailSource):
    account = service.add_mail_account("inbox@example.com", "收件箱测试邮箱")
    service.set_mail_credential("inbox@example.com", "abcdefghijklmnop")
    service.grant_consent(ConsentScope.MAIL_READ)
    service.resume_collection()
    return account


# ---------------------------------------------------------------------------
# list_mail_accounts / mail_inbox_status
# ---------------------------------------------------------------------------

def test_list_mail_accounts_only_email() -> None:
    service = make_service()
    try:
        service.add_qq_account("测试 QQ", ["张三"])
        mail = service.add_mail_account("inbox@example.com", "邮箱")
        accounts = service.list_mail_accounts()
        assert [account.id for account in accounts] == [mail.id]
        assert all(account.kind is AccountKind.EMAIL for account in accounts)
    finally:
        service.shutdown()


def test_mail_inbox_status_empty_and_single_account() -> None:
    service = make_service()
    try:
        empty = service.mail_inbox_status()
        assert empty["configured"] is False
        assert empty["enabled"] is False
        assert empty["has_credential"] is False
        assert empty["consent"] is False
        assert empty["paused"] is True
        assert empty["message_count"] == 0
        assert empty["demo_count"] == 0
        assert empty["last_sync_at"] is None
        assert empty["last_status"] == ""
        assert empty["last_error"] is None

        account = service.add_mail_account("inbox@example.com", "邮箱", folder="INBOX")
        status = service.mail_inbox_status(account.id)
        assert status["configured"] is True
        assert status["enabled"] is True
        assert status["has_credential"] is False
        assert status["consent"] is False
        assert status["paused"] is True
        assert status["message_count"] == 0
        assert status["account_id"] == account.id

        service.set_mail_credential("inbox@example.com", "abcdefghijklmnop")
        service.grant_consent(ConsentScope.MAIL_READ)
        service.resume_collection()
        status = service.mail_inbox_status()
        assert status["has_credential"] is True
        assert status["consent"] is True
        assert status["paused"] is False
        assert service.mail_inbox_status(account.id)["has_credential"] is True
    finally:
        service.shutdown()


def test_mail_inbox_status_rejects_qq_scope() -> None:
    service = make_service()
    try:
        qq = service.add_qq_account("测试 QQ", ["张三"])
        with pytest.raises(ServiceError):
            service.mail_inbox_status(qq.id)
        with pytest.raises(NotFoundError):
            service.mail_inbox_status("missing-account")
    finally:
        service.shutdown()


def test_mail_inbox_status_counts_and_last_run(tmp_path) -> None:
    source = FakeMailSource()
    service = make_service(source_factory=lambda account: source)
    try:
        account = _setup_real_mail(service, source)
        service.sync_mail(account.id)
        status = service.mail_inbox_status(account.id)
        assert status["message_count"] == 2
        assert status["demo_count"] == 0
        assert status["last_status"] == "ok"
        assert status["last_error"] is None
        assert status["last_sync_at"] is not None
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# sync_mail：阻断语义 / 只扫邮箱 / 真实同步
# ---------------------------------------------------------------------------

def test_sync_mail_requires_consent_and_reports_blocked() -> None:
    source = FakeMailSource()
    service = make_service(source_factory=lambda account: source)
    try:
        service.add_mail_account("inbox@example.com", "邮箱")
        service.set_mail_credential("inbox@example.com", "abcdefghijklmnop")
        service.resume_collection()  # 未授予 MAIL_READ
        results = service.sync_mail()
        assert len(results) == 1
        assert results[0].status is ScanStatus.BLOCKED
        assert "同意" in (results[0].error or "")
        assert source.calls == []
        assert service.list_inbox_messages() == []
        assert service.mail_inbox_status()["last_status"] == "blocked"
    finally:
        service.shutdown()


def test_sync_mail_paused_reports_paused() -> None:
    source = FakeMailSource()
    service = make_service(source_factory=lambda account: source)
    try:
        account = service.add_mail_account("inbox@example.com", "邮箱")
        service.set_mail_credential("inbox@example.com", "abcdefghijklmnop")
        service.grant_consent(ConsentScope.MAIL_READ)
        service.pause_collection()
        results = service.sync_mail()
        assert len(results) == 1
        assert results[0].status is ScanStatus.PAUSED
        assert "暂停" in (results[0].error or "")
        assert source.calls == []
        assert service.list_inbox_messages() == []
        assert service.mail_inbox_status()["paused"] is True
    finally:
        service.shutdown()


def test_sync_mail_only_email_accounts_never_qq() -> None:
    source = FakeMailSource()
    seen_kinds: list[AccountKind] = []

    def factory(account):
        seen_kinds.append(account.kind)
        return source

    service = make_service(source_factory=factory)
    try:
        qq = service.add_qq_account("测试 QQ", ["张三"])
        service.grant_consent(ConsentScope.QQ_READ)
        # 只有 QQ 账号时 sync_mail 不返回任何结果、也不触碰 QQ 采集器
        assert service.sync_mail() == []
        assert seen_kinds == []
        with pytest.raises(ServiceError):
            service.sync_mail(qq.id)  # QQ 账号不能走邮箱同步
        assert seen_kinds == []
    finally:
        service.shutdown()


def test_sync_mail_success_persists_subject_and_reopen(tmp_path) -> None:
    source = FakeMailSource()
    credential_store = InMemoryCredentialStore()
    service = make_service(
        credential_store=credential_store, source_factory=lambda account: source
    )
    try:
        account = _setup_real_mail(service, source)
        results = service.sync_mail(account.id, user_confirmed=True)
        assert len(results) == 1
        assert results[0].status is ScanStatus.OK
        assert results[0].messages_new == 2
        # scan_run 触发来源标记为 mail_sync
        with sqlite3.connect(service.storage.db_path) as conn:
            trigger = conn.execute(
                "SELECT trigger FROM scan_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert trigger == "mail_sync"

        inbox = service.list_inbox_messages()
        assert len(inbox) == 2
        # 历史日期邮件（2025）也能读到，且按时间倒序
        assert [message.subject for message in inbox] == [
            "历史邮件主题 1",
            "历史邮件主题 0",
        ]
        assert all(message.is_demo is False for message in inbox)
        assert inbox[0].text.startswith("主题1：")
        assert inbox[0].conversation_kind is ConversationKind.MAIL

        # 二次同步走增量（fake 用 since 返回 1 条），去重不重复入库
        second = service.sync_mail()
        assert second[0].messages_new == 0
        assert len(service.list_inbox_messages()) == 2

        # 同日摘要按最新邮件日期生成
        summary = service.generate_latest_mail_summary()
        assert summary is not None
        assert summary.day.isoformat() == (OLD_MAIL_TIME + timedelta(days=1)).date().isoformat()
    finally:
        service.shutdown()

    # 重开 service（同一数据目录）：subject/is_demo 从 DB 回填，邮件仍可查
    reopened = make_service(
        credential_store=credential_store, source_factory=lambda account: source
    )
    try:
        inbox = reopened.list_inbox_messages()
        assert len(inbox) == 2
        assert inbox[0].subject == "历史邮件主题 1"
        assert inbox[0].is_demo is False
        status = reopened.mail_inbox_status()
        assert status["message_count"] == 2
        assert status["last_status"] == "ok"
        latest = reopened.generate_latest_mail_summary()
        assert latest is not None and latest.day == (OLD_MAIL_TIME + timedelta(days=1)).date()
    finally:
        reopened.shutdown()


def test_sync_mail_demo_account_structured_blocked_without_network() -> None:
    source = FakeMailSource()
    service = make_service(source_factory=lambda account: source)
    try:
        service.load_demo_data()
        # 聚合同步静默跳过演示账号（UI 不应把演示账号当“同步失败”）
        assert service.sync_mail() == []
        assert source.calls == []
        # 明确指定演示账号 → 结构化说明，不联网
        from social_secretary.core.demo import DEMO_MAIL_ACCOUNT_ID

        results = service.sync_mail(DEMO_MAIL_ACCOUNT_ID)
        assert len(results) == 1
        assert results[0].status is ScanStatus.BLOCKED
        assert "演示账号" in (results[0].error or "")
        assert source.calls == []
        # 演示邮件仍可在收件箱看到，且 is_demo=True
        inbox = service.list_inbox_messages()
        assert len(inbox) == 1
        assert inbox[0].is_demo is True
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# list_inbox_messages：排序 / 过滤 / is_demo
# ---------------------------------------------------------------------------

def test_list_inbox_messages_sorting_and_filters() -> None:
    service = make_service()
    try:
        mail_account = service.add_mail_account("inbox@example.com", "邮箱")
        conversation = service.list_conversations(mail_account.id)[0]
        credential_store = service.credential_store
        credential_store  # noqa: B018 - 凭据不参与本用例，仅保持接口一致

        older = make_message(
            conversation,
            "旧邮件正文",
            timestamp=datetime(2024, 1, 2, 8, 0, tzinfo=TZ),
            sender="A",
        )
        newer = make_message(
            conversation,
            "新邮件正文",
            timestamp=datetime(2024, 5, 6, 9, 0, tzinfo=TZ),
            sender="B",
        )
        no_timestamp = make_message(
            conversation,
            "无时间邮件正文",
            timestamp=None,
            collected_at=datetime(2024, 3, 1, 7, 0, tzinfo=TZ),
            sender="C",
        )
        service.storage.insert_messages([older, newer, no_timestamp])

        inbox = service.list_inbox_messages()
        assert [message.text for message in inbox] == [
            "新邮件正文",
            "无时间邮件正文",
            "旧邮件正文",
        ]
        assert service.list_inbox_messages(limit=1)[0].text == "新邮件正文"
        assert service.list_inbox_messages(mail_account.id, limit=10) == inbox

        with pytest.raises(ServiceError):
            qq = service.add_qq_account("QQ", ["张三"])
            service.list_inbox_messages(qq.id)
        with pytest.raises(NotFoundError):
            service.list_inbox_messages("missing-account")
    finally:
        service.shutdown()


def test_message_is_demo_backfilled_from_account_prefix() -> None:
    service = make_service()
    try:
        demo_account = make_account("demo-mail-test", kind=AccountKind.EMAIL, identifier="demo@example.com")
        real_account = make_account("mail-real", kind=AccountKind.EMAIL, identifier="real@example.com")
        service.storage.upsert_account(demo_account)
        service.storage.upsert_account(real_account)
        demo_conv = make_conversation(
            "demo-conv-mail-test",
            account_id=demo_account.id,
            kind=ConversationKind.MAIL,
            title="INBOX",
            external_key="INBOX",
        )
        real_conv = make_conversation(
            "real-conv-mail-test",
            account_id=real_account.id,
            kind=ConversationKind.MAIL,
            title="INBOX",
            external_key="INBOX",
        )
        service.storage.upsert_conversation(demo_conv)
        service.storage.upsert_conversation(real_conv)
        service.storage.insert_messages(
            [
                make_message(demo_conv, "演示邮件", timestamp=OLD_MAIL_TIME),
                make_message(real_conv, "真实邮件", timestamp=OLD_MAIL_TIME),
            ]
        )
        inbox = {message.text: message for message in service.list_inbox_messages()}
        assert inbox["演示邮件"].is_demo is True
        assert inbox["真实邮件"].is_demo is False
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# generate_latest_mail_summary
# ---------------------------------------------------------------------------

def test_generate_latest_mail_summary_none_when_no_mail() -> None:
    service = make_service()
    try:
        assert service.generate_latest_mail_summary() is None
    finally:
        service.shutdown()


def test_generate_latest_mail_summary_uses_most_recent_mail_day() -> None:
    service = make_service()
    try:
        mail_account = service.add_mail_account("inbox@example.com", "邮箱")
        conversation = service.list_conversations(mail_account.id)[0]
        service.storage.insert_messages(
            [
                make_message(
                    conversation,
                    "三月邮件",
                    timestamp=datetime(2025, 3, 4, 10, 0, tzinfo=TZ),
                    category=Category.IMPORTANT,
                ),
                make_message(
                    conversation,
                    "五月邮件",
                    timestamp=datetime(2025, 5, 9, 10, 0, tzinfo=TZ),
                ),
            ]
        )
        summary = service.generate_latest_mail_summary()
        assert summary is not None
        assert summary.day.isoformat() == "2025-05-09"
        assert summary.counts["total"] == 1
        assert summary.is_demo is False
    finally:
        service.shutdown()


def test_generate_latest_mail_summary_demo_only() -> None:
    service = make_service()
    try:
        service.load_demo_data()
        summary = service.generate_latest_mail_summary()
        assert summary is not None
        assert summary.is_demo is True
        assert summary.day == service._now().date()
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 迁移 / subject 持久化
# ---------------------------------------------------------------------------

def test_subject_column_migration_from_v1(tmp_path) -> None:
    from social_secretary.core.storage import MIGRATIONS, SCHEMA_VERSION

    db_path = tmp_path / "legacy-v1.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statement in MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (1, '2026-01-01T00:00:00+08:00')"
        )
    storage = Storage(db_path)
    storage.initialize()
    assert storage.schema_version == SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "subject" in columns
    assert "attachment_names_json" in columns


def test_service_import_keeps_mail_collector_lazy() -> None:
    """冻结版要求：import core.service 不得把 collectors.mail/imaplib 拉进主进程（懒 import）。"""
    src = Path(__file__).resolve().parents[2] / "src"
    code = (
        "import sys\n"
        "import social_secretary.core.service  # noqa: F401\n"
        "print(int('social_secretary.collectors.mail' in sys.modules))\n"
        "print(int('imaplib' in sys.modules))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["0", "0"]
