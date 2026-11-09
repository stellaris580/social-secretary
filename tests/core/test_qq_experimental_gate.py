"""P0-22 实验性 QQ 客户端只读（默认关闭）服务层安全门控测试。

覆盖：
- 默认关闭：run_scan 对 QQ 账号返回 BLOCKED、不构造 worker/不启动 UIA；test_account 同样拒绝；
- 开启后：run_scan/test_account 走现有 worker 路径（用 fake worker 断言）；
- 聚合扫描只按 QQ 账号门控，邮箱账号仍正常同步；
- settings 字段序列化/旧 settings.json 兼容/update_settings 支持。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from social_secretary.contracts import (
    AccountKind,
    ConsentScope,
    ScanStatus,
)
from social_secretary.core.service import (
    QQ_EXPERIMENTAL_DISABLED_RUN_SCAN,
    QQ_EXPERIMENTAL_DISABLED_TEST,
    SocialSecretaryService,
)
from social_secretary.core.settings_store import SettingsStore
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import (
    FakeMessageSource,
    make_conversation,
    make_service,
)

FAKE_WORKER = Path(__file__).with_name("fake_collector_worker.py")
FAKE_COMMAND = [sys.executable, str(FAKE_WORKER)]


def read_tasks(log_dir: Path) -> list[dict]:
    path = log_dir / "tasks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _qq_service(tmp_path, log_dir: Path) -> SocialSecretaryService:
    return make_service(
        credential_store=InMemoryCredentialStore(),
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={"FAKE_WORKER_LOG_DIR": str(log_dir)},
        qq_worker_output_dir=tmp_path / "ipc",
    )


def _prepare_qq(service, *, consent: bool = True):
    account = service.add_qq_account("实验 QQ", ["测试会话"])
    service.storage.upsert_conversation(
        make_conversation(
            "conv-qq-exp",
            account_id=account.id,
            title="测试会话",
            external_key="测试会话",
        )
    )
    if consent:
        service.grant_consent(ConsentScope.QQ_READ)
    service.resume_collection()
    return account


# ---------------------------------------------------------------------------
# 默认关闭：run_scan / test_account
# ---------------------------------------------------------------------------

def test_run_scan_qq_blocked_by_default_without_starting_worker(tmp_path) -> None:
    log_dir = tmp_path / "worker-logs"
    service = _qq_service(tmp_path, log_dir)
    try:
        account = _prepare_qq(service)
        results = service.run_scan(account.id, foreground_ack=True)

        assert len(results) == 1
        assert results[0].status is ScanStatus.BLOCKED
        assert results[0].error == QQ_EXPERIMENTAL_DISABLED_RUN_SCAN
        assert results[0].messages_new == 0
        # 默认关闭时绝不构造 worker/子进程
        assert read_tasks(log_dir) == []
        assert service.list_messages() == []
        assert service.collection_status()["last_scan_status"] == "blocked"
        assert "实验性 QQ" in (service.collection_status()["last_error"] or "")
    finally:
        service.shutdown()


def test_test_account_qq_blocked_by_default_without_starting_worker(tmp_path) -> None:
    log_dir = tmp_path / "worker-logs"
    service = _qq_service(tmp_path, log_dir)
    try:
        account = _prepare_qq(service)
        check = service.test_account(account.id)
        assert check.ok is False
        assert check.detail == QQ_EXPERIMENTAL_DISABLED_TEST
        assert check.discovered.get("experimental_disabled") is True
        assert read_tasks(log_dir) == []
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 开启后：走现有 worker 路径
# ---------------------------------------------------------------------------

def test_run_scan_qq_works_after_enabling_experimental_flag(tmp_path) -> None:
    log_dir = tmp_path / "worker-logs"
    service = _qq_service(tmp_path, log_dir)
    try:
        account = _prepare_qq(service)
        updated = service.update_settings(qq_read_experimental=True)
        assert updated.qq_read_experimental is True

        results = service.run_scan(account.id, foreground_ack=True)
        assert results[0].status is ScanStatus.OK
        assert results[0].messages_new == 1
        modes = [task["mode"] for task in read_tasks(log_dir)]
        assert "fetch_messages" in modes
        assert len(service.list_messages()) == 1
    finally:
        service.shutdown()


def test_test_account_qq_works_after_enabling_experimental_flag(tmp_path) -> None:
    log_dir = tmp_path / "worker-logs"
    service = _qq_service(tmp_path, log_dir)
    try:
        account = _prepare_qq(service)
        assert service.test_account(account.id).ok is False  # 默认关闭

        service.update_settings(qq_read_experimental=True)
        check = service.test_account(account.id)
        assert check.ok is True
        modes = [task["mode"] for task in read_tasks(log_dir)]
        assert modes and modes[0] == "test_connection"
    finally:
        service.shutdown()


def test_aggregate_scan_gates_only_qq_not_email(tmp_path) -> None:
    class _MailSource:
        kind = AccountKind.EMAIL

        def test_connection(self):
            from social_secretary.contracts import ConnectionCheck

            return ConnectionCheck(ok=True, detail="fake mail")

        def list_conversations(self):
            return []

        def fetch_messages(self, conversation, **kwargs):
            from social_secretary.contracts import RawMessage, TimestampSource

            return [
                RawMessage(
                    conversation_key=conversation.id,
                    sender="邮件发件人",
                    is_self=False,
                    text="邮件正文",
                    timestamp=None,
                    timestamp_source=TimestampSource.COLLECTED,
                    external_id="imap:1:1",
                    source_locator="IMAP INBOX UID 1",
                )
            ]

        def close(self) -> None:
            return None

    mail_source = _MailSource()
    log_dir = tmp_path / "worker-logs"
    service = make_service(
        credential_store=InMemoryCredentialStore(),
        source_factory=lambda account: mail_source if account.kind is AccountKind.EMAIL else None,
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={"FAKE_WORKER_LOG_DIR": str(log_dir)},
        qq_worker_output_dir=tmp_path / "ipc",
    )
    try:
        qq_account = _prepare_qq(service)
        mail_account = service.add_mail_account("neg@example.com", "邮箱")
        service.set_mail_credential("neg@example.com", "abcdefghijklmnop")
        service.grant_consent(ConsentScope.MAIL_READ)
        service.resume_collection()

        results = service.run_scan()
        by_account = {result.account_id: result for result in results}
        assert by_account[qq_account.id].status is ScanStatus.BLOCKED
        assert by_account[qq_account.id].error == QQ_EXPERIMENTAL_DISABLED_RUN_SCAN
        assert by_account[mail_account.id].status is ScanStatus.OK
        # 只允许邮箱消息入库
        messages = service.list_messages()
        assert len(messages) == 1
        assert messages[0].account_id == mail_account.id
        # QQ worker 未被启动
        assert read_tasks(log_dir) == []
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# settings 持久化 / 旧 JSON 兼容
# ---------------------------------------------------------------------------

def test_settings_field_defaults_persists_and_backward_compatible(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    # 旧版本 settings.json 没有 qq_read_experimental → 默认 False
    settings_path.write_text(
        json.dumps({"log_level": "INFO", "collection_paused": False}),
        encoding="utf-8",
    )
    store = SettingsStore(settings_path)
    loaded = store.load()
    assert loaded.qq_read_experimental is False

    store.save(
        type(loaded)(**{**loaded.to_dict(), "qq_read_experimental": True})
    )
    assert SettingsStore(settings_path).load().qq_read_experimental is True

    # 服务层 update_settings 支持该字段（不是 consent 字段）
    service = make_service(credential_store=InMemoryCredentialStore())
    try:
        assert service.get_settings().qq_read_experimental is False
        updated = service.update_settings(qq_read_experimental=True)
        assert updated.qq_read_experimental is True
        assert service.get_settings().qq_read_experimental is True
    finally:
        service.shutdown()


def test_experimental_setting_persists_across_service_restart() -> None:
    credentials = InMemoryCredentialStore()
    service = make_service(credential_store=credentials)
    try:
        service.update_settings(qq_read_experimental=True)
    finally:
        service.shutdown()

    reopened = make_service(credential_store=credentials)
    try:
        assert reopened.get_settings().qq_read_experimental is True
    finally:
        reopened.shutdown()
