"""P0-25：一键读取当前 QQ 聊天（实验）UI 测试。"""

from __future__ import annotations

from social_secretary.contracts import ConsentScope


class _FakeDialog:
    result = True
    instances = 0

    def __init__(self, window, *args, **kwargs):
        type(self).instances += 1
        self.window = window

    def show(self):
        return type(self).result


def _patch_dialog(monkeypatch, result: bool) -> type:
    from social_secretary.ui import main_window as main_window_module

    class FakeDialog(_FakeDialog):
        pass

    FakeDialog.result = result
    FakeDialog.instances = 0
    monkeypatch.setattr(main_window_module, "QuickQQReadDialog", FakeDialog)
    return FakeDialog


def test_quick_qq_button_object_exists_but_not_on_main_ui(window):
    # P0 界面减法：方法对象保留以兼容测试，但不再 pack 到主界面（高级入口在设置页）。
    assert hasattr(window, "btn_quick_qq")
    assert "读取 QQ 当前聊天" in window.btn_quick_qq.cget("text")
    assert window.btn_quick_qq.cget("state") == "normal"
    assert window.btn_quick_qq.winfo_manager() == ""


def test_quick_qq_cancel_does_not_call_service(window, service, monkeypatch, notify_records):
    _patch_dialog(monkeypatch, False)
    window.btn_quick_qq.invoke()
    assert not service.called("quick_qq_read")
    assert "已取消" in window.status_label.cget("text")
    assert window.current_page_key() == "summary"


def test_quick_qq_confirm_success_shows_counts_and_refreshes(window, service, monkeypatch, notify_records):
    from social_secretary.ui import main_window as main_window_module

    dialog_cls = _patch_dialog(monkeypatch, True)
    window.btn_quick_qq.invoke()
    assert dialog_cls.instances == 1
    calls = service.calls_to("quick_qq_read")
    assert calls and calls[-1][2]["user_confirmed"] is True
    status = window.status_label.cget("text")
    assert "已读取当前 QQ 聊天：看到 3 个气泡" in status
    assert "本机新保存 2 条" in status
    assert "可能有重复" in status
    assert window.current_page_key() == "sources"
    # 一键读取成功后实验开关状态同步
    assert service.get_settings().qq_read_experimental is True
    assert window.pages["settings"].qq_experimental_var.get() is True


def test_quick_qq_failure_is_red_and_never_says_success(window, service, notify_records):
    service.fail_method("quick_qq_read", "演示一键读取失败")
    result = window.quick_qq_read(auto_confirm=True)
    assert result["ok"] is False
    status = window.status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status
    assert window.status_label.cget("fg") == window.theme.color("danger")
    assert notify_records["error"]


def test_quick_qq_busy_state_disables_button(window, service):
    captured = {}

    original = service.quick_qq_read

    def checking(*, user_confirmed=False):
        captured["state"] = window.btn_quick_qq.cget("state")
        captured["text"] = window.btn_quick_qq.cget("text")
        return original(user_confirmed=user_confirmed)

    service.quick_qq_read = checking
    result = window.quick_qq_read(auto_confirm=True)
    assert result["ok"] is True
    assert captured["state"] == "disabled"
    assert "正在读取" in captured["text"]
    assert window.btn_quick_qq.cget("state") == "normal"


def test_quick_qq_auto_confirm_skips_dialog(window, service, monkeypatch):
    from social_secretary.ui import main_window as main_window_module

    class ExplodingDialog:
        def __init__(self, *args, **kwargs):
            raise AssertionError("auto_confirm=True 时不应创建确认框")

    monkeypatch.setattr(main_window_module, "QuickQQReadDialog", ExplodingDialog)
    result = window.quick_qq_read(auto_confirm=True)
    assert result["ok"] is True


def test_settings_quick_qq_button_calls_same_logic(window, service, monkeypatch, notify_records):
    settings = window.pages["settings"]
    assert hasattr(settings, "quick_qq_read_button")
    assert "一键读取当前 QQ 聊天" in settings.quick_qq_read_button.cget("text")
    dialog_cls = _patch_dialog(monkeypatch, True)
    settings.quick_qq_read_button.invoke()
    assert dialog_cls.instances == 1
    assert service.called("quick_qq_read")
    assert settings.qq_experimental_var.get() is True
    assert "当前：已开启" in settings.qq_experimental_status.cget("text")


def test_fake_quick_qq_requires_confirmation(service):
    blocked = service.quick_qq_read()
    assert blocked["ok"] is False
    assert blocked["error"]
    assert service.quick_qq_read(user_confirmed=True)["ok"] is True


def test_quick_qq_dialog_auto_confirm(window):
    from social_secretary.ui.dialogs import QuickQQReadDialog

    dialog = QuickQQReadDialog(window, auto_confirm=True)
    assert dialog.show() is True


def test_summary_and_first_run_copy_use_one_button_flow(window):
    page = window.pages["summary"]
    page.show_summary(None)
    text = page.narrative_label.cget("text")
    assert "读取 QQ 当前聊天（实验）" in text
    assert "一次即可" in text

    window.apply_first_run_consents([ConsentScope.QQ_READ])
    status = window.status_label.cget("text")
    assert "读取 QQ 当前聊天（实验）" in status
    assert "一次即可" in status
