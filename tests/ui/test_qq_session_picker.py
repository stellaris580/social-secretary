"""P0-2x：QQ 会话选择器（隐私优先）UI 测试。"""

from __future__ import annotations


def _dialog(window, *, auto_confirm: bool = False):
    from social_secretary.ui.dialogs import QQSessionPickerDialog

    return QQSessionPickerDialog(window, auto_confirm=auto_confirm)


def _select(dialog, indexes: list[int]) -> None:
    for index in indexes:
        dialog.session_vars[index].set(1)
    dialog._update_apply_button()


def test_session_picker_button_object_exists_but_not_on_main_ui(window):
    # P0 界面减法：按钮对象保留，但主界面不显示（设置页仍有同一个按钮）。
    assert hasattr(window, "btn_qq_session_picker")
    assert "选择要读取的 QQ 会话" in window.btn_qq_session_picker.cget("text")
    assert window.btn_qq_session_picker.cget("state") == "normal"
    assert window.btn_qq_session_picker.winfo_manager() == ""
    settings = window.pages["settings"]
    assert hasattr(settings, "qq_session_picker_button")
    assert "选择要读取的 QQ 会话" in settings.qq_session_picker_button.cget("text")


def test_cancel_before_loading_calls_nothing(window, service):
    dialog = _dialog(window)
    try:
        dialog._cancel()
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
    assert not service.called("list_qq_visible_sessions")
    assert not service.called("apply_qq_session_selection")
    assert window.service.get_settings().qq_read_experimental is False


def test_loading_sessions_reads_titles_default_unchecked(window, service):
    dialog = _dialog(window)
    try:
        result = dialog.load_sessions()
        assert result["ok"] is True
        assert len(dialog.sessions) >= 1
        assert all(var.get() == 0 for var in dialog.session_vars)  # 默认全部不勾选
        assert dialog.apply_button.cget("state") == "disabled"
        assert "只读取勾选的会话（0 个）" in dialog.apply_button.cget("text")
        calls = service.calls_to("list_qq_visible_sessions")
        assert calls and calls[-1][2]["user_confirmed"] is True
        assert not service.called("run_scan")
    finally:
        dialog.destroy()


def test_zero_selection_cannot_apply(window, service):
    dialog = _dialog(window, auto_confirm=True)
    try:
        dialog.load_sessions()
        result = dialog.confirm_selection()
        assert result["ok"] is False
        assert not service.called("apply_qq_session_selection")
        assert "请先勾选" in dialog.status_text()
    finally:
        dialog.destroy()


def test_select_two_and_confirm_applies_selection(window, service):
    dialog = _dialog(window, auto_confirm=True)
    try:
        dialog.load_sessions()
        _select(dialog, [0, 1])
        assert "只读取勾选的会话（2 个）" in dialog.apply_button.cget("text")
        result = dialog.confirm_selection()
        assert result["ok"] is True
        assert result["selected_count"] == 2
        assert result["enabled_count"] == 2
        calls = service.calls_to("apply_qq_session_selection")
        assert calls and calls[-1][1] == (2,)  # 只记录数量，不把标题写日志/记录
        assert "已保存" in dialog.status_text()
        assert "成功" not in dialog.status_text()
        assert not service.called("run_scan")
        assert service.get_settings().qq_read_experimental is True
        assert "小张" not in repr(service.calls)
    finally:
        dialog.destroy()


def test_secondary_confirm_cancel_does_not_apply(window, service, notify_records):
    notify_records["confirm_result"] = False
    dialog = _dialog(window)
    try:
        dialog.load_sessions()
        _select(dialog, [0, 1])
        result = dialog.confirm_selection()
        assert result["ok"] is False
        assert not service.called("apply_qq_session_selection")
        assert "已取消" in dialog.status_text()
        assert notify_records["confirm"]
        assert service.get_settings().qq_read_experimental is False
    finally:
        dialog.destroy()


def test_list_failure_is_red_and_no_success(window, service):
    service.fail_method("list_qq_visible_sessions", "演示列表读取失败")
    dialog = _dialog(window)
    try:
        result = dialog.load_sessions()
        assert result["ok"] is False
        assert "失败" in dialog.status_text()
        assert "成功" not in dialog.status_text()
        assert dialog.status_label.cget("fg") == window.theme.color("danger")
    finally:
        dialog.destroy()


def test_apply_failure_is_red_and_no_success(window, service):
    dialog = _dialog(window, auto_confirm=True)
    try:
        dialog.load_sessions()
        _select(dialog, [0])
        service.fail_method("apply_qq_session_selection", "演示保存失败")
        result = dialog.confirm_selection()
        assert result["ok"] is False
        assert "失败" in dialog.status_text()
        assert "成功" not in dialog.status_text()
        assert dialog.status_label.cget("fg") == window.theme.color("danger")
    finally:
        dialog.destroy()


def test_settings_button_calls_same_window_opener(window, service, monkeypatch):
    settings = window.pages["settings"]
    captured = {}

    def fake_open(*, auto_confirm=False):
        captured["called"] = True
        captured["auto_confirm"] = auto_confirm
        return {"ok": True, "applied": False}

    monkeypatch.setattr(window, "open_qq_session_picker", fake_open)
    settings.qq_session_picker_button.invoke()
    assert captured["called"] is True
    assert captured["auto_confirm"] is False


def test_window_auto_confirm_reads_list_without_apply_or_scan(window, service):
    result = window.open_qq_session_picker(auto_confirm=True)
    assert result["ok"] is True
    assert len(result["sessions"]) >= 1
    assert result["applied"] is False
    calls = service.calls_to("list_qq_visible_sessions")
    assert calls and calls[-1][2]["user_confirmed"] is True
    assert not service.called("apply_qq_session_selection")
    assert not service.called("run_scan")
    assert service.get_settings().qq_read_experimental is False
