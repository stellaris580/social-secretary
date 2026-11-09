"""P2 B/C：消息手动分类（set_message_category）测试。

只改本地分类，不写回 QQ/邮箱；重复采集不得覆盖用户手动分类。
"""

from __future__ import annotations

import json

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ConsentScope,
    MessageWorkStatus,
    ScanStatus,
)
from social_secretary.core.service import NotFoundError, ServiceError
from social_secretary.core.storage import Storage, now_local
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import (
    FakeMessageSource,
    make_account,
    make_conversation,
    make_message,
    make_raw,
    make_service,
)


def _service_with_message():
    service = make_service(credential_store=InMemoryCredentialStore())
    account = service.add_mail_account("cat@example.com", "分类邮箱")
    conversation = service.storage.list_conversations(account.id)[0]
    message = make_message(
        conversation,
        "请明天提交周报，正文标记CAT9Q7",
        timestamp=now_local(),
        category=Category.NORMAL,
    )
    service.storage.insert_message(message)
    return service, account, conversation, message


def test_set_message_category_updates_local_and_audit_no_body() -> None:
    service, _account, conversation, message = _service_with_message()
    try:
        service.set_message_category(message.id, Category.IMPORTANT)
        loaded = service.storage.get_message(message.id)
        assert loaded.category is Category.IMPORTANT

        # 手动分类后 list_messages 分类过滤立即生效
        assert [item.id for item in service.list_messages(category=Category.IMPORTANT)] == [
            message.id
        ]
        assert service.list_messages(category=Category.FYI) == []

        audit = json.dumps(service.storage.list_audit(), ensure_ascii=False)
        assert "message_category_changed" in audit
        assert "important" in audit
        assert message.id in audit
        assert "正文标记CAT9Q7" not in audit
        assert conversation.title not in audit
    finally:
        service.shutdown()


def test_set_message_category_invalid_inputs() -> None:
    service, _account, _conversation, message = _service_with_message()
    try:
        with pytest.raises(NotFoundError):
            service.set_message_category("missing-id", Category.FYI)
        with pytest.raises(NotFoundError):
            service.set_message_category("", Category.FYI)
        with pytest.raises(ServiceError):
            service.set_message_category(message.id, "not-a-category")  # type: ignore[arg-type]
        # 非法输入不改变原分类
        assert service.storage.get_message(message.id).category is Category.NORMAL
    finally:
        service.shutdown()


def test_set_message_category_clears_summary_cache() -> None:
    service, _account, _conversation, message = _service_with_message()
    try:
        day = message.timestamp.date()
        service.generate_daily_summary(day=day)
        assert service.get_cached_summary(day) is not None
        service.set_message_category(message.id, Category.FYI)
        assert service.get_cached_summary(day) is None
    finally:
        service.shutdown()


def test_rescan_does_not_overwrite_manual_category() -> None:
    data_dir_service = make_service(credential_store=InMemoryCredentialStore())
    try:
        account = data_dir_service.add_mail_account("rescan-cat@example.com", "分类邮箱")
        conversation = data_dir_service.storage.list_conversations(account.id)[0]
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
        data_dir_service.shutdown()

        service = make_service(
            credential_store=InMemoryCredentialStore(),
            source_factory=lambda item: source,
        )
        try:
            service.grant_consent(ConsentScope.MAIL_READ)
            service.resume_collection()
            first = service.run_scan(account.id)
            assert first[0].status is ScanStatus.OK
            message = service.list_messages()[0]
            service.set_message_category(message.id, Category.IMPORTANT)
            service.set_message_work_status(message.id, MessageWorkStatus.DONE)

            second = service.run_scan(account.id)
            assert second[0].messages_new == 0
            loaded = service.storage.get_message(message.id)
            assert loaded.category is Category.IMPORTANT
            assert len(service.list_messages()) == 1
        finally:
            service.shutdown()
    finally:
        try:
            data_dir_service.shutdown()
        except Exception:
            pass


def test_category_persists_across_storage_reopen(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-cat", account_id=account.id, title="INBOX", external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = make_message(conversation, "分类持久化", timestamp=now_local())
    storage.insert_message(message)
    assert storage.update_message_category(message.id, Category.FYI) is True

    reopened = Storage(tmp_path / "db.sqlite")
    reopened.initialize()
    assert reopened.get_message(message.id).category is Category.FYI
    assert reopened.update_message_category("missing", Category.FYI) is False
