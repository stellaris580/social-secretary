"""只读 IMAP 采集器集成测试（端到端跑本地 mock IMAP 服务器）。

覆盖：首次同步、UID 增量、UIDVALIDITY 变化重同步、认证失败、超时、断线、连接拒绝、
HTML/base64/quoted-printable 解析、无 Date 头、超大邮件跳过、limit 分批、since 过滤、
无写命令断言、BODY.PEEK 断言、重复/并发调用幂等、只读命令护栏、错误信息脱敏。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from social_secretary.collectors.mail import (
    MailAuthError,
    MailCollector,
    MailCollectorClosedError,
    MailConnectionError,
    MailCredentialMissingError,
    MailFolderNotFoundError,
    MailTimeoutError,
    MessageSource,
    ReadOnlyIMAP4,
    ReadOnlyViolation,
    mail_credential_key,
)
from social_secretary.contracts import (
    Account,
    AccountKind,
    Conversation,
    ConversationKind,
    TimestampSource,
)

from .conftest import unused_tcp_port
from .helpers import make_email

TZ = timezone(timedelta(hours=8))


def _conversation(collector: MailCollector) -> Conversation:
    return collector.list_conversations()[0]


def _collector_for(
    server,
    credentials,
    state_store,
    account,
    *,
    password: str | None = "mock-auth-code",
    **overrides,
) -> MailCollector:
    if password is not None:
        credentials.set_secret(mail_credential_key(account.identifier), password)
    kwargs = {
        "account": account,
        "credential_store": credentials,
        "sync_state": state_store,
        "host": "127.0.0.1",
        "port": server.port,
        "insecure_plaintext": True,
        "timeout": 2.0,
    }
    kwargs.update(overrides)
    return MailCollector(**kwargs)


# ---------------------------------------------------------------------------
# 协议/接口层
# ---------------------------------------------------------------------------

def test_collector_implements_message_source_protocol(collector, conversations):
    assert isinstance(collector, MessageSource)
    assert collector.kind == AccountKind.EMAIL
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.kind == ConversationKind.MAIL
    assert conversation.external_key == "INBOX"
    assert conversation.title == "收件箱"
    assert conversation.account_id == "acc-mail-1"
    assert conversation.id == "mail:acc-mail-1:INBOX"


def test_test_connection_ok(collector, mock_server, mailbox, mock_module):
    mailbox.messages.append(mock_module.MockMessage(uid=1, raw=make_email()))

    check = collector.test_connection()

    assert check.ok is True
    assert check.latency_ms is not None and check.latency_ms >= 0
    assert check.discovered["readonly"] is True
    assert check.discovered["uidvalidity"] == 100
    assert check.discovered["message_count"] == 1
    assert "IMAP4REV1" in check.discovered["capabilities"]
    assert "只读" in check.detail
    assert mock_server.write_attempts == []
    assert "EXAMINE" in mock_server.verbs


def test_test_connection_auth_failure_returns_check(make_collector):
    collector = make_collector(password="wrong-auth-code")
    check = collector.test_connection()
    assert check.ok is False
    assert check.discovered["code"] == "MAIL_AUTH_FAILED"
    assert "授权码" in check.detail


# ---------------------------------------------------------------------------
# 首次同步 / 增量同步 / UIDVALIDITY
# ---------------------------------------------------------------------------

def test_first_sync_parses_messages_and_advances_state(
    collector, mock_server, state_store, mailbox, mock_module
):
    mailbox.messages.extend(
        [
            mock_module.MockMessage(
                uid=1,
                raw=make_email(
                    subject="第一封：周会",
                    body="下午三点周会。",
                    from_addr="alice@example.com",
                    from_name="Alice",
                    date="Mon, 27 Oct 2025 10:00:00 +0800",
                    message_id="<first@example.com>",
                ),
            ),
            mock_module.MockMessage(
                uid=2,
                raw=make_email(
                    subject="第二封：报销",
                    body="请提交发票。",
                    from_addr="bob@example.com",
                    from_name="Bob",
                    date="Tue, 28 Oct 2025 11:00:00 +0800",
                    message_id="<second@example.com>",
                ),
            ),
            mock_module.MockMessage(
                uid=3,
                raw=make_email(
                    subject="第三封：附件",
                    body="见附件。",
                    from_addr="carol@example.com",
                    from_name="Carol",
                    date="Wed, 29 Oct 2025 12:00:00 +0800",
                    message_id="<third@example.com>",
                    attachment=("合成.docx", b"x" * 100, "application", "vnd.openxmlformats"),
                ),
            ),
        ]
    )
    conversation = _conversation(collector)

    messages = collector.fetch_messages(conversation)

    assert [message.external_id for message in messages] == [
        "<first@example.com>",
        "<second@example.com>",
        "<third@example.com>",
    ]
    first = messages[0]
    assert first.conversation_key == conversation.id
    assert first.sender == "Alice"
    assert first.is_self is False
    assert "第一封：周会" in first.text
    assert "下午三点周会。" in first.text
    assert first.timestamp is not None and first.timestamp.utcoffset() == timedelta(hours=8)
    assert first.timestamp_source == TimestampSource.UI
    assert first.raw_time_text == "Mon, 27 Oct 2025 10:00:00 +0800"
    assert first.source_locator == "IMAP INBOX UID 1 (UIDVALIDITY 100)"
    assert first.extras["subject"] == "第一封：周会"
    assert first.extras["from_address"] == "alice@example.com"
    assert first.extras["to"] == ["test@qq.com"]
    assert first.extras["uid"] == 1
    assert messages[2].extras["attachment_count"] == 1
    assert messages[2].extras["attachment_bytes"] == 100
    # 附件文件名只读展示（P0-19）：collector.extras 携带已清洗的文件名，不含附件内容
    assert messages[2].extras["attachment_names"] == ["合成.docx"]
    assert first.extras["attachment_names"] == []

    state = state_store.get("account:acc-mail-1", "INBOX")
    assert state is not None
    assert state.uidvalidity == 100
    assert state.next_uid == 4
    assert state.last_synced_at is not None
    assert state.last_synced_at.tzinfo is not None
    assert collector.last_stats["messages_new"] == 3


def test_second_sync_only_fetches_new_uids(collector, mock_server, mailbox, mock_module, state_store):
    mailbox.messages.append(mock_module.MockMessage(uid=1, raw=make_email(subject="旧邮件")))
    conversation = _conversation(collector)
    first = collector.fetch_messages(conversation)
    assert len(first) == 1

    mock_server.clear_log()
    mailbox.messages.append(mock_module.MockMessage(uid=2, raw=make_email(subject="新邮件")))
    second = collector.fetch_messages(conversation)

    assert [message.external_id for message in second] == ["<synthetic-001@example.com>"]
    # 只 FETCH 了 UID 2 的正文；UID 1 不在任何 FETCH 记录里。
    assert mock_server.body_fetch_uids == [2]
    for record in mock_server.fetch_sets:
        if any(item.startswith("BODY") for item in record.items):
            assert record.uids == (2,)
    state = state_store.get("account:acc-mail-1", "INBOX")
    assert state.next_uid == 3


def test_no_new_mail_issues_no_body_fetch(collector, mock_server, mailbox, mock_module):
    for uid in (1, 2, 3):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"邮件 {uid}"))
        )
    conversation = _conversation(collector)
    assert len(collector.fetch_messages(conversation)) == 3

    mock_server.clear_log()
    messages = collector.fetch_messages(conversation)

    assert messages == []
    # RFC 3501 的 3:* 怪癖会让服务器返回最后一封；采集器必须过滤掉，且绝不重复 FETCH 正文。
    assert mock_server.fetch_sets == []
    assert mock_server.body_fetch_uids == []


def test_uidvalidity_change_triggers_full_resync(
    collector, mock_server, mailbox, mock_module, state_store
):
    mailbox.messages.extend(
        [
            mock_module.MockMessage(uid=1, raw=make_email(subject="旧 A")),
            mock_module.MockMessage(uid=2, raw=make_email(subject="旧 B")),
        ]
    )
    conversation = _conversation(collector)
    assert len(collector.fetch_messages(conversation)) == 2
    assert state_store.get("account:acc-mail-1", "INBOX").next_uid == 3

    mock_server.clear_log()
    mock_server.replace_messages(
        [
            make_email(subject="重建后 A", message_id="<a2@example.com>"),
            make_email(subject="重建后 B", message_id="<b2@example.com>"),
            make_email(subject="重建后 C", message_id="<c2@example.com>"),
        ],
        uidvalidity=200,
    )

    messages = collector.fetch_messages(conversation)

    assert [message.external_id for message in messages] == [
        "<a2@example.com>",
        "<b2@example.com>",
        "<c2@example.com>",
    ]
    assert any("UIDVALIDITY" in warning for warning in collector.last_warnings)
    state = state_store.get("account:acc-mail-1", "INBOX")
    assert state.uidvalidity == 200
    assert state.next_uid == 4
    assert mock_server.body_fetch_uids == [1, 2, 3]


def test_limit_batches_do_not_lose_or_duplicate(collector, state_store, mailbox, mock_module):
    for uid in range(1, 6):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"第 {uid} 封"))
        )
    conversation = _conversation(collector)

    batch1 = collector.fetch_messages(conversation, limit=2)
    batch2 = collector.fetch_messages(conversation, limit=2)
    batch3 = collector.fetch_messages(conversation, limit=2)
    batch4 = collector.fetch_messages(conversation, limit=2)

    assert [message.extras["uid"] for message in batch1] == [1, 2]
    assert [message.extras["uid"] for message in batch2] == [3, 4]
    assert [message.extras["uid"] for message in batch3] == [5]
    assert batch4 == []
    assert state_store.get("account:acc-mail-1", "INBOX").next_uid == 6


def test_since_filter_advances_state(collector, state_store, mailbox, mock_module):
    mailbox.messages.extend(
        [
            mock_module.MockMessage(
                uid=1, raw=make_email(subject="旧", date="Mon, 20 Oct 2025 09:00:00 +0800")
            ),
            mock_module.MockMessage(
                uid=2, raw=make_email(subject="新", date="Thu, 30 Oct 2025 09:00:00 +0800")
            ),
        ]
    )
    conversation = _conversation(collector)
    since = datetime(2025, 10, 25, 0, 0, tzinfo=TZ)

    messages = collector.fetch_messages(conversation, since=since)

    assert [message.extras["uid"] for message in messages] == [2]
    assert collector.last_stats["skipped_before_since"] == 1
    # 被 since 过滤的邮件也计入"已处理"，否则下次会反复读取。
    assert state_store.get("account:acc-mail-1", "INBOX").next_uid == 3


# ---------------------------------------------------------------------------
# 降级 / 错误路径
# ---------------------------------------------------------------------------

def test_missing_date_header_marks_collected(collector, mailbox, mock_module):
    mailbox.messages.append(mock_module.MockMessage(uid=1, raw=make_email(omit_date=True)))
    messages = collector.fetch_messages(_conversation(collector))
    assert messages[0].timestamp is None
    assert messages[0].timestamp_source == TimestampSource.COLLECTED
    assert any("Date" in warning for warning in collector.last_warnings)


def test_oversize_message_skipped_but_state_advances(
    collector, state_store, mock_server, mailbox, mock_module, credentials, account
):
    mailbox.messages.extend(
        [
            mock_module.MockMessage(uid=1, raw=make_email(subject="小邮件", body="hi")),
            mock_module.MockMessage(uid=2, raw=make_email(subject="巨型邮件", body="长" * 5000)),
        ]
    )
    conversation = _conversation(collector)
    collector_small_limit = _collector_for(
        mock_server, credentials, state_store, account, max_message_bytes=800
    )

    messages = collector_small_limit.fetch_messages(conversation)

    assert [message.extras["uid"] for message in messages] == [1]
    assert any("大小上限" in warning for warning in collector_small_limit.last_warnings)
    assert collector_small_limit.last_stats["skipped_oversize"] == 1
    # 超大邮件只取了 RFC822.SIZE，没有下载正文。
    assert mock_server.body_fetch_uids == [1]
    assert state_store.get("account:acc-mail-1", "INBOX").next_uid == 3


def test_auth_failure_raises_and_never_leaks_secret(
    mock_server, credentials, state_store, account, caplog
):
    secret = "WRONG-AUTHCODE-SECRET-123"
    collector_with_bad_code = _collector_for(
        mock_server, credentials, state_store, account, password=secret
    )
    conversation = collector_with_bad_code.list_conversations()[0]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MailAuthError) as exc_info:
            collector_with_bad_code.fetch_messages(conversation)

    assert exc_info.value.code.value == "MAIL_AUTH_FAILED"
    assert secret not in str(exc_info.value)
    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
    assert mock_server.login_attempts == 1
    assert mock_server.write_attempts == []


def test_missing_credential_does_not_connect(make_collector, mock_server):
    collector = make_collector(password=None)
    with pytest.raises(MailCredentialMissingError):
        collector.fetch_messages(_conversation(collector))
    assert mock_server.connection_count == 0


def test_timeout_maps_to_mail_timeout(start_mock, credentials, state_store, account, caplog):
    server = start_mock(greeting_delay=10.0)
    collector = _collector_for(server, credentials, state_store, account, timeout=0.3)
    started = time.perf_counter()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MailTimeoutError):
            collector.fetch_messages(_conversation(collector))
    assert time.perf_counter() - started < 5.0
    assert server.write_attempts == []


def test_connection_refused_maps_to_connection_error(
    credentials, state_store, account, caplog
):
    port = unused_tcp_port()
    credentials.set_secret(mail_credential_key(account.identifier), "mock-auth-code")
    collector = MailCollector(
        account=account,
        credential_store=credentials,
        sync_state=state_store,
        host="127.0.0.1",
        port=port,
        insecure_plaintext=True,
        timeout=1.0,
    )
    # DSH 网络沙箱下，连接本机关闭端口可能得到 RST（连接被拒绝）也可能被静默丢弃（超时），
    # 两种都必须映射成稳定的网络类错误码，而不是裸 OSError/TimeoutError。
    with pytest.raises((MailConnectionError, MailTimeoutError)) as exc_info:
        collector.fetch_messages(_conversation(collector))
    assert exc_info.value.code.value in {"MAIL_CONNECTION_FAILED", "MAIL_TIMEOUT"}


def test_translate_exception_maps_connection_refused(collector):
    """单元层面确认 ConnectionRefusedError → MAIL_CONNECTION_FAILED（不依赖本地网络行为）。"""
    error = collector._translate_exception(ConnectionRefusedError(10061, "连接被拒绝"))  # noqa: SLF001
    assert isinstance(error, MailConnectionError)
    assert error.code.value == "MAIL_CONNECTION_FAILED"


def test_server_disconnect_after_login(start_mock, credentials, state_store, account, mailbox):
    server = start_mock(mailbox=mailbox, drop_after_login=True)
    collector = _collector_for(server, credentials, state_store, account, timeout=1.0)
    with pytest.raises(MailConnectionError):
        collector.fetch_messages(_conversation(collector))


def test_folder_not_allowed(collector, account):
    conversation = Conversation(
        id="mail:acc-mail-1:Sent",
        account_id=account.id,
        kind=ConversationKind.MAIL,
        title="已发送",
        external_key="Sent",
    )
    with pytest.raises(MailFolderNotFoundError):
        collector.fetch_messages(conversation)


def test_limit_validation(collector):
    conversation = _conversation(collector)
    with pytest.raises(ValueError):
        collector.fetch_messages(conversation, limit=0)


def test_invalid_account_identifier_raises(credentials, state_store):
    bad_account = Account(id="x", kind=AccountKind.EMAIL, display_name="坏账号", identifier="not-an-email")
    with pytest.raises(ValueError):
        MailCollector(account=bad_account, credential_store=credentials, sync_state=state_store)


def test_insecure_plaintext_only_for_loopback(credentials, state_store, account):
    with pytest.raises(ValueError):
        MailCollector(
            account=account,
            credential_store=credentials,
            sync_state=state_store,
            host="imap.qq.com",
            insecure_plaintext=True,
        )


# ---------------------------------------------------------------------------
# 只读性 / 幂等 / 关闭
# ---------------------------------------------------------------------------

def test_only_read_commands_and_peek_fetch(collector, mock_server, mailbox, mock_module):
    for uid in (1, 2):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"只读校验 {uid}"))
        )
    collector.fetch_messages(_conversation(collector))

    allowed_verbs = {"CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT"}
    assert set(mock_server.verbs) <= allowed_verbs, set(mock_server.verbs)
    assert mock_server.write_attempts == []
    assert mock_server.non_peek_fetch_uids == []
    for message in mailbox.messages:
        assert message.flags == ()


def test_session_guard_blocks_write_apis(mock_server):
    conn = ReadOnlyIMAP4("127.0.0.1", mock_server.port, timeout=2)
    try:
        conn.login("test@qq.com", "mock-auth-code")
        conn.select("INBOX", readonly=True)
        with pytest.raises(ReadOnlyViolation):
            conn.store("1", "+FLAGS", "(\\Seen)")
        with pytest.raises(ReadOnlyViolation):
            conn.expunge()
        with pytest.raises(ReadOnlyViolation):
            conn.append("INBOX", None, None, b"should never be sent")
        with pytest.raises(ReadOnlyViolation):
            conn.copy("1", "Sent")
        with pytest.raises(ReadOnlyViolation):
            conn.uid("STORE", "1", "+FLAGS", "(\\Seen)")
        with pytest.raises(ReadOnlyViolation):
            conn.uid("FETCH", "1", "(BODY[])")
        with pytest.raises(ReadOnlyViolation):
            conn.uid("EXPUNGE")
    finally:
        conn.logout()
    assert mock_server.write_attempts == []


def test_repeated_calls_are_idempotent(collector, mailbox, mock_module):
    for uid in (1, 2, 3):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"幂等 {uid}"))
        )
    conversation = _conversation(collector)
    first = collector.fetch_messages(conversation)
    second = collector.fetch_messages(conversation)
    third = collector.fetch_messages(conversation)
    assert len(first) == 3
    assert second == []
    assert third == []


def test_concurrent_calls_do_not_duplicate(collector, mailbox, mock_module):
    for uid in (1, 2, 3):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"并发 {uid}"))
        )
    conversation = _conversation(collector)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(collector.fetch_messages, conversation),
            pool.submit(collector.fetch_messages, conversation),
        ]
        batches = [future.result(timeout=15) for future in futures]
    combined = [message for batch in batches for message in batch]
    assert len(combined) == 3
    assert len({message.extras["uid"] for message in combined}) == 3


def test_close_semantics(collector):
    conversation = _conversation(collector)
    collector.close()
    collector.close()  # 可重复调用
    with pytest.raises(MailCollectorClosedError):
        collector.fetch_messages(conversation)


def test_fetch_limit_cap_limits_each_sync(make_collector, state_store, mailbox, mock_module):
    """fetch_limit_cap 把单次同步封顶（验收工具用它实现"最近 N 封"）。"""
    for uid in range(1, 7):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"封顶 {uid}"))
        )
    collector = make_collector(fetch_limit_cap=2)
    conversation = _conversation(collector)

    first = collector.fetch_messages(conversation, limit=100)
    second = collector.fetch_messages(conversation, limit=100)
    third = collector.fetch_messages(conversation, limit=100)
    fourth = collector.fetch_messages(conversation, limit=100)

    assert [message.extras["uid"] for message in first] == [1, 2]
    assert [message.extras["uid"] for message in second] == [3, 4]
    assert [message.extras["uid"] for message in third] == [5, 6]
    assert fourth == []
    assert state_store.get("account:acc-mail-1", "INBOX").next_uid == 7


def test_initial_sync_newest_reads_latest_and_skips_backlog(
    make_collector, state_store, mailbox, mock_module
):
    """initial_sync_newest=True：首次同步读最近 N 封并跳过更早积压，之后正常增量。"""
    for uid in range(1, 7):
        mailbox.messages.append(
            mock_module.MockMessage(uid=uid, raw=make_email(subject=f"最近 {uid}"))
        )
    collector = make_collector(fetch_limit_cap=2, initial_sync_newest=True)
    conversation = _conversation(collector)

    first = collector.fetch_messages(conversation, limit=100)
    assert [message.extras["uid"] for message in first] == [5, 6]
    assert state_store.get("account:acc-mail-1", "INBOX").next_uid == 7
    assert any("最近 2 封" in warning for warning in collector.last_warnings)
    assert collector.last_stats["newest_first"] is True

    assert collector.fetch_messages(conversation, limit=100) == []
    mailbox.messages.append(
        mock_module.MockMessage(uid=7, raw=make_email(subject="最新增量"))
    )
    third = collector.fetch_messages(conversation, limit=100)
    assert [message.extras["uid"] for message in third] == [7]


def test_invalid_fetch_limit_cap_rejected(credentials, state_store):
    with pytest.raises(ValueError):
        MailCollector(
            account=Account(
                id="acc",
                kind=AccountKind.EMAIL,
                display_name="x",
                identifier="user@qq.com",
            ),
            credential_store=credentials,
            sync_state=state_store,
            fetch_limit_cap=0,
        )
