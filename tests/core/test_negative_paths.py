"""负向路径回归：无 Key / 断网 / 授权失败 / QQ 不可用 / Keyring 不可用 —— 绝不误报成功。

对应 task-10（P0-4）。真实网络场景（真实 imap.qq.com / 真实 worker）由
``tools/negative_acceptance.py`` 执行并写 evidence；本文件保证这些负向行为在 CI 离线可回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from social_secretary.contracts import (
    AccountKind,
    ConnectionCheck,
    ConsentScope,
    ScanStatus,
)
from social_secretary.core.service import ServiceError
from social_secretary.security.consent import ConsentRequired
from social_secretary.security.credential_store import (
    CredentialStoreError,
    InMemoryCredentialStore,
    KeyringCredentialStore,
)
from tests.core.fakes import make_conversation, make_service

API_KEY = "sk-testplaceholder1234567890"
WRONG_AUTH_CODE = "abcdefghijklmnop"
FAKE_WORKER = Path(__file__).with_name("fake_collector_worker.py")
FAKE_COMMAND = [sys.executable, str(FAKE_WORKER)]


# ---------------------------------------------------------------------------
# fakes：邮件认证失败 / 网络不可达 / keyring 损坏
# ---------------------------------------------------------------------------

class _AuthFailSource:
    kind = AccountKind.EMAIL

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(
            ok=False,
            detail="邮箱登录失败：授权码可能已失效或被重置。请重新生成授权码。",
            latency_ms=380,
            discovered={"code": "MAIL_AUTH_FAILED"},
        )

    def list_conversations(self):
        return []

    def fetch_messages(self, conversation, **kwargs):
        from social_secretary.collectors.mail.errors import MailAuthError

        raise MailAuthError("认证失败（负向测试）")

    def close(self) -> None:
        return None


class _UnreachableMailSource(_AuthFailSource):
    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(
            ok=False,
            detail="无法连接 QQ 邮箱服务器（imap.qq.com:993）。请检查网络连接，或稍后再试。",
            latency_ms=20,
            discovered={"code": "MAIL_CONNECTION_FAILED"},
        )

    def fetch_messages(self, conversation, **kwargs):
        from social_secretary.collectors.mail.errors import MailConnectionError

        raise MailConnectionError("连接被拒绝（负向测试）")


class _BrokenKeyring:
    def set_password(self, *args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    def get_password(self, *args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    def delete_password(self, *args, **kwargs):
        raise RuntimeError("keyring backend unavailable")


def _prepare_mail_account(service):
    account = service.add_mail_account("neg@example.com", "负向邮箱")
    service.set_mail_credential("neg@example.com", WRONG_AUTH_CODE)
    service.grant_consent(ConsentScope.MAIL_READ)
    service.resume_collection()
    return account


# ---------------------------------------------------------------------------
# 邮箱：认证失败 / 网络失败
# ---------------------------------------------------------------------------

def test_mail_auth_failure_scan_failed_no_messages_and_summary_not_misleading() -> None:
    service = make_service(source_factory=lambda account: _AuthFailSource())
    try:
        account = _prepare_mail_account(service)
        results = service.run_scan(account.id)
        assert len(results) == 1
        assert results[0].status is ScanStatus.FAILED
        assert results[0].messages_new == 0
        assert "MAIL_AUTH_FAILED" in (results[0].error or "")
        assert service.list_messages() == []
        assert service.storage.count_messages() == 0

        status = service.collection_status()
        assert status["last_scan_status"] == "failed"
        assert status["last_error"]

        summary = service.generate_daily_summary()
        assert summary.scan_status is ScanStatus.FAILED
        assert any("采集存在失败" in caveat for caveat in summary.caveats)
        # 关键：不能把「采集失败导致的 0 消息」说成「今天没有新消息」
        assert "没有新的消息需要整理" not in summary.narrative
        assert "不代表今天没有新消息" in summary.narrative
    finally:
        service.shutdown()


def test_mail_auth_failure_test_credential_is_structured() -> None:
    service = make_service(source_factory=lambda account: _AuthFailSource())
    try:
        _prepare_mail_account(service)
        check = service.test_mail_credential("neg@example.com")
        assert check.ok is False
        assert "登录失败" in check.detail
        assert check.discovered.get("code") == "MAIL_AUTH_FAILED"
    finally:
        service.shutdown()


def test_mail_network_failure_structured_and_zero_messages() -> None:
    service = make_service(source_factory=lambda account: _UnreachableMailSource())
    try:
        account = _prepare_mail_account(service)
        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.FAILED
        assert results[0].messages_new == 0
        assert "MAIL_CONNECTION_FAILED" in (results[0].error or "")
        assert service.list_messages() == []
        check = service.test_mail_credential("neg@example.com")
        assert check.ok is False and "无法连接" in check.detail
    finally:
        service.shutdown()


def test_real_mail_collector_reports_unreachable_host(tmp_path) -> None:
    """真实 MailCollector 连接 127.0.0.1:1 → 结构化网络失败（不依赖外网）。"""
    mail_pkg = pytest.importorskip("social_secretary.collectors.mail")
    from dataclasses import replace

    from social_secretary.collectors.mail.errors import MailCollectorError
    from social_secretary.collectors.mail.state import MemorySyncStateStore
    from social_secretary.contracts import Account, ConversationKind

    credentials = InMemoryCredentialStore()
    email = "neg@example.com"
    credentials.set_secret(mail_pkg.mail_credential_key(email), WRONG_AUTH_CODE)
    account = Account(
        id="acc-unreachable",
        kind=AccountKind.EMAIL,
        display_name="不可达邮箱",
        identifier=email,
    )
    collector = mail_pkg.MailCollector(
        account=account,
        credential_store=credentials,
        sync_state=MemorySyncStateStore(),
        folders=["INBOX"],
        host="127.0.0.1",
        port=1,
        insecure_plaintext=True,
        timeout=5,
    )
    try:
        check = collector.test_connection()
        assert check.ok is False
        assert isinstance(check.detail, str) and check.detail
        assert str(check.discovered.get("code", "")).startswith("MAIL_")
        conversation = make_conversation(
            "conv-mail", account_id=account.id, title="INBOX", external_key="INBOX"
        )
        conversation = replace(conversation, kind=ConversationKind.MAIL)
        with pytest.raises(MailCollectorError):
            collector.fetch_messages(conversation, limit=10)
    finally:
        collector.close()


# ---------------------------------------------------------------------------
# 外部 AI：无 Key / 无同意 / 不可达 / 适配器缺失
# ---------------------------------------------------------------------------

def test_external_ai_without_key_fails_explicitly_and_saves_no_drafts() -> None:
    service = make_service()
    try:
        service.load_demo_data()
        service.grant_consent(ConsentScope.EXTERNAL_AI)
        with pytest.raises(ServiceError) as excinfo:
            service.generate_drafts("demo-msg-3", use_external=True)
        message = str(excinfo.value)
        assert "API Key" in message or "Key" in message
        assert service.list_drafts("demo-msg-3") == []
    finally:
        service.shutdown()


def test_external_ai_without_consent_blocked_before_any_call() -> None:
    service = make_service()
    try:
        service.load_demo_data()
        with pytest.raises(ConsentRequired):
            service.generate_drafts("demo-msg-3", use_external=True)
        assert service.list_drafts("demo-msg-3") == []
    finally:
        service.shutdown()


def test_external_ai_unreachable_network_fails_structured() -> None:
    service = make_service()
    try:
        service.load_demo_data()
        service.set_external_ai_key(API_KEY)
        service.update_settings(
            external_ai_base_url="http://127.0.0.1:1/v1",
            external_ai_model="negative-test-model",
        )
        service.grant_consent(ConsentScope.EXTERNAL_AI)

        with pytest.raises(ServiceError) as excinfo:
            service.generate_drafts("demo-msg-3", use_external=True)
        assert "外部 AI" in str(excinfo.value)
        assert service.list_drafts("demo-msg-3") == []

        check = service.test_external_ai()
        assert check.ok is False
        assert "外部 AI" in check.detail
        # 失败不产生成功审计
        actions = {event["action"] for event in service.storage.list_audit()}
        assert "drafts_generated_external" not in actions
    finally:
        service.shutdown()


def test_external_ai_provider_missing_after_consent_and_key() -> None:
    service = make_service()
    try:
        service.load_demo_data()
        service.set_external_ai_key(API_KEY)
        service.grant_consent(ConsentScope.EXTERNAL_AI)
        with pytest.raises(ServiceError) as excinfo:
            service.generate_drafts("demo-msg-3", use_external=True)
        assert "适配器不可用" in str(excinfo.value)
        assert service.list_drafts("demo-msg-3") == []
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# QQ：worker 不可用（崩溃）→ FAILED + caveats 明确
# ---------------------------------------------------------------------------

def test_qq_worker_crash_failed_and_summary_not_misleading(tmp_path) -> None:
    log_dir = tmp_path / "qq-worker-logs"
    service = make_service(
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={
            "FAKE_WORKER_BEHAVIOR": "crash",
            "FAKE_WORKER_LOG_DIR": str(log_dir),
        },
        qq_worker_output_dir=tmp_path / "ipc",
    )
    try:
        account = service.add_qq_account("负向 QQ", ["负向测试会话"])
        service.storage.upsert_conversation(
            make_conversation(
                "conv-qq-neg",
                account_id=account.id,
                title="负向测试会话",
                external_key="负向测试会话",
            )
        )
        service.grant_consent(ConsentScope.QQ_READ)
        service.update_settings(qq_read_experimental=True)
        service.resume_collection()

        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.FAILED
        assert results[0].messages_new == 0
        assert "子进程" in (results[0].error or "")
        assert service.list_messages() == []
        status = service.collection_status()
        assert status["last_scan_status"] == "failed"
        assert status["last_error"]

        summary = service.generate_daily_summary()
        assert summary.scan_status is ScanStatus.FAILED
        assert any("采集存在失败" in caveat for caveat in summary.caveats)
        assert "没有新的消息需要整理" not in summary.narrative
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# Keyring：不可用时明确错误、不误报、不崩溃
# ---------------------------------------------------------------------------

def test_keyring_unavailable_gives_clear_errors() -> None:
    broken = KeyringCredentialStore(keyring_backend=_BrokenKeyring())
    service = make_service(credential_store=broken)
    try:
        service.load_demo_data()
        with pytest.raises(ServiceError) as excinfo:
            service.set_mail_credential("neg@example.com", WRONG_AUTH_CODE)
        assert "凭据管理器" in str(excinfo.value)

        with pytest.raises(ServiceError) as excinfo:
            service.set_external_ai_key(API_KEY)
        assert "凭据管理器" in str(excinfo.value)

        assert service.has_mail_credential("neg@example.com") is False
        assert service.has_external_ai_key() is False
        check = service.test_mail_credential("neg@example.com")
        assert check.ok is False
        assert "凭据管理器" in check.detail

        service.grant_consent(ConsentScope.EXTERNAL_AI)
        ai_check = service.test_external_ai()
        assert ai_check.ok is False
        assert "凭据管理器" in ai_check.detail

        with pytest.raises(ServiceError):
            service.generate_drafts("demo-msg-3", use_external=True)
        assert service.list_drafts("demo-msg-3") == []
        # 凭据不可用不应影响其余功能
        assert len(service.list_messages()) > 0
    finally:
        service.shutdown()


def test_credential_store_error_type_is_sanitized() -> None:
    broken = KeyringCredentialStore(keyring_backend=_BrokenKeyring())
    with pytest.raises(CredentialStoreError) as excinfo:
        broken.set_secret("mail.auth_code.neg@example.com", WRONG_AUTH_CODE)
    message = str(excinfo.value)
    assert "凭据管理器" in message
    assert WRONG_AUTH_CODE not in message
