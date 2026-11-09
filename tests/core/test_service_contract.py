"""ServiceAPI 全方法契约测试：UI 能调用的每个方法都有覆盖。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ConsentScope,
    ConversationKind,
    ExtractedTodo,
    Priority,
    ScanStatus,
    TodoStatus,
)
from social_secretary.core.service import NotFoundError, ServiceError
from social_secretary.core.storage import now_local
from social_secretary.security.credential_store import (
    EXTERNAL_AI_KEY,
    InMemoryCredentialStore,
)
from tests.core.fakes import (
    FakeClassifier,
    FakeDraftProvider,
    FakeMessageSource,
    FakeTodoExtractor,
    assert_service_api_surface,
    make_conversation,
    make_raw,
    make_service,
)

API_KEY = "sk-testplaceholder1234567890"
AUTH_CODE = "abcdefghijklmnop"


@pytest.fixture()
def env():
    source = FakeMessageSource()
    classifier = FakeClassifier({"请明天上午10点前提交周报": Category.IMPORTANT})
    todo_extractor = FakeTodoExtractor(
        {
            "请明天上午10点前提交周报": [
                ExtractedTodo(text="提交周报", due_text="明天上午10点前", confidence=0.8)
            ]
        }
    )
    draft_provider = FakeDraftProvider()
    credentials = InMemoryCredentialStore()
    service = make_service(
        credential_store=credentials,
        source_factory=lambda account: source,
        classifier=classifier,
        todo_extractor=todo_extractor,
        draft_provider=draft_provider,
    )
    try:
        yield SimpleNamespace(
            service=service,
            source=source,
            classifier=classifier,
            todo_extractor=todo_extractor,
            draft_provider=draft_provider,
            credentials=credentials,
        )
    finally:
        service.shutdown()


def setup_qq(env, *, allowlist=("张三",), titles=("张三", "项目群"), messages=None):
    account = env.service.add_qq_account("测试 QQ", list(allowlist))
    conversations = [
        make_conversation(
            f"conv-{index}",
            account_id=account.id,
            title=title,
            external_key=title,
            kind=ConversationKind.GROUP if "群" in title else ConversationKind.PRIVATE,
        )
        for index, title in enumerate(titles)
    ]
    env.source.conversations = list(conversations)
    env.source.messages = (
        messages
        if messages is not None
        else {
            "张三": [
                make_raw(
                    "张三",
                    "请明天上午10点前提交周报",
                    timestamp=now_local(),
                    source_locator="row-1",
                    external_id="ext-1",
                )
            ]
        }
    )
    env.service.grant_consent(ConsentScope.QQ_READ)
    # P0-22：实验性 QQ 读取默认关闭；本组用例验证扫描路径，先显式开启。
    env.service.update_settings(qq_read_experimental=True)
    env.service.refresh_conversations(account.id)
    env.service.resume_collection()
    return account, conversations


# ---------------------------------------------------------------------------
# 契约 & 账号
# ---------------------------------------------------------------------------

def test_service_implements_service_api(env) -> None:
    assert_service_api_surface(env.service)


def test_account_crud_and_validation(env) -> None:
    assert env.service.list_accounts() == []

    qq = env.service.add_qq_account("我的 QQ", ["张三"])
    assert qq.kind is AccountKind.QQ and qq.enabled is True
    mail = env.service.add_mail_account("demo@example.com", "我的邮箱")
    assert mail.kind is AccountKind.EMAIL and mail.identifier == "demo@example.com"
    assert len(env.service.list_accounts()) == 2

    with pytest.raises(ServiceError):
        env.service.add_qq_account("   ", [])
    with pytest.raises(ServiceError):
        env.service.add_mail_account("not-an-email", "x")
    with pytest.raises(ServiceError):
        env.service.add_mail_account("demo@example.com", "重复邮箱")

    env.service.set_account_enabled(qq.id, False)
    accounts = {account.id: account for account in env.service.list_accounts()}
    assert accounts[qq.id].enabled is False
    with pytest.raises(NotFoundError):
        env.service.set_account_enabled("missing", False)
    with pytest.raises(NotFoundError):
        env.service.remove_account("missing")

    env.service.remove_account(mail.id)
    assert [account.id for account in env.service.list_accounts()] == [qq.id]


def test_mail_account_creates_folder_conversation(env) -> None:
    account = env.service.add_mail_account("demo@example.com", "我的邮箱", folder="inbox")
    conversations = env.service.list_conversations(account.id)
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.kind is ConversationKind.MAIL
    assert conversation.external_key == "INBOX"
    assert conversation.enabled is True


# ---------------------------------------------------------------------------
# 会话 / 白名单 / 刷新
# ---------------------------------------------------------------------------

def test_refresh_requires_consent_then_applies_whitelist(env) -> None:
    account = env.service.add_qq_account("测试 QQ", ["张三"])
    env.source.conversations = [
        make_conversation("conv-a", account_id=account.id, title="张三", external_key="张三"),
        make_conversation("conv-b", account_id=account.id, title="项目群", external_key="项目群"),
    ]
    # 未授权：刷新被拒绝，且不创建会话
    check = env.service.refresh_conversations(account.id)
    assert check.ok is False
    assert check.discovered.get("consent_required") is True
    assert env.service.list_conversations(account.id) == []

    env.service.grant_consent(ConsentScope.QQ_READ)
    check = env.service.refresh_conversations(account.id)
    assert check.ok is True
    assert check.discovered["total"] == 2
    conversations = {item.title: item for item in env.service.list_conversations(account.id)}
    assert conversations["张三"].enabled is True
    assert conversations["项目群"].enabled is False  # 严格白名单：未授权不启用
    with pytest.raises(NotFoundError):
        env.service.refresh_conversations("missing")


def test_conversation_settings(env) -> None:
    account, conversations = setup_qq(env)
    conversation = env.service.list_conversations(account.id)[0]
    env.service.set_conversation_enabled(conversation.id, False)
    assert env.service.list_conversations(account.id)[0].enabled is False
    env.service.set_conversation_priority(conversation.id, Priority.HIGH)
    assert env.service.list_conversations(account.id)[0].priority is Priority.HIGH
    with pytest.raises(NotFoundError):
        env.service.set_conversation_enabled("missing", True)
    with pytest.raises(NotFoundError):
        env.service.set_conversation_priority("missing", Priority.LOW)


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

def test_scan_flow_status_and_summary(env) -> None:
    account, _ = setup_qq(env)
    results = env.service.run_scan(account.id)
    assert len(results) == 1
    assert results[0].status is ScanStatus.OK
    assert results[0].messages_new == 1
    assert env.source.fetch_calls  # 只 fetch 白名单会话

    messages = env.service.list_messages()
    assert len(messages) == 1
    message = messages[0]
    assert message.category is Category.IMPORTANT
    assert message.conversation_title == "张三"

    detail = env.service.get_source_detail(message.id)
    assert detail.account_kind is AccountKind.QQ
    assert "QQ" in detail.origin_label
    assert detail.message_id == message.id

    status = env.service.collection_status()
    assert set(status) == {"paused", "running", "last_scan_at", "last_scan_status", "last_error"}
    assert status["paused"] is False
    assert status["running"] is False
    assert status["last_scan_status"] == "ok"
    assert status["last_error"] is None
    assert status["last_scan_at"] is not None

    summary = env.service.generate_daily_summary()
    assert summary.counts["total"] == 1
    assert summary.counts["important"] == 1
    assert summary.is_demo is False
    assert env.service.get_cached_summary() is not None

    # 暂停：不采集、返回 PAUSED；恢复后继续
    env.service.pause_collection()
    paused_results = env.service.run_scan(account.id)
    assert paused_results[0].status is ScanStatus.PAUSED
    assert env.service.collection_status()["paused"] is True
    env.service.resume_collection()
    again = env.service.run_scan(account.id)
    assert again[0].messages_new == 0  # 跨批次幂等
    assert len(env.service.list_messages()) == 1


def test_scan_blocked_without_consent(env) -> None:
    account = env.service.add_qq_account("测试 QQ", ["张三"])
    env.service.update_settings(qq_read_experimental=True)
    env.service.resume_collection()
    env.source.conversations = [
        make_conversation("conv-a", account_id=account.id, title="张三", external_key="张三")
    ]
    results = env.service.run_scan(account.id)
    assert results[0].status is ScanStatus.BLOCKED
    assert "同意" in (results[0].error or "")
    assert env.source.fetch_calls == []
    assert env.service.list_messages() == []
    assert env.service.collection_status()["last_scan_status"] == "blocked"


def test_scan_source_unavailable_is_failed() -> None:
    service = make_service(source_factory=lambda account: None)
    try:
        account = service.add_qq_account("测试 QQ", ["张三"])
        # 有已启用会话才会真正去找采集器；否则是「无需采集」的 OK
        service.storage.upsert_conversation(
            make_conversation("conv-a", account_id=account.id, title="张三", external_key="张三")
        )
        service.grant_consent(ConsentScope.QQ_READ)
        service.update_settings(qq_read_experimental=True)
        service.resume_collection()
        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.FAILED
        assert "采集器不可用" in (results[0].error or "")
    finally:
        service.shutdown()


def test_test_account(env) -> None:
    account, _ = setup_qq(env)
    check = env.service.test_account(account.id)
    assert check.ok is True
    env.source.connection_ok = False
    assert env.service.test_account(account.id).ok is False
    with pytest.raises(NotFoundError):
        env.service.test_account("missing")


# ---------------------------------------------------------------------------
# 待办 / 草稿 / 消息
# ---------------------------------------------------------------------------

def test_todos_flow(env) -> None:
    account, _ = setup_qq(env)
    env.service.run_scan(account.id)
    todos = env.service.list_todos()
    assert len(todos) == 1
    assert todos[0].status is TodoStatus.OPEN

    env.service.update_todo_status(todos[0].id, TodoStatus.DONE)
    assert env.service.list_todos() == []
    assert len(env.service.list_todos(include_done=True)) == 1
    with pytest.raises(NotFoundError):
        env.service.update_todo_status("missing", TodoStatus.DONE)


def test_drafts_flow_local(env) -> None:
    account, _ = setup_qq(env)
    env.service.run_scan(account.id)
    message = env.service.list_messages()[0]

    drafts = env.service.generate_drafts(message.id)
    assert len(drafts) >= 2  # 默认两种风格，每种至少一条候选
    assert all(draft.kind.value == "local_rules" for draft in drafts)
    assert all(draft.requires_external_consent is False for draft in drafts)
    stored = env.service.list_drafts(message.id)
    assert len(stored) >= len(drafts)
    env.service.save_draft_edit(stored[0].id, "编辑后的回复")
    assert env.service.list_drafts(message.id)[0].edited_text == "编辑后的回复"

    with pytest.raises(NotFoundError):
        env.service.generate_drafts("missing-message")
    with pytest.raises(NotFoundError):
        env.service.save_draft_edit("missing-draft", "x")


def test_list_messages_filters(env) -> None:
    account, _ = setup_qq(env)
    env.service.run_scan(account.id)
    today = now_local().date()
    assert len(env.service.list_messages(day=today)) == 1
    assert len(env.service.list_messages(category=Category.IMPORTANT)) == 1
    assert env.service.list_messages(category=Category.FYI) == []
    assert len(env.service.list_messages(limit=1)) == 1
    with pytest.raises(ServiceError):
        env.service.list_messages(category="important")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        env.service.get_source_detail("missing")


# ---------------------------------------------------------------------------
# 设置 / 同意
# ---------------------------------------------------------------------------

def test_settings_and_consent_cycle(env) -> None:
    settings = env.service.get_settings()
    assert settings.collection_paused is True
    assert settings.qq_read_consent is False
    assert settings.external_ai_consent is False

    updated = env.service.update_settings(log_level="DEBUG", theme="dark")
    assert updated.log_level == "DEBUG"
    assert env.service.get_settings().theme == "dark"
    with pytest.raises(ServiceError):
        env.service.update_settings(no_such_field=1)
    with pytest.raises(ServiceError):
        env.service.update_settings(log_level="VERBOSE")
    with pytest.raises(ServiceError):
        env.service.update_settings(external_ai_base_url="ftp://x")
    # 同意状态必须走 grant/revoke，不能借 update_settings 绕过
    with pytest.raises(ServiceError):
        env.service.update_settings(qq_read_consent=True)

    env.service.grant_consent(ConsentScope.QQ_READ, confirmation_text="我同意读取 QQ")
    assert env.service.get_settings().qq_read_consent is True
    history = env.service.list_consent_history()
    assert history and history[0]["action"] == "grant"
    assert history[0]["scope"] == "qq_read"
    assert "我同意读取 QQ" not in str(history)  # 确认文本原文不落库

    env.service.revoke_consent(ConsentScope.QQ_READ)
    assert env.service.get_settings().qq_read_consent is False
    assert env.service.list_consent_history()[0]["action"] == "revoke"


# ---------------------------------------------------------------------------
# 邮箱凭据
# ---------------------------------------------------------------------------

def test_mail_credentials(env) -> None:
    env.service.add_mail_account("demo@example.com", "我的邮箱")
    assert env.service.has_mail_credential("demo@example.com") is False
    env.service.set_mail_credential("demo@example.com", AUTH_CODE)
    assert env.service.has_mail_credential("demo@example.com") is True
    with pytest.raises(ServiceError):
        env.service.set_mail_credential("demo@example.com", "   ")
    with pytest.raises(ServiceError):
        env.service.set_mail_credential("bad-email", AUTH_CODE)

    result = env.service.test_mail_credential("demo@example.com")
    assert result.ok is True  # 注入的 fake source
    env.service.delete_mail_credential("demo@example.com")
    assert env.service.has_mail_credential("demo@example.com") is False
    assert env.service.test_mail_credential("demo@example.com").ok is False
    # 凭据只在内存凭据库，不落文件
    for path in env.service.paths.data_dir.rglob("*"):
        if path.is_file():
            assert AUTH_CODE.encode() not in path.read_bytes()


# ---------------------------------------------------------------------------
# 演示数据 / 统计 / 导出 / 删除 / 关闭
# ---------------------------------------------------------------------------

def test_demo_mode_and_stats(env) -> None:
    assert env.service.is_demo_active() is False
    env.service.load_demo_data()
    assert env.service.is_demo_active() is True

    stats = env.service.get_storage_stats()
    assert set(stats) == {
        "message_count",
        "conversation_count",
        "todo_count",
        "draft_count",
        "db_bytes",
        "data_dir",
    }
    assert stats["message_count"] == 6
    assert stats["conversation_count"] == 3
    assert stats["todo_count"] == 2
    assert stats["draft_count"] == 2
    assert stats["data_dir"] == str(env.service.paths.data_dir)

    summary = env.service.generate_daily_summary()
    assert summary.is_demo is True
    assert summary.counts["total"] == 5
    assert env.service.get_cached_summary() is not None

    env.service.load_demo_data()  # 幂等
    assert env.service.get_storage_stats()["message_count"] == 6
    env.service.clear_demo_data()
    assert env.service.is_demo_active() is False
    assert env.service.get_storage_stats()["message_count"] == 0
    assert env.service.get_cached_summary() is None


def test_get_cached_summary_invalidated_by_scan(env) -> None:
    account, _ = setup_qq(env)
    env.service.run_scan(account.id)
    env.service.generate_daily_summary()
    assert env.service.get_cached_summary() is not None
    env.service.run_scan(account.id)
    assert env.service.get_cached_summary() is None


def test_export_diagnostics(env) -> None:
    env.service.load_demo_data()
    env.service.set_mail_credential("demo@example.com", AUTH_CODE)
    env.service.set_external_ai_key(API_KEY)
    path_text = env.service.export_diagnostics()
    path = env.service.paths.data_dir
    assert path_text
    from pathlib import Path

    diagnostics = Path(path_text)
    assert diagnostics.exists()
    assert diagnostics.parent == env.service.paths.exports_dir
    raw = diagnostics.read_text(encoding="utf-8")
    assert AUTH_CODE not in raw
    assert API_KEY not in raw
    # 诊断包不含任何消息正文
    assert "项目评审会" not in raw
    payload = json.loads(raw)
    assert "notes" in payload and "storage" in payload
    assert payload["data_dir"] == str(path)
    assert payload["credential_presence"]["external_ai_key_saved"] is True


def test_delete_all_local_data(env) -> None:
    env.service.load_demo_data()
    env.service.set_external_ai_key(API_KEY)
    env.service.set_mail_credential("demo@example.com", AUTH_CODE)
    env.service.update_settings(log_level="DEBUG")

    env.service.delete_all_local_data()

    stats = env.service.get_storage_stats()
    assert stats["message_count"] == 0
    assert stats["conversation_count"] == 0
    assert stats["todo_count"] == 0
    assert stats["draft_count"] == 0
    assert env.service.is_demo_active() is False
    assert env.service.has_external_ai_key() is False
    assert env.service.has_mail_credential("demo@example.com") is False
    settings = env.service.get_settings()
    assert settings.log_level == "INFO"
    assert settings.collection_paused is True


def test_remove_account_soft_keeps_messages(env) -> None:
    account, _ = setup_qq(env)
    env.service.run_scan(account.id)
    env.service.remove_account(account.id, delete_messages=False)
    assert env.service.list_accounts() == []
    assert len(env.service.list_messages()) == 1  # 软删除保留历史消息
    env.service.delete_all_local_data()
    assert env.service.list_messages() == []


def test_shutdown_is_idempotent_and_blocks_calls(env) -> None:
    env.service.shutdown()
    env.service.shutdown()
    with pytest.raises(ServiceError):
        env.service.list_accounts()
