"""设置与隐私页测试：账号、授权码帮助、白名单、采集、AI 同意门控、隐私数据操作（Tkinter）。"""

from __future__ import annotations

from social_secretary.contracts import ConsentScope
from social_secretary.ui.pages import settings_page as settings_page_module


def _page(window):
    return window.pages["settings"]


def test_accounts_and_whitelist_loaded(window):
    page = _page(window)
    assert len(page.account_rows) == 2
    assert len(page.whitelist_rows) == 3  # 当前账号（QQ）有 3 个会话
    assert "允许读取" in page.whitelist_status_label.cget("text")


def test_toggle_account_enabled_calls_service(window, service):
    page = _page(window)
    row = page.account_rows[0]
    row["enabled_var"].set(0)
    page._set_account_enabled(row["account"].id, row["enabled_var"])
    window.root.update()
    assert service.called("set_account_enabled")
    calls = service.calls_to("set_account_enabled")
    assert calls[-1][1][1] is False


def test_remove_account_requires_confirm(window, service, notify_records):
    page = _page(window)
    page.account_rows[0]["remove_button"].invoke()
    window.root.update()
    assert notify_records["confirm"], "移除账号必须二次确认"
    assert service.called("remove_account")
    assert len(service.list_accounts()) == 1


def test_add_qq_account(window, service, notify_records):
    page = _page(window)
    page.qq_name_input.insert(0, "工作 QQ（示例）")
    page.qq_allowlist_input.insert(0, "同事甲（示例），同事乙（示例）")
    page.add_qq_button.invoke()
    window.root.update()

    assert service.called("add_qq_account")
    args = service.calls_to("add_qq_account")[-1][1]
    assert args[0] == "工作 QQ（示例）"
    assert args[1] == ["同事甲（示例）", "同事乙（示例）"]
    assert len(service.list_accounts()) == 3
    assert notify_records["info"]
    assert page.qq_name_input.get() == ""


def test_add_qq_account_requires_name(window, service, notify_records):
    page = _page(window)
    page.add_qq_button.invoke()
    window.root.update()
    assert notify_records["warning"]
    assert service.called("add_qq_account") is False


def test_auth_code_help_explains_what_it_is(window):
    page = _page(window)
    assert page.auth_help_panel.winfo_manager() == ""
    page.auth_help_button.invoke()
    window.root.update()
    assert page.auth_help_panel.winfo_manager() == "pack"
    text = page.auth_help_panel.cget("text")
    for keyword in ("不是你的 QQ 密码", "16 位", "mail.qq.com", "IMAP", "Windows 凭据管理器"):
        assert keyword in text, f"授权码帮助缺少：{keyword}"
    # 授权码输入框必须是密码回显
    assert page.mail_auth_code_input.cget("show") == "*"


def test_add_mail_account_with_auth_code(window, service, notify_records):
    page = _page(window)
    page.mail_address_input.insert(0, "test@qq.com")
    page.mail_name_input.insert(0, "测试邮箱")
    page.mail_auth_code_input.insert(0, "1234567890abcdef")
    page.add_mail_button.invoke()
    window.root.update()

    assert service.called("add_mail_account")
    assert service.called("set_mail_credential")
    assert service._mail_credentials == {"test@qq.com": 16}
    assert page.mail_auth_code_input.get() == ""  # 输入后立刻清空，不留明文
    assert "Windows 凭据管理器" in page.mail_status_label.cget("text")


def test_save_and_test_mail_credential(window, service):
    page = _page(window)
    page.mail_address_input.insert(0, "test@qq.com")
    page.mail_auth_code_input.insert(0, "abcdef1234567890")
    page.save_mail_credential_button.invoke()
    window.root.update()
    assert service._mail_credentials.get("test@qq.com") == 16
    assert page.mail_auth_code_input.get() == ""

    page.test_mail_credential_button.invoke()
    window.root.update()
    assert "连接成功" in page.mail_status_label.cget("text")


def test_delete_mail_credential(window, service):
    service.set_mail_credential("test@qq.com", "x" * 16)
    page = _page(window)
    page.mail_address_input.insert(0, "test@qq.com")
    page.delete_mail_credential_button.invoke()
    window.root.update()
    assert "test@qq.com" not in service._mail_credentials
    assert "已删除" in page.mail_status_label.cget("text")


def test_whitelist_toggle_calls_service(window, service):
    page = _page(window)
    row = page.whitelist_rows[0]
    row["enabled_var"].set(0)
    page._on_whitelist_toggle(row["conversation"].id, row["enabled_var"])
    window.root.update()
    assert service.called("set_conversation_enabled")
    calls = service.calls_to("set_conversation_enabled")
    assert calls[-1][1][1] is False


def test_refresh_conversations(window, service):
    page = _page(window)
    page.refresh_conversations_button.invoke()
    window.root.update()
    assert service.called("refresh_conversations")
    assert "已刷新" in page.whitelist_status_label.cget("text")


def test_pause_checkbox_calls_service(window, service):
    page = _page(window)
    assert page.pause_var.get() == 0
    page.pause_var.set(1)
    page._on_pause_toggled()
    window.root.update()
    assert service.called("pause_collection")
    page.pause_var.set(0)
    page._on_pause_toggled()
    window.root.update()
    assert service.called("resume_collection")


