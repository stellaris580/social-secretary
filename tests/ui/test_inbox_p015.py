"""P0-15：收件箱页 / 连接向导 / 连接 API / 摘要回退（Tkinter）。"""

from __future__ import annotations

from datetime import timedelta

from social_secretary.contracts import ConsentScope


def _inbox(window):
    return window.pages["inbox"]


def _connect_ready_service(service, email: str = "me@qq.com", code: str = "x" * 16):
    service.add_mail_account(email, email)
    service.set_mail_credential(email, code)
    return email


# ---------------------------------------------------------------------------
# 页面结构 / 固定钩子
# ---------------------------------------------------------------------------
def test_inbox_page_is_second_nav_and_exposes_hooks(window):
    keys = list(window.nav_buttons)
    assert keys.index("inbox") == 1
    assert window.nav_buttons["inbox"].cget("text") == "收件箱"
    page = window.pages["inbox"]
    for name in ("refresh", "rows", "sync_now", "select_row", "open_selected", "detail_content", "status_text"):
        assert callable(getattr(page, name)), name
    assert page.tree is not None
    assert page.detail_text is not None


def test_inbox_demo_rows_are_marked_demo_and_detail_shows_body(window):
    page = _inbox(window)
    page.refresh()
    rows = page.rows()
    assert rows, "演示数据里应有邮箱示例消息"
    assert all(row["values"][0] == "演示" for row in rows)
    assert page.select_row(0) is True
    detail = page.detail_content()
    assert "主题：" in detail and "发件人：" in detail and "来源：" in detail and "正文：" in detail


def test_inbox_real_mail_is_marked_real_and_detail_matches(service, window):
    message = service.seed_inbox_mail(
        subject="真实主题：周末活动确认",
        sender="真实发件人 <real@qq.com>",
        text="真实正文内容：请确认周六场地和人数。",
        is_demo=False,
    )
    page = _inbox(window)
    page.refresh()
    rows = page.rows()
    assert any(row["values"][0] == "真实" for row in rows)
    index = next(index for index, row in enumerate(rows) if row["values"][0] == "真实")
    assert page.select_row(index) is True
    assert page.open_selected() is True
    detail = page.detail_content()
    assert "真实主题：周末活动确认" in detail
    assert "真实正文内容：请确认周六场地和人数。" in detail
    assert message.source_locator in detail


def test_inbox_empty_state_unconfigured(service, window):
    service.clear_demo_data()
    page = _inbox(window)
    page.refresh()
    assert page.rows() == []
    text = page.status_text()
    assert "还没有连接 QQ 邮箱" in text
    assert "连接 QQ 邮箱" in text


def test_inbox_empty_state_configured_not_synced(window):
    service = window.service
    service.clear_demo_data()
    _connect_ready_service(service)
    page = _inbox(window)
    page.refresh()
    text = page.status_text()
    assert "还没有同步过" in text
    assert "同步邮件" in text


def test_inbox_empty_state_synced_but_empty(window):
    service = window.service
    service.clear_demo_data()
    email = _connect_ready_service(service)
    service.sync_mail(user_confirmed=True)
    assert service.list_inbox_messages() == []  # 真实邮箱没有邮件
    service.mail_inbox_status = lambda *args, **kwargs: {
        "account_count": 1,
        "email": email,
        "credential_saved": True,
        "mail_read_consent": True,
        "paused": False,
        "last_sync_at": service._now_dt,
        "last_error": None,
        "message_count": 0,
    }
    page = _inbox(window)
    page.refresh()
    assert "邮箱暂无可显示邮件" in page.status_text()


def test_inbox_sync_failure_is_red_and_never_contains_success(window, notify_records):
    service = window.service
    service.fail_method("sync_mail", "演示同步故障")
    page = _inbox(window)
    page.refresh()
    assert page.sync_now() is None
    text = page.status_text()
    assert "同步邮件失败" in text
    assert "成功" not in text
    assert page.status_label.cget("fg") == window.theme.color("danger")


def test_inbox_sync_now_calls_sync_mail_with_user_confirmation(window):
    page = _inbox(window)
    page.refresh()
    results = page.sync_now()
    assert results is not None
    service_calls = window.service.calls_to("sync_mail")
    assert service_calls
    assert service_calls[-1][2]["user_confirmed"] is True


def test_inbox_test_connection_failure_has_no_success_word(window, notify_records):
    page = _inbox(window)
    page.refresh()
    # 演示服务默认没有保存授权码 → 连接失败
    check = page.test_connection()
    assert check is not None and check.ok is False
    text = page.status_text()
    assert "连接失败" in text
    assert "成功" not in text


# ---------------------------------------------------------------------------
# 连接向导 / window.connect_mail_account（GUI E2E 共用）
# ---------------------------------------------------------------------------
def test_connect_mail_account_requires_credential(service, window):
    result = window.connect_mail_account("new@qq.com")
    assert result["ok"] is False
    assert "授权码" in result["error"]
    assert result["consent_ok"] is False


