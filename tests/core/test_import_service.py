"""P0-17 实验性聊天文本导入（Service 层）测试。

覆盖：dry-run 不落库、真实导入可查询/进摘要、二次导入去重、组聊/选项、
非法输入与选项结构化错误、超长截断、run_scan/test_account/refresh 不触碰 IMPORT 账号、
关闭重开持久化、返回值与警告不含消息正文。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from social_secretary.contracts import (
    AccountKind,
    ConversationKind,
    ScanStatus,
)
from social_secretary.core.service import (
    IMPORT_ACCOUNT_DISPLAY_NAME,
    IMPORT_ACCOUNT_IDENTIFIER,
    SocialSecretaryService,
)
from social_secretary.core.storage import now_local
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import make_service


def build_text(*, count: int = 2, base: datetime | None = None, group: bool = False) -> str:
    moment = base or now_local().replace(microsecond=0)
    lines: list[str] = []
    senders = ["张三", "我"] if not group else ["李四", "我"]
    for index in range(count):
        stamp = (moment + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S")
        sender = senders[index % len(senders)]
        lines.append(f"{stamp} {sender}: 第{index + 1}条测试消息，请明天提交周报。")
    return "\n".join(lines) + "\n"


def _import_account(service):
    return next(
        (a for a in service.list_accounts() if a.kind is AccountKind.IMPORT), None
    )


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------

def test_dry_run_parses_without_writing() -> None:
    service = make_service()
    try:
        result = service.import_chat_text(
            build_text(count=2), source_name="chat.txt", dry_run=True
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["experimental"] is True
        assert result["format_id"] == "generic"
        assert result["format"] == result["format_label"]
        assert result["format_label"]
        assert result["message_count"] == 2
        assert result["imported_count"] == 0
        assert result["duplicate_count"] == 0
        assert result["conversation_count"] == 1
        assert isinstance(result["stats"], dict)
        assert result["stats"]["parsed_messages"] == 2
        assert result["skipped_count"] == int(result["stats"]["skipped_lines"])
        assert result["warning_count"] == len(result["warnings"])
        assert all(isinstance(item, dict) and "code" in item for item in result["warnings"])
        assert result["error"] is None
        # UI 预览行：只有元数据/长度，绝不含正文
        assert isinstance(result["preview"], list) and len(result["preview"]) == 2
        for row in result["preview"]:
            assert "text" not in row
            assert isinstance(row["length"], int) and row["length"] > 0
            assert row["status"] == "将导入"

        # 未创建 IMPORT 账号 / 会话 / 消息
        assert _import_account(service) is None
        assert service.storage.count_messages() == 0
        assert service.storage.count_conversations() == 0
        # 返回值不含消息正文（sender/主题/长度等元数据可用于 UI 预览）
        assert "第1条测试消息" not in json.dumps(result, ensure_ascii=False)
        assert "请明天提交周报" not in json.dumps(result, ensure_ascii=False)
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 真实导入
# ---------------------------------------------------------------------------

def test_real_import_creates_import_account_and_queryable_messages() -> None:
    service = make_service()
    try:
        text = build_text(count=2)
        result = service.import_chat_text(text, source_name="chat.txt", dry_run=False)
        assert result["ok"] is True
        assert result["dry_run"] is False
        assert result["imported_count"] == 2
        assert result["duplicate_count"] == 0
        assert result["account_id"]

        account = _import_account(service)
        assert account is not None
        assert account.id == result["account_id"]
        assert account.kind is AccountKind.IMPORT
        assert account.enabled is False
        assert account.display_name == IMPORT_ACCOUNT_DISPLAY_NAME
        assert account.identifier == IMPORT_ACCOUNT_IDENTIFIER
        # 不参与邮箱/账号采集列表
        assert [item.id for item in service.list_mail_accounts()] == []

        messages = service.list_messages()
        assert len(messages) == 2
        stored = {message.text for message in messages}
        assert "第1条测试消息，请明天提交周报。" in stored
        assert all(message.source_locator.startswith("import:chat.txt:") for message in messages)
        assert all(message.conversation_kind is ConversationKind.PRIVATE for message in messages)
        assert all(message.category_reasons for message in messages)
        # 默认“我”识别为已发送
        assert any(message.is_self for message in messages)

        detail = service.get_source_detail(messages[0].id)
        assert detail.account_kind is AccountKind.IMPORT
        assert "导入" in detail.origin_label
        assert detail.locator and detail.locator.startswith("import:chat.txt:")

        # 进当天摘要（“我”的一条算 sent，不计入收到的消息）
        summary = service.generate_daily_summary()
        assert summary.counts["total"] == 1
        assert summary.counts["sent"] == 1
        assert "第1条测试消息" not in (summary.narrative or "")
    finally:
        service.shutdown()


def test_real_import_group_options_and_conversation_reuse() -> None:
    service = make_service()
    try:
        text = build_text(count=1, group=True)
        result = service.import_chat_text(
            text,
            source_name="group.txt",
            options={
                "kind": "group",
                "title": "项目讨论群",
                "conversation_key": "import-group-1",
            },
            dry_run=False,
        )
        assert result["ok"] is True
        conversation = service.list_conversations()[0]
        assert conversation.kind is ConversationKind.GROUP
        assert conversation.title == "项目讨论群"
        assert conversation.external_key == "import-group-1"
        # 再导入同 key：复用会话、不重复建
        second = service.import_chat_text(
            text,
            source_name="group.txt",
            options={"kind": "group", "title": "项目讨论群", "conversation_key": "import-group-1"},
            dry_run=False,
        )
        assert second["imported_count"] == 0
        assert second["duplicate_count"] == 1
        assert len(service.list_conversations()) == 1
    finally:
        service.shutdown()


def test_second_import_is_idempotent() -> None:
    service = make_service()
    try:
        text = build_text(count=2)
        first = service.import_chat_text(text, source_name="chat.txt", dry_run=False)
        assert first["imported_count"] == 2
        second = service.import_chat_text(text, source_name="chat.txt", dry_run=False)
        assert second["ok"] is True
        assert second["imported_count"] == 0
        assert second["duplicate_count"] == 2
        assert service.storage.count_messages() == 2
        # 只有一个 IMPORT 账号、一个会话
        accounts = [a for a in service.list_accounts() if a.kind is AccountKind.IMPORT]
        assert len(accounts) == 1
        assert len(service.list_conversations()) == 1
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 非法输入 / 结构化错误
# ---------------------------------------------------------------------------

def test_empty_text_is_structured_error_not_exception() -> None:
    service = make_service()
    try:
        result = service.import_chat_text("", dry_run=True)
        assert result["ok"] is False
        assert result["error"]
        assert result["warnings"]
        assert result["message_count"] == 0
        assert service.storage.count_messages() == 0
        blank = service.import_chat_text("   \n  ", dry_run=False)
        assert blank["ok"] is False and blank["error"]
    finally:
        service.shutdown()


def test_unrecognized_format_is_structured_error() -> None:
    service = make_service()
    try:
        result = service.import_chat_text("hello world\nthis is not chat", dry_run=True)
        assert result["ok"] is False
        assert result["error"] and ("解析" in result["error"] or "格式" in result["error"])
        assert isinstance(result["warnings"], list)
    finally:
        service.shutdown()


def test_non_string_input_is_structured_error() -> None:
    service = make_service()
    try:
        result = service.import_chat_text(123, dry_run=True)  # type: ignore[arg-type]
        assert result["ok"] is False and "文本" in result["error"]
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    "options,keyword",
    [
        ("not-a-dict", "字典"),
        ({"kind": "mail"}, "私聊/群聊"),
        ({"conversation_kind": "channel"}, "非法"),
        ({"max_messages": 0}, "整数"),
        ({"max_messages": True}, "整数"),
        ({"self_sender_names": 123}, "字符串"),
        ({"now": "not-a-date"}, "now"),
        ({"dedupe": "yes"}, "布尔"),
    ],
)
def test_invalid_options_are_structured_errors(options, keyword: str) -> None:
    service = make_service()
    try:
        result = service.import_chat_text(build_text(count=1), options=options, dry_run=True)
        assert result["ok"] is False
        assert keyword in (result["error"] or "")
        assert service.storage.count_messages() == 0
    finally:
        service.shutdown()


def test_unknown_options_are_ignored() -> None:
    service = make_service()
    try:
        result = service.import_chat_text(
            build_text(count=1), options={"unknown_option": 1}, dry_run=True
        )
        assert result["ok"] is True
    finally:
        service.shutdown()


def test_oversized_input_truncated_structured() -> None:
    service = make_service()
    try:
        moment = now_local().replace(microsecond=0)
        first_line = moment.strftime("%Y-%m-%d %H:%M:%S") + " 张三: 短"
        huge = first_line + "\n" + ("填充" * 500) + "\n"
        result = service.import_chat_text(
            huge, options={"max_input_chars": max(20, len(first_line) + 1)}, dry_run=True
        )
        assert isinstance(result, dict)
        assert result["stats"]["truncated_input"] is True
        assert "input_too_large" in result["warning_codes"]
        assert result["ok"] is True  # 截断前第一条仍能解析
        assert result["stats"]["parsed_messages"] == 1
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# IMPORT 账号不参与采集（安全隔离）
# ---------------------------------------------------------------------------

def test_run_scan_and_connection_ignore_import_account() -> None:
    calls: list[object] = []

    def factory(account):
        calls.append(account)
        raise AssertionError("导入账号不应构造任何采集器")

    service = make_service(source_factory=factory)
    try:
        service.import_chat_text(build_text(count=1), source_name="chat.txt", dry_run=False)
        account = _import_account(service)
        assert account is not None

        results = service.run_scan(account.id)
        assert len(results) == 1
        assert results[0].status is ScanStatus.BLOCKED
        assert "导入" in (results[0].error or "")
        # 聚合扫描也不应包含 IMPORT 账号
        assert service.run_scan() == []
        assert calls == []
        assert service.storage.count_scan_runs() == 0

        check = service.test_account(account.id)
        assert check.ok is False and "导入" in check.detail
        refresh = service.refresh_conversations(account.id)
        assert refresh.ok is False and "导入" in refresh.detail
        assert calls == []
        # 导入数据仍在
        assert service.storage.count_messages() == 1
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 持久化 / 重开
# ---------------------------------------------------------------------------

def test_import_persists_across_service_restart() -> None:
    credentials = InMemoryCredentialStore()
    service = make_service(credential_store=credentials)
    try:
        service.import_chat_text(build_text(count=2), source_name="chat.txt", dry_run=False)
    finally:
        service.shutdown()

    reopened = make_service(credential_store=credentials)
    try:
        messages = reopened.list_messages()
        assert len(messages) == 2
        assert all(m.source_locator.startswith("import:chat.txt:") for m in messages)
        account = _import_account(reopened)
        assert account is not None and account.kind is AccountKind.IMPORT
        assert len(reopened.list_conversations()) == 1
        # 重开后再次导入仍去重
        second = reopened.import_chat_text(
            build_text(count=2), source_name="chat.txt", dry_run=False
        )
        assert second["imported_count"] == 0
        assert second["duplicate_count"] == 2
        summary = reopened.generate_daily_summary()
        assert summary.counts["total"] == 1
        assert summary.counts["sent"] == 1
    finally:
        reopened.shutdown()


# ---------------------------------------------------------------------------
# 隐私：返回值/警告/证据不含正文
# ---------------------------------------------------------------------------

def test_import_does_not_leak_message_text_in_response_or_audit() -> None:
    service = make_service()
    try:
        marker = "绝密导入正文标记X9Q7"
        text = (
            now_local().strftime("%Y-%m-%d %H:%M:%S")
            + f" 张三: {marker}，请明天提交周报\n"
        )
        result = service.import_chat_text(text, source_name="privacy.txt", dry_run=False)
        assert result["ok"] is True
        assert marker not in json.dumps(result, ensure_ascii=False)
        assert marker not in str(service.storage.list_audit())
        assert result["source"]["length"] == len("privacy.txt")
        assert result["source_name"] == "privacy.txt"
        assert result["skipped_count"] == 0
        assert result["warning_count"] == len(result["warnings"])
    finally:
        service.shutdown()
