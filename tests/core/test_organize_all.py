"""P0 返工：一键整理所有消息（organize_all）测试。

验证：
- 未配置账号时中文提示、不抛异常；
- 仅邮箱 / 仅 QQ / 两者都已配置；
- 批量 QQ 多会话扫描（fetch_messages 多次）而不是 quick_qq_read/当前聊天接口；
- 单账号/会话失败不阻断其他账号，成功/失败计数正确；
- 扫描后生成本地今日简报；简报失败不阻断并记录 errors；
- 审计仅计数/耗时/provider，无正文/标题；无发送/无额外采集路径。
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ConsentScope,
    ScanStatus,
)
from social_secretary.core.storage import now_local
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import (
    FakeMessageSource,
    make_conversation,
    make_message,
    make_raw,
    make_service,
)

FAKE_WORKER = Path(__file__).with_name("fake_collector_worker.py")
FAKE_COMMAND = [sys.executable, str(FAKE_WORKER)]


def _make_service(tmp_path, log_dir=None):
    kwargs = dict(credential_store=InMemoryCredentialStore())
    if log_dir is not None:
        kwargs.update(
            qq_worker_command=FAKE_COMMAND,
            qq_worker_env={"FAKE_WORKER_LOG_DIR": str(log_dir)},
            qq_worker_output_dir=tmp_path / "ipc",
        )
    return make_service(**kwargs)


def _read_worker_tasks(log_dir: Path) -> list[dict]:
    path = log_dir / "tasks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _add_email(service, text: str = "请明天提交周报"):
    account = service.add_mail_account("mail@example.com", "工作邮箱")
    service.grant_consent(ConsentScope.MAIL_READ)
    service.resume_collection()
    conversation = service.storage.list_conversations(account.id)[0]
    raw = make_raw(
        "INBOX",
        text,
        timestamp=now_local(),
        external_id="imap:1:1",
        source_locator="IMAP INBOX UID 1",
    )
    source = FakeMessageSource(
        kind=AccountKind.EMAIL,
        conversations=[conversation],
        messages={"INBOX": [raw]},
    )
    return account, conversation, source


def _add_qq(
    service,
    titles=("会话甲", "会话乙"),
    *,
    fail_titles=None,
    fail_fetch: bool = False,
):
    account = service.add_qq_account("测试 QQ", list(titles))
    conversations = []
    messages: dict[str, list] = {}
    for index, title in enumerate(titles):
        conversation = make_conversation(
            f"conv-{account.id}-{index}",
            account_id=account.id,
            title=title,
            external_key=title,
        )
        service.storage.upsert_conversation(conversation)
        conversations.append(conversation)
        messages[title] = [
            make_raw(
                title,
                f"请回复{title}的周报",
                timestamp=now_local(),
                external_id=f"qq-{account.id}-{index}",
                source_locator=f"row-{index}",
            )
        ]
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(qq_read_experimental=True)
    service.resume_collection()
    fail_ids = {
        conversation.id
        for conversation in conversations
        if conversation.title in set(fail_titles or ())
    }
    source = FakeMessageSource(
        kind=AccountKind.QQ,
        conversations=conversations,
        messages=messages,
        fail_conversations=fail_ids,
        fail_fetch=fail_fetch,
    )
    return account, conversations, source


class _AckSource(FakeMessageSource):
    """记录 foreground_ack 的 fake source。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.acks: list[bool] = []

    def fetch_messages(self, conversation, **kwargs):
        self.acks.append(bool(kwargs.get("foreground_ack")))
        return super().fetch_messages(conversation, **kwargs)


def _service_with_sources(tmp_path, *, log_dir=None):
    sources: dict[str, FakeMessageSource] = {}

    def factory(account):
        return sources.get(account.id)

    kwargs = dict(
        credential_store=InMemoryCredentialStore(),
        source_factory=factory,
    )
    if log_dir is not None:
        kwargs.update(
            {
                "qq_worker_command": FAKE_COMMAND,
                "qq_worker_env": {"FAKE_WORKER_LOG_DIR": str(log_dir)},
                "qq_worker_output_dir": tmp_path / "ipc",
            }
        )
    service = make_service(**kwargs)
    return service, sources