def test_connect_mail_account_full_flow(service, window):
    result = window.connect_mail_account("new@qq.com", "y" * 16)
    assert result["ok"] is True, result
    assert result["added_account"] is True
    assert result["connection_ok"] is True
    assert result["consent_ok"] is True
    assert result["resumed"] is True
    assert result["sync_ok"] is True
    assert result["error"] is None
    assert any(call[1] and call[1][0] == ConsentScope.MAIL_READ for call in service.calls_to("grant_consent"))
    assert service.calls_to("resume_collection")
    assert service.calls_to("sync_mail")[-1][2]["user_confirmed"] is True
    # 已存在的邮箱账号应复用，不重复添加
    service.reset_calls()
    result2 = window.connect_mail_account("new@qq.com")
    assert result2["ok"] is True
    assert result2["added_account"] is False


def test_connect_mail_account_connection_failure_never_reports_success(service, window):
    service.fail_method("test_mail_credential", "演示连接故障")
    result = window.connect_mail_account("bad@qq.com", "z" * 16)
    assert result["ok"] is False
    assert result["connection_ok"] is False
    assert "失败" in result["error"]
    assert "成功" not in result["error"]
    assert not service.called("grant_consent")


def test_connect_mail_account_sync_failure_never_reports_success(service, window):
    service.fail_method("sync_mail", "演示同步故障")
    result = window.connect_mail_account("syncfail@qq.com", "q" * 16)
    assert result["ok"] is False
    assert result["sync_ok"] is False
    assert "失败" in result["error"]
    assert "成功" not in result["error"]


def test_mail_connect_wizard_is_same_product_code(window, service):
    from social_secretary.ui.dialogs import MailConnectWizard

    wizard = MailConnectWizard(window)
    try:
        wizard.email_entry.insert(0, "wizard@qq.com")
        wizard.auth_entry.insert(0, "w" * 16)
        first = wizard.save_and_test()
        assert first["connection_ok"] is True
        assert wizard.consent_check.cget("state") == "normal"
        wizard.consent_var.set(1)
        wizard._update_consent_button()
        assert wizard.sync_button.cget("state") == "normal"
        second = wizard.grant_and_sync()
        assert second["ok"] is True
        assert wizard.confirmed is True
        assert service.calls_to("sync_mail")[-1][2]["user_confirmed"] is True
    finally:
        if wizard.winfo_exists():
            wizard.destroy()


def test_mail_connect_wizard_detects_saved_credential(window, service):
    from social_secretary.ui.dialogs import MailConnectWizard

    service.set_mail_credential("saved@qq.com", "s" * 16)
    wizard = MailConnectWizard(window)
    try:
        wizard.email_entry.insert(0, "saved@qq.com")
        wizard._detect_saved()
        assert "已检测到本机已保存授权码" in wizard.saved_hint.cget("text")
    finally:
        wizard.destroy()


def test_mail_connect_wizard_failure_keeps_consent_disabled(window, service):
    from social_secretary.ui.dialogs import MailConnectWizard

    wizard = MailConnectWizard(window)
    try:
        wizard.email_entry.insert(0, "nocode@qq.com")
        result = wizard.save_and_test()
        assert result["connection_ok"] is False
        assert wizard.consent_check.cget("state") == "disabled"
        text = wizard.status_label.cget("text")
        assert "连接失败" in text
        assert "成功" not in text
    finally:
        wizard.destroy()


# ---------------------------------------------------------------------------
# 摘要回退：今日无邮件 → 最近有邮件的日期
# ---------------------------------------------------------------------------
def test_summary_falls_back_to_latest_mail_day(service, window):
    service.clear_demo_data()
    _connect_ready_service(service)
    old = service._now_dt - timedelta(days=3)
    service.seed_inbox_mail(
        subject="三天前的真实邮件",
        text="这是三天前的真实邮件正文。",
        timestamp=old,
        is_demo=False,
    )
    summary = window.generate_summary()
    assert summary is not None
    assert summary.day == old.date()
    assert len(summary.important) + len(summary.normal) + len(summary.fyi) > 0
    page = window.pages["summary"]
    assert page.summary() is summary
    banner_text = page.fallback_banner.text()
    assert "今日无邮件" in banner_text
    assert old.date().isoformat() in banner_text
    assert summary.is_demo is False


def test_summary_no_fallback_when_today_has_mail(service, window):
    service.clear_demo_data()
    _connect_ready_service(service)
    service.seed_inbox_mail(subject="今天的邮件", text="今天的邮件正文。", is_demo=False)
    summary = window.generate_summary()
    assert summary is not None
    assert summary.day == service._now_dt.date()
    assert window.pages["summary"].fallback_banner.text() == ""
