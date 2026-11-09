"""明确显示已保存授权范围（用户要求：软件里不要让人迷糊）。"""

from __future__ import annotations


def test_settings_shows_authorized_scope(window, service):
    service.add_qq_account("演示QQ", ["演示会话A", "演示会话B"])
    settings = window.pages["settings"]
    settings.refresh()
    text = settings.qq_authorized_status.cget("text")
    assert "已授权会读取" in text
    assert "演示会话A" in text and "演示会话B" in text
    assert "不会读取" in text


def test_picker_shows_saved_scope(window, service):
    from social_secretary.ui.dialogs import QQSessionPickerDialog

    service.add_qq_account("演示QQ", ["演示会话A"])
    dialog = QQSessionPickerDialog(window)
    try:
        text = dialog.saved_auth_label.text()
    finally:
        dialog.destroy()
    assert "当前已保存的授权范围" in text
    assert "演示会话A" in text
    assert "不在这里不代表授权被删除" in text
