"""邮件采集器接线测试：MailCollector 构造、凭据 key 对齐、IMAP 增量状态持久化。

不访问真实网络：只验证“接线正确 + 无网络初始化”。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

mail_pkg = pytest.importorskip("social_secretary.collectors.mail")
mail_state_mod = pytest.importorskip("social_secretary.collectors.mail.state")

from social_secretary.collectors.mail import (  # noqa: E402
    MailCollector,
    mail_credential_key as collector_mail_key,
)
from social_secretary.collectors.mail.state import MailSyncState  # noqa: E402
from social_secretary.contracts import (  # noqa: E402
    AccountKind,
    ConnectionCheck,
    ConversationKind,
    ConsentScope,
    RawMessage,
    ScanStatus,
    TimestampSource,
)
from social_secretary.core.mail_state import StorageSyncStateStore  # noqa: E402
from social_secretary.core.storage import Storage, now_local  # noqa: E402
from tests.core.fakes import make_conversation, make_service  # noqa: E402


# ---------------------------------------------------------------------------
# SQLite 增量同步状态（collectors.mail.state.SyncStateStore 的实现）
# ---------------------------------------------------------------------------

def test_storage_sync_state_store_roundtrip(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    store = StorageSyncStateStore(storage)
    assert store.get("account:abc", "INBOX") is None

    state = MailSyncState(
        account_key="account:abc",
        mailbox="INBOX",
        uidvalidity=99,
        next_uid=42,
        last_synced_at=now_local(),
    )
    store.set(state)
    # 新实例（模拟重启）仍能读取；INBOX 大小写不敏感
    reloaded = StorageSyncStateStore(storage).get("account:abc", "inbox")
    assert reloaded == state

    store.set(
        MailSyncState(
            account_key="account:abc", mailbox="INBOX", uidvalidity=99, next_uid=50
        )
    )
    assert store.get("account:abc", "INBOX").next_uid == 50

    store.clear("account:abc")
    assert store.get("account:abc", "INBOX") is None


def test_storage_sync_state_store_ignores_invalid_objects(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    store = StorageSyncStateStore(storage)
    store.set(object())  # 不符合协议：忽略而不是崩溃
    assert store.get("account:abc", "INBOX") is None


# ---------------------------------------------------------------------------
# 服务内置接线
# ---------------------------------------------------------------------------

def test_service_wires_real_mail_collector_without_network() -> None:
    service = make_service()
    try:
        account = service.add_mail_account("demo@example.com", "我的邮箱")
        conversations = service.list_conversations(account.id)
        assert len(conversations) == 1
        assert conversations[0].kind is ConversationKind.MAIL
        assert conversations[0].external_key == "INBOX"

        service.set_mail_credential("demo@example.com", "abcdefghijklmnop")
        # 凭据写入的 key 与真实 MailCollector 读取的 key 必须一致
        credentials = service.credential_store
        assert credentials.get_secret(collector_mail_key("demo@example.com")) == "abcdefghijklmnop"

        source = service._make_source(account)
        assert isinstance(source, MailCollector)
        assert source._credential_key == collector_mail_key("demo@example.com")
        assert "INBOX" in source._folders
        assert source.close() is None
    finally:
        service.shutdown()


def test_service_wires_qq_collector_through_worker_subprocess() -> None:
    """QQ 必须走子进程适配器；父进程不直接构造/import QqMessageSource。"""
    from social_secretary.core.collector_worker import WorkerQQMessageSource

    service = make_service()
    try:
        account = service.add_qq_account("QQ 测试", ["张三"])
        source = service._make_source(account)
        assert isinstance(source, WorkerQQMessageSource)
        assert source.allowlist == ("张三",)
        assert any(
            "social_secretary.core.collector_worker" in part
            for part in source.worker_command
        )
        source.close()
    finally:
        service.shutdown()


def test_service_mail_credential_lifecycle_uses_collector_key() -> None:
    service = make_service()
    try:
        service.add_mail_account("user@example.com", "邮箱")
        assert service.has_mail_credential("user@example.com") is False
        service.set_mail_credential("user@example.com", "abcdefghijklmnop")
        assert service.has_mail_credential("user@example.com") is True
        assert service.credential_store.has_secret(collector_mail_key("user@example.com"))
        service.delete_mail_credential("user@example.com")
        assert service.has_mail_credential("user@example.com") is False
        # 非法邮箱：布尔接口返回 False，不抛异常
        assert service.has_mail_credential("not-an-email") is False
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 邮件扫描端到端（fake MessageSource，走真实 pipeline/dedupe/storage）
# ---------------------------------------------------------------------------

class FakeMailSource:
    """只读邮件 source fake：RawMessage.conversation_key 用 conversation.id（与真实采集器一致）。"""

    kind = AccountKind.EMAIL

    def __init__(self) -> None:
        self.fetched_conversations: list[str] = []
        self.closed = False

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(ok=True, detail="fake IMAP 连接成功（只读 EXAMINE）")

    def list_conversations(self):
        return []

    def fetch_messages(self, conversation, *, since=None, limit=100, foreground_ack=False):
        self.fetched_conversations.append(conversation.id)
        return [
            RawMessage(
                conversation_key=conversation.id,
                sender="项目经理",
                is_self=False,
                text="请明天上午10点前提交周报",
                timestamp=now_local(),
                timestamp_source=TimestampSource.UI,
                raw_time_text="今天 09:00",
                external_id="imap:1:1",
                source_locator="IMAP INBOX UID 1 (UIDVALIDITY 7)",
            )
        ]

    def close(self) -> None:
        self.closed = True


def test_mail_scan_end_to_end_with_fake_source() -> None:
    holder: dict[str, FakeMailSource] = {}

    def factory(account):
        source = FakeMailSource()
        holder["source"] = source
        return source

    service = make_service(source_factory=factory)
    try:
        account = service.add_mail_account("demo@example.com", "我的邮箱")
        service.grant_consent(ConsentScope.MAIL_READ)
        service.resume_collection()

        results = service.run_scan(account.id)
        assert len(results) == 1
        assert results[0].status is ScanStatus.OK
        assert results[0].messages_new == 1
        assert holder["source"].fetched_conversations == [
            service.list_conversations(account.id)[0].id
        ]

        messages = service.list_messages()
        assert len(messages) == 1
        message = messages[0]
        assert message.conversation_kind is ConversationKind.MAIL

        detail = service.get_source_detail(message.id)
        assert detail.account_kind is AccountKind.EMAIL
        assert "邮箱" in detail.origin_label
        assert detail.raw_time_text == "今天 09:00"
        assert detail.external_id == "imap:1:1"

        # 第二次扫描：external_id + content_hash 双层幂等
        second = service.run_scan(account.id)
        assert second[0].messages_new == 0
        assert len(service.list_messages()) == 1

        # 邮件待办由 intel 规则/或测试注入实现提取（此处仅断言消息与状态）
        assert service.collection_status()["last_scan_status"] == "ok"
    finally:
        service.shutdown()


def test_test_mail_credential_with_fake_source() -> None:
    def factory(account):
        return FakeMailSource()

    service = make_service(source_factory=factory)
    try:
        service.add_mail_account("demo@example.com", "我的邮箱")
        # 未保存授权码：直接失败，不构造采集器
        result = service.test_mail_credential("demo@example.com")
        assert result.ok is False
        assert "授权码" in result.detail

        service.set_mail_credential("demo@example.com", "abcdefghijklmnop")
        result = service.test_mail_credential("demo@example.com")
        assert result.ok is True
    finally:
        service.shutdown()