def test_save_collection_settings(window, service):
    page = _page(window)
    page.interval_spin.delete(0, "end")
    page.interval_spin.insert(0, "30")
    page.foreground_var.set(0)
    service.reset_calls()
    page.save_collection_button.invoke()
    window.root.update()

    assert service.called("update_settings")
    settings = service.get_settings()
    assert settings.auto_scan_interval_minutes == 30
    assert settings.show_foreground_warning is False


def test_external_ai_save_blocked_without_consent(window, service, notify_records):
    page = _page(window)
    page.ai_mode_var.set(1)
    service.reset_calls()
    page.save_ai_settings_button.invoke()
    window.root.update()

    assert notify_records["warning"], "未同意外部 AI 时保存必须被阻止并提示"
    assert service.called("update_settings") is False
    assert service.get_settings().external_ai_consent is False


def test_external_ai_save_allowed_with_consent(window, service):
    service.grant_consent(ConsentScope.EXTERNAL_AI)
    page = _page(window)
    page.refresh()
    assert page.ai_mode_var.get() == 1
    page.external_url_input.delete(0, "end")
    page.external_url_input.insert(0, "https://example.invalid/v1")
    page.external_model_input.delete(0, "end")
    page.external_model_input.insert(0, "demo-model")
    service.reset_calls()
    page.save_ai_settings_button.invoke()
    window.root.update()

    assert service.called("update_settings")
    settings = service.get_settings()
    assert settings.external_ai_base_url == "https://example.invalid/v1"
    assert settings.external_ai_model == "demo-model"


def test_grant_external_consent_via_upload_dialog(window, service, monkeypatch):
    page = _page(window)
    service.reset_calls()

    class AcceptedUpload:
        def __init__(self, window_, **kwargs):
            pass

        def show(self):
            return True

    monkeypatch.setattr(settings_page_module, "ExternalAIUploadDialog", AcceptedUpload)
    page.grant_external_button.invoke()
    window.root.update()
    assert service.called("grant_consent")
    assert service.get_settings().external_ai_consent is True
    assert "已同意" in page.external_consent_label.cget("text")


def test_revoke_external_consent(window, service, notify_records):
    service.grant_consent(ConsentScope.EXTERNAL_AI)
    page = _page(window)
    page.refresh()
    service.reset_calls()
    page.revoke_external_button.invoke()
    window.root.update()
    assert notify_records["confirm"]
    assert service.called("revoke_consent")
    assert service.get_settings().external_ai_consent is False


def test_external_key_is_stored_without_plaintext(window, service):
    page = _page(window)
    page.external_key_input.insert(0, "sk-testplaceholder1234567890")
    page.save_external_key_button.invoke()
    window.root.update()
    assert service.called("set_external_ai_key")
    assert service.has_external_ai_key() is True
    assert page.external_key_input.get() == ""
    assert "凭据管理器" in page.external_status_label.cget("text")


def test_delete_all_data_requires_double_confirmation(window, service, monkeypatch, notify_records):
    page = _page(window)

    class ConfirmedDialog:
        def __init__(self, window_):
            self.confirmed = True

        def show(self):
            return True

    monkeypatch.setattr(settings_page_module, "ConfirmDeleteDataDialog", ConfirmedDialog)
    page.delete_all_data_button.invoke()
    window.root.update()
    assert service.called("delete_all_local_data")
    assert notify_records["info"]


def test_delete_all_data_cancelled_does_nothing(window, service, monkeypatch, notify_records):
    page = _page(window)

    class CancelledDialog:
        def __init__(self, window_):
            self.confirmed = False

        def show(self):
            return False

    monkeypatch.setattr(settings_page_module, "ConfirmDeleteDataDialog", CancelledDialog)
    service.reset_calls()
    page.delete_all_data_button.invoke()
    window.root.update()
    assert service.called("delete_all_local_data") is False


def test_export_diagnostics(window, service, notify_records):
    page = _page(window)
    page.export_diagnostics_button.invoke()
    window.root.update()
    assert service.called("export_diagnostics")
    assert notify_records["info"]
    title, text = notify_records["info"][-1]
    assert "诊断" in title
    assert "不包含" in text or "不含" in text


def test_load_and_clear_demo_data(window, service, notify_records):
    service.clear_demo_data()
    page = _page(window)
    page.refresh()
    assert "没有加载演示数据" in page.demo_status_label.cget("text")

    page.load_demo_button.invoke()
    window.root.update()
    assert service.called("load_demo_data")
    assert service.is_demo_active() is True
    assert notify_records["info"]

    notify_records["confirm_result"] = True
    page.clear_demo_button.invoke()
    window.root.update()
    assert service.called("clear_demo_data")
    assert service.is_demo_active() is False


def test_storage_stats_and_consent_history(window, service):
    service.grant_consent(ConsentScope.QQ_READ, confirmation_text="测试同意")
    page = _page(window)
    page.refresh()
    stats_text = page.storage_stats_label.cget("text")
    assert "消息 6 条" in stats_text
    assert "待办" in stats_text
    assert "数据目录" in stats_text

    entries = [page.consent_history_list.get(index) for index in range(page.consent_history_list.size())]
    assert any("读取 QQ 会话" in text for text in entries)
    assert any("测试同意" in text for text in entries)


def test_about_section_states_read_only(window):
    page = _page(window)
    text = page.about_label.cget("text")
    assert "只读" in text
    assert "不会发送" in text
    assert "外部 AI" in text