def test_organize_all_without_configured_accounts_is_structured(tmp_path) -> None:
    service, _sources = _service_with_sources(tmp_path)
    try:
        result = service.organize_all()
        assert result["ok"] is False
        assert result["configured"] == {"qq": False, "mail": False}
        assert result["results"] == []
        assert result["summary"] is None
        assert result["qq_success"] == result["qq_failed"] == 0
        assert result["mail_success"] == result["mail_failed"] == 0
        assert result["errors"] and "还没有可整理的账号" in result["errors"][0]
        assert result["duration_ms"] == 0
        assert set(result) == {
            "ok",
            "configured",
            "results",
            "summary",
            "qq_success",
            "qq_failed",
            "mail_success",
            "mail_failed",
            "errors",
            "duration_ms",
            "details",
            "qq_authorized",
            "qq_sessions_scanned",
            "qq_sessions_failed",
            "qq_failure_hint",
            "history_total",
            "latest_message_day",
        }
        assert result["details"] == []
        assert result["qq_authorized"] == 0
        assert result["qq_sessions_scanned"] == 0
        assert result["qq_sessions_failed"] == 0
        assert result["qq_failure_hint"] is None
        assert result["history_total"] == 0
        assert result["latest_message_day"] is None
    finally:
        service.shutdown()


