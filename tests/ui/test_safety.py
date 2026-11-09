"""安全静态审计：确认 UI 层没有任何发送 QQ 消息 / 发送邮件 / 远端修改能力。

审计对象：``src/social_secretary/ui`` 全部源码 + ``contracts.ServiceAPI`` 协议方法名。
本测试不读取真实数据，只做源码字符串与控件文字检查。同时确认新 UI 不 import PySide6/PyQt。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import social_secretary.ui as ui_package
from social_secretary.contracts import ServiceAPI, ConsentScope

FORBIDDEN_TOKENS = (
    "import PySide6",
    "from PySide6",
    "import PyQt",
    "from PyQt",
    "smtplib",
    "sendmail",
    "send_message",
    "send_email",
    "SendKeys",
    "keybd_event",
    "pyautogui",
    "uiautomation",
    "imaplib",
    "win32api",
    "requests.post",
    "urllib.request",
)

FORBIDDEN_SERVICE_METHODS = ("send", "delete_message", "delete_remote", "modify_message", "append_message")


def _walk_widget_texts(widget) -> list[str]:
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
    return texts


def test_ui_sources_have_no_sending_capability():
    root = Path(ui_package.__file__).resolve().parent
    sources = sorted(root.rglob("*.py"))
    assert sources, "没有找到 UI 源码"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{path.name} 出现疑似发送/自动化能力：{token}"


def test_service_api_has_no_sending_methods():
    method_names = [name for name in dir(ServiceAPI) if not name.startswith("_")]
    for forbidden in FORBIDDEN_SERVICE_METHODS:
        assert all(forbidden not in name for name in method_names), f"ServiceAPI 出现可疑方法：{forbidden}"


def test_main_window_buttons_never_send(window):
    buttons = []

    def walk(node):
        for child in node.winfo_children():
            if isinstance(child, tk.Button):
                buttons.append(str(child.cget("text")))
            walk(child)

    walk(window.root)
    assert buttons, "主窗口应该有按钮"
    assert all("发送" not in text for text in buttons)


def test_drafts_page_has_copy_but_no_send(window):
    drafts_page = window.pages["drafts"]
    buttons = []

    def walk(node):
        for child in node.winfo_children():
            if isinstance(child, tk.Button):
                buttons.append(str(child.cget("text")))
            walk(child)

    walk(drafts_page)
    assert any("复制" in text for text in buttons)
    assert all("发送" not in text for text in buttons)


def test_external_ai_is_off_by_default(service):
    settings = service.get_settings()
    assert settings.external_ai_consent is False
    assert settings.external_ai_base_url == ""


def test_dangerous_actions_are_guarded_by_dialogs(window):
    from social_secretary.ui.dialogs import (
        ConfirmDeleteDataDialog,
        ConsentWizard,
        ExternalAIUploadDialog,
        ForegroundWarningDialog,
    )

    delete_dialog = ConfirmDeleteDataDialog(window)
    try:
        text = "\n".join(_walk_widget_texts(delete_dialog))
        assert "无法恢复" in text
        assert "不会删除" in text
        assert str(delete_dialog.delete_button.cget("state")) == "disabled"
    finally:
        delete_dialog._cancel()

    upload_dialog = ExternalAIUploadDialog(window, endpoint="https://example.invalid", message_count=1)
    try:
        assert str(upload_dialog.confirm_button.cget("state")) == "disabled"
    finally:
        upload_dialog._cancel()

    wizard = ConsentWizard(window)
    try:
        assert str(wizard.accept_button.cget("state")) == "disabled"
        assert len(wizard.checks) == 5
    finally:
        wizard._cancel()

    warning = ForegroundWarningDialog(window, seconds=1)
    try:
        assert str(warning.confirm_button.cget("state")) == "disabled"
    finally:
        warning._cancel()


def test_consent_scopes_are_part_of_contract():
    assert ConsentScope.QQ_READ.value == "qq_read"
    assert ConsentScope.MAIL_READ.value == "mail_read"
    assert ConsentScope.EXTERNAL_AI.value == "external_ai"
