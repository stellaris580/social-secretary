"""对话框测试：首次运行同意向导、前台警告、删除确认、外部 AI 上传确认、来源/消息详情。"""

from __future__ import annotations

import tkinter as tk

from social_secretary.contracts import ConsentScope
from social_secretary.ui import dialogs
from social_secretary.ui.dialogs import (
    ConfirmDeleteDataDialog,
    ConsentWizard,
    ExternalAIUploadDialog,
    ForegroundWarningDialog,
    MessageDetailDialog,
    SourceDetailDialog,
    about_text,
)


def _all_text(widget) -> str:
    texts: list[str] = []

    def walk(node):
        for child in node.winfo_children():
            try:
                text = child.cget("text")
                if text:
                    texts.append(str(text))
            except Exception:  # noqa: BLE001
                pass
            walk(child)

    walk(widget)
    return "\n".join(texts)


def test_consent_wizard_requires_every_checkbox(window):
    wizard = ConsentWizard(window)
    try:
        assert len(wizard.checks) == 5
        assert str(wizard.accept_button.cget("state")) == "disabled"
        assert wizard.selected_consents == []

        for check in wizard.checks[:-1]:
            check._var.set(1)
        wizard._update_accept_state()
        assert str(wizard.accept_button.cget("state")) == "disabled"

        wizard.set_all_checked(True)
        assert str(wizard.accept_button.cget("state")) == "normal"
        assert wizard.is_fully_confirmed() is True
        assert wizard.selected_consents == [ConsentScope.QQ_READ, ConsentScope.MAIL_READ]
    finally:
        wizard._cancel()


def test_consent_wizard_copy_mentions_safety_boundaries(window):
    wizard = ConsentWizard(window)
    try:
        text = _all_text(wizard)
        for keyword in (
            "只读",
            "不能发送",
            "Windows 凭据管理器",
            "不联网",
            "外部 AI",
            "打断",
            "授权码",
            "16 位",
        ):
            assert keyword in text, f"同意向导缺少关键说明：{keyword}"
    finally:
        wizard._cancel()


def test_foreground_warning_countdown_blocks_then_enables(window):
    dialog = ForegroundWarningDialog(window, seconds=3)
    try:
        assert str(dialog.confirm_button.cget("state")) == "disabled"
        assert "3 秒后" in dialog.countdown_label.cget("text")
        dialog._tick()
        dialog._tick()
        assert str(dialog.confirm_button.cget("state")) == "disabled"
        dialog._tick()
        assert str(dialog.confirm_button.cget("state")) == "normal"
        dialog._accept()
        assert dialog.confirmed is True
    finally:
        dialog._cancel()


def test_foreground_warning_can_be_cancelled(window):
    dialog = ForegroundWarningDialog(window, seconds=0)
    try:
        assert str(dialog.confirm_button.cget("state")) == "normal"
        dialog._cancel()
        assert dialog.confirmed is False
    finally:
        dialog._cancel()


def test_delete_data_requires_checkbox_and_typed_word(window):
    dialog = ConfirmDeleteDataDialog(window)
    try:
        assert str(dialog.delete_button.cget("state")) == "disabled"

        dialog._check_var.set(1)
        assert str(dialog.delete_button.cget("state")) == "disabled"  # 还要输入「删除」

        dialog._type_var.set("删")
        assert str(dialog.delete_button.cget("state")) == "disabled"
        dialog._type_var.set("删除吧")
        assert str(dialog.delete_button.cget("state")) == "disabled"
        dialog._type_var.set("删除")
        assert str(dialog.delete_button.cget("state")) == "normal"

        dialog._accept()
        assert dialog.confirmed is True
    finally:
        dialog._cancel()


def test_delete_data_dialog_lists_scope(window):
    dialog = ConfirmDeleteDataDialog(window)
    try:
        text = _all_text(dialog)
        assert "本机保存的消息" in text
        assert "不会删除" in text
        assert "无法恢复" in text
    finally:
        dialog._cancel()


def test_external_ai_upload_dialog_requires_consent_and_shows_scope(window):
    dialog = ExternalAIUploadDialog(
        window,
        model_label="demo-model",
        endpoint="https://example.invalid/v1",
        message_count=2,
    )
    try:
        assert str(dialog.confirm_button.cget("state")) == "disabled"
        dialog._check_var.set(1)
        dialog._update_state()
        assert str(dialog.confirm_button.cget("state")) == "normal"

        text = _all_text(dialog)
        assert "demo-model" in text
        assert "https://example.invalid/v1" in text
        assert "不会上传" in text
        assert "2 条" in text

        dialog._accept()
        assert dialog.consent_granted is True
    finally:
        dialog._cancel()


def test_source_detail_dialog_shows_origin_fields(window, service):
    detail = service.get_source_detail("msg-1")
    dialog = SourceDetailDialog(window, detail)
    try:
        assert dialog.value_labels["会话"].cget("text") == "小张（示例）"
        assert dialog.value_labels["账号类型"].cget("text") == "QQ"
        assert "界面" in dialog.value_labels["时间来源"].cget("text")
        assert dialog.value_labels["来源定位"].cget("text")
        text = _all_text(dialog)
        assert "凭据" in text or "授权码" in text
        assert dialog.detail.message_id == "msg-1"
    finally:
        dialog._cancel()


def test_message_detail_can_open_source_dialog(window, service, monkeypatch):
    message = next(item for item in service.list_messages() if item.id == "msg-2")
    dialog = MessageDetailDialog(window, message, service)
    opened: dict = {}

    class FakeSourceDialog:
        def __init__(self, window_, detail):
            opened["detail"] = detail

        def show(self):
            return True

    monkeypatch.setattr(dialogs, "SourceDetailDialog", FakeSourceDialog)
    try:
        assert dialog.message.id == "msg-2"
        dialog.source_button.invoke()
        window.root.update()
        assert opened["detail"].message_id == "msg-2"
    finally:
        dialog._cancel()


def test_about_text_states_read_only():
    text = about_text()
    assert "不能发送" in text or "不会发送" in text
    assert "只读" in text
    assert "外部 AI" in text


def test_modal_dialog_esc_key_cancels(window):
    dialog = ForegroundWarningDialog(window, seconds=0)
    try:
        dialog.event_generate("<Escape>")
        window.root.update()
        assert dialog.confirmed is False
    finally:
        dialog._cancel()