def test_organize_all_only_email(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        account, _conversation, source = _add_email(service, "请明天提交周报（邮件正文标记ORG1）")
        sources[account.id] = source
        result = service.organize_all(foreground_ack=True)
        assert result["ok"] is True
        assert result["configured"] == {"qq": False, "mail": True}
        assert len(result["results"]) == 1
        assert result["results"][0].status is ScanStatus.OK
        assert result["mail_success"] == 1 and result["mail_failed"] == 0
        assert result["qq_success"] == result["qq_failed"] == 0
        assert result["summary"] is not None
        assert result["errors"] == []
        assert result["duration_ms"] >= 0
        assert len(service.list_messages()) == 1
    finally:
        service.shutdown()


def test_organize_all_only_qq_batches_multiple_sessions(tmp_path) -> None:
    log_dir = tmp_path / "worker-logs"
    service, sources = _service_with_sources(tmp_path, log_dir=log_dir)
    try:
        account, conversations, source = _add_qq(service, ("会话甲", "会话乙"))
        sources[account.id] = source

        result = service.organize_all()
        assert result["ok"] is True
        assert result["configured"] == {"qq": True, "mail": False}
        assert len(result["results"]) == 1
        assert result["results"][0].status is ScanStatus.OK
        assert result["qq_success"] == 1 and result["qq_failed"] == 0
        assert result["summary"] is not None
        # 批量扫描：两个白名单会话都 fetch 过，而不是 quick/当前聊天
        assert len(source.fetch_calls) == 2
        assert set(source.fetch_calls) == {conversation.id for conversation in conversations}
        assert _read_worker_tasks(log_dir) == []  # 未走 detect_current_chat/quick
        assert not hasattr(service, "_last_quick_qq")  # 未调用 quick_qq_read 路径
        assert len(service.list_messages()) == 2
    finally:
        service.shutdown()


def test_organize_all_both_qq_and_mail(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        mail_account, _mail_conv, mail_source = _add_email(service, "邮件请回复")
        qq_account, _qq_convs, qq_source = _add_qq(service, ("QQ会话一",))
        sources[mail_account.id] = mail_source
        sources[qq_account.id] = qq_source

        result = service.organize_all(foreground_ack=True)
        assert result["ok"] is True
        assert result["configured"] == {"qq": True, "mail": True}
        assert result["qq_success"] == 1
        assert result["mail_success"] == 1
        assert result["qq_failed"] == result["mail_failed"] == 0
        assert len(result["results"]) == 2
        assert result["summary"] is not None
        assert len(service.list_messages()) == 2
    finally:
        service.shutdown()


def test_organize_all_partial_failure_does_not_block_other_accounts(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        ok_account, _convs, ok_source = _add_qq(service, ("正常会话",))
        bad_account, _bad_convs, bad_source = _add_qq(
            service, ("坏会话", "正常会话B"), fail_titles={"坏会话"}
        )
        mail_account, _mail_conv, mail_source = _add_email(service, "邮件也继续")
        sources[ok_account.id] = ok_source
        sources[bad_account.id] = bad_source
        sources[mail_account.id] = mail_source

        result = service.organize_all()
        assert result["ok"] is True
        # 正常账号 OK + 部分失败账号 PARTIAL 都按成功计
        assert result["qq_success"] == 2
        assert result["qq_failed"] == 0
        assert result["mail_success"] == 1
        assert len(result["results"]) == 3
        statuses = {(item.account_id, item.status) for item in result["results"]}
        assert (ok_account.id, ScanStatus.OK) in statuses
        assert (bad_account.id, ScanStatus.PARTIAL) in statuses
        assert (mail_account.id, ScanStatus.OK) in statuses
    finally:
        service.shutdown()


def test_organize_all_account_failure_counted_but_not_blocking(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        good_account, _convs, good_source = _add_qq(service, ("正常",))
        failed_account, _bad, failed_source = _add_qq(service, ("失败",), fail_fetch=True)
        sources[good_account.id] = good_source
        sources[failed_account.id] = failed_source

        result = service.organize_all()
        assert result["ok"] is True
        assert result["qq_success"] == 1
        assert result["qq_failed"] == 1
        assert result["summary"] is not None
    finally:
        service.shutdown()


def test_organize_all_summary_failure_is_reported_not_blocking(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        account, _conversation, source = _add_email(service, "简报失败仍要整理")
        sources[account.id] = source

        def boom(day=None):
            raise RuntimeError("summary boom")

        service.generate_daily_summary = boom  # type: ignore[assignment]
        result = service.organize_all()
        assert result["ok"] is True
        assert result["summary"] is None
        assert result["mail_success"] == 1
        assert any("今日简报生成失败" in error for error in result["errors"])
    finally:
        service.shutdown()


def test_organize_all_passes_foreground_ack_to_batch_fetch(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        account, _convs, _source = _add_qq(service, ("确认会话一", "确认会话二"))
        ack_source = _AckSource(
            kind=AccountKind.QQ,
            conversations=service.storage.list_conversations(account.id),
            messages={
                "确认会话一": [make_raw("确认会话一", "a", timestamp=now_local())],
                "确认会话二": [make_raw("确认会话二", "b", timestamp=now_local())],
            },
        )
        sources[account.id] = ack_source
        result = service.organize_all(foreground_ack=True)
        assert result["ok"] is True
        assert ack_source.acks == [True, True]
    finally:
        service.shutdown()


def test_organize_all_audit_has_counts_only_no_body(tmp_path) -> None:
    log_dir = tmp_path / "worker-logs"
    service, sources = _service_with_sources(tmp_path, log_dir=log_dir)
    try:
        account, _conversation, source = _add_email(
            service, "绝密正文标记ORG9Q7，请明天处理"
        )
        sources[account.id] = source
        service.organize_all()
        audit = json.dumps(service.storage.list_audit(), ensure_ascii=False)
        assert "organize_all" in audit
        assert "ORG9Q7" not in audit
        entries = [
            entry
            for entry in service.storage.list_audit()
            if entry.get("action") == "organize_all"
        ]
        assert entries
        detail = entries[-1]["detail"]
        assert set(detail) == {
            "configured",
            "qq_success",
            "qq_failed",
            "mail_success",
            "mail_failed",
            "result_count",
            "duration_ms",
            "provider",
        }
        assert detail["provider"] == "local_rules"
        assert "工作邮箱" not in audit
    finally:
        service.shutdown()


class _WindowUnavailableSource(FakeMessageSource):
    """模拟真实失败：某些会话读取时 QQ 主面板无法置前（qq_window_unavailable）。"""

    def __init__(self, *, bad_ids, **kwargs) -> None:
        super().__init__(**kwargs)
        self.bad_ids = set(bad_ids)

    def fetch_messages(self, conversation, **kwargs):
        self.fetch_calls.append(conversation.id)
        if conversation.id in self.bad_ids:
            raise RuntimeError(
                "无法将 QQ 主面板置前，已取消读取。（qq_window_unavailable）"
            )
        return list(self.messages.get(conversation.external_key, []))


def test_organize_all_partial_reports_details_hint_and_history(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        account, conversations, _ = _add_qq(service, ("私密会话标题X", "第二会话"))
        bad_id = conversations[0].id
        source = _WindowUnavailableSource(
            kind=AccountKind.QQ,
            conversations=conversations,
            bad_ids={bad_id},
            messages={
                "第二会话": [
                    make_raw("第二会话", "正常正文内容OK", timestamp=now_local())
                ]
            },
        )
        sources[account.id] = source
        # 已有历史消息（3 天前），验证失败时仍能展示“历史还在”
        history = make_message(
            conversations[1],
            "历史消息",
            timestamp=now_local() - timedelta(days=3),
        )
        service.storage.insert_message(history)

        result = service.organize_all()
        assert result["ok"] is True
        assert result["qq_authorized"] == 2
        assert result["qq_sessions_scanned"] == 1
        assert result["qq_sessions_failed"] == 1
        hint = (
            "QQ 主面板无法置前（可能被最小化到托盘或被其他窗口遮挡）。"
            "请先打开 QQ 主面板，保持可见，再重试。"
        )
        assert result["qq_failure_hint"] == hint
        assert hint in result["errors"]
        assert result["history_total"] == 2  # 历史消息 + 本次成功会话消息
        assert result["latest_message_day"] == now_local().date().isoformat()

        qq_details = [item for item in result["details"] if item["kind"] == "qq"]
        assert len(qq_details) == 1
        detail = qq_details[0]
        assert detail["status"] == "partial"
        assert detail["conversations_scanned"] == 1
        assert detail["messages_new"] >= 1
        assert "qq_window_unavailable" in json.dumps(detail, ensure_ascii=False)
        # 审计仍不含标题/正文
        audit = json.dumps(service.storage.list_audit(), ensure_ascii=False)
        assert "私密会话标题X" not in audit
        assert "正常正文内容OK" not in audit
        assert "历史消息" not in audit
    finally:
        service.shutdown()


def test_organize_all_generic_qq_failure_hint_and_counts(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        account, _conversations, source = _add_qq(service, ("失败会话",))
        source.fail_fetch = True
        sources[account.id] = source

        result = service.organize_all()
        assert result["ok"] is True
        assert result["qq_authorized"] == 1
        assert result["qq_sessions_scanned"] == 0
        assert result["qq_sessions_failed"] == 1
        assert result["qq_failed"] == 1
        assert result["qq_failure_hint"] == (
            "QQ 读取失败，请确认 QQ 已登录且主面板未被遮挡"
        )
        assert result["qq_failure_hint"] in result["errors"]
        detail = [item for item in result["details"] if item["kind"] == "qq"][0]
        assert detail["status"] == "failed"
        assert detail["error"]
        assert detail["conversations_scanned"] == 0
    finally:
        service.shutdown()


def test_organize_all_reports_history_even_when_scan_finds_nothing(tmp_path) -> None:
    service, sources = _service_with_sources(tmp_path)
    try:
        account, conversation, source = _add_email(service, "本次没有新邮件")
        source.messages = {"INBOX": []}
        sources[account.id] = source
        old_moment = now_local() - timedelta(days=3)
        service.storage.insert_message(
            make_message(conversation, "更早的历史消息", timestamp=old_moment)
        )

        result = service.organize_all()
        assert result["ok"] is True
        assert result["mail_success"] == 1
        assert result["history_total"] == 1
        assert result["latest_message_day"] == old_moment.date().isoformat()
        assert result["details"][0]["kind"] == "email"
        assert result["details"][0]["status"] == "ok"
        assert result["details"][0]["messages_new"] == 0
        assert result["qq_failure_hint"] is None
    finally:
        service.shutdown()
