"""主窗口测试：创建、导航、生成摘要、暂停、读取、首次运行、消息详情（Tkinter）。"""

from __future__ import annotations

from social_secretary.contracts import ConsentScope


def test_main_window_initial_state(window):
    assert window.current_page_key() == "summary"
    assert window.btn_generate.cget("text") == "生成今日摘要"
    assert window.btn_pause.cget("text") == "暂停采集"
    assert "只读" in window.readonly_label.cget("text")
    assert "最后摘要" in window.status_label.cget("text")
    assert window.is_collection_paused() is False


def test_navigation_switches_pages(window):
    for key in ("summary", "todos", "drafts", "sources", "settings"):
        window.show_page(key)
        assert window.current_page_key() == key
        assert window.current_page() is window.pages[key]


def test_generate_summary_renders_category_counts(window, service, notify_records):
    summary = window.generate_summary()
    assert summary is not None
    assert window.pages["summary"].category_counts() == {"important": 2, "normal": 1, "fyi": 2}
    assert service.called("generate_daily_summary")
    assert "今日摘要已生成" in window.status_label.cget("text")
    assert "演示模式（仅合成数据）" in window.demo_badge.cget("text")
    assert window.demo_badge.winfo_manager() == "pack"


def test_generate_summary_error_is_actionable(window, service, notify_records):
    service.fail_method("generate_daily_summary", "演示故障")
    assert window.generate_summary() is None
    assert notify_records["error"], "生成失败时必须给出错误提示"
    title, text = notify_records["error"][-1]
    assert "失败" in title
    assert "设置与隐私" in text or "重试" in text
    assert str(window.btn_generate.cget("state")) == "normal"


def test_pause_button_calls_service_and_updates_ui(window, service, notify_records):
    assert window.is_collection_paused() is False
    window.btn_pause.invoke()
    assert service.called("pause_collection")
    assert window.is_collection_paused() is True
    assert window.btn_pause.cget("text") == "恢复采集"
    assert "已暂停" in window.status_label.cget("text")

    window.btn_pause.invoke()
    assert service.called("resume_collection")
    assert window.is_collection_paused() is False
    assert window.btn_pause.cget("text") == "暂停采集"


def test_run_scan_shows_foreground_warning_and_reads(window, service, monkeypatch, notify_records):
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=False, qq_read_experimental=True)

    from social_secretary.ui import main_window as main_window_module

    class FakeWarning:
        def __init__(self, window_):
            pass

        def show(self):
            return True

    monkeypatch.setattr(main_window_module, "ForegroundWarningDialog", FakeWarning)

    results = window.run_scan()
    assert results is not None
    assert service.called("run_scan")
    call = service.calls_to("run_scan")[-1]
    assert call[2]["foreground_ack"] is True
    assert "读取完成" in window.status_label.cget("text")
    assert len(window.last_scan_results() or []) == len(results)


def test_run_scan_cancelled_at_foreground_warning(window, service, monkeypatch, notify_records):
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=False, qq_read_experimental=True)

    from social_secretary.ui import main_window as main_window_module

    class CancelledWarning:
        def __init__(self, window_):
            pass

        def show(self):
            return False

    monkeypatch.setattr(main_window_module, "ForegroundWarningDialog", CancelledWarning)
    service.reset_calls()

    assert window.run_scan() is None
    assert service.called("run_scan") is False
    assert "已取消读取" in window.status_label.cget("text")


def test_run_scan_blocked_while_paused(window, service, notify_records):
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=True)
    service.reset_calls()

    assert window.run_scan() is None
    assert service.called("run_scan") is False
    assert notify_records["warning"]


def test_run_scan_blocked_without_consent(window, service, notify_records):
    service.update_settings(collection_paused=False, qq_read_consent=False, mail_read_consent=False)
    service.reset_calls()

    assert window.run_scan() is None
    assert service.called("run_scan") is False
    assert notify_records["warning"]
    title, text = notify_records["warning"][-1]
    assert "同意" in title or "同意" in text


def test_run_scan_failure_shows_error(window, service, notify_records):
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=False, show_foreground_warning=False)
    service.fail_method("run_scan", "演示读取失败")

    assert window.run_scan() is None
    assert notify_records["error"]
    assert "读取" in notify_records["error"][-1][0]


def test_maybe_show_first_run_skips_when_completed(window, service):
    service.update_settings(first_run_completed=True)
    assert window.maybe_show_first_run() is False


def test_maybe_show_first_run_applies_consents(window, service, monkeypatch, notify_records):
    service.update_settings(first_run_completed=False)

    from social_secretary.ui import main_window as main_window_module

    class FakeWizard:
        selected_consents = [ConsentScope.QQ_READ, ConsentScope.MAIL_READ]

        def __init__(self, window_):
            pass

        def show(self):
            return True

    monkeypatch.setattr(main_window_module, "ConsentWizard", FakeWizard)
    assert window.maybe_show_first_run() is True
    settings = service.get_settings()
    assert settings.first_run_completed is True
    assert settings.qq_read_consent is True
    assert settings.mail_read_consent is True
    assert service.called("grant_consent")


def test_first_run_rejected_keeps_reading_blocked(window, service, monkeypatch, notify_records):
    service.update_settings(first_run_completed=False)

    from social_secretary.ui import main_window as main_window_module

    class RejectedWizard:
        selected_consents: list = []

        def __init__(self, window_):
            pass

        def show(self):
            return False

    monkeypatch.setattr(main_window_module, "ConsentWizard", RejectedWizard)
    assert window.maybe_show_first_run() is False
    settings = service.get_settings()
    assert settings.first_run_completed is False
    assert settings.qq_read_consent is False
    assert notify_records["info"]


def test_apply_first_run_consents(window, service):
    window.apply_first_run_consents([ConsentScope.QQ_READ, ConsentScope.MAIL_READ])
    settings = service.get_settings()
    assert settings.first_run_completed is True
    assert settings.qq_read_consent is True
    assert settings.mail_read_consent is True
    assert "同意已保存" in window.status_label.cget("text")


def test_open_message_detail(window, service, monkeypatch):
    from social_secretary.ui import main_window as main_window_module

    opened: dict = {}

    class FakeDetail:
        def __init__(self, window_, message, service_=None):
            opened["message"] = message

        def show(self):
            return True

    monkeypatch.setattr(main_window_module, "MessageDetailDialog", FakeDetail)
    dialog = window.open_message_detail("msg-1")
    assert dialog is not None
    assert opened["message"].id == "msg-1"


def test_open_message_detail_missing(window, notify_records):
    assert window.open_message_detail("no-such-message") is None
    assert notify_records["info"]


def test_close_destroys_root(window, monkeypatch):
    destroyed: list[bool] = []
    monkeypatch.setattr(window.root, "destroy", lambda *args, **kwargs: destroyed.append(True))
    window.close()
    assert destroyed, "关闭窗口必须销毁 Tk 根窗口"


def test_status_shows_demo_mode(window, service):
    service.load_demo_data()
    window.refresh_all()
    assert window.demo_badge.winfo_manager() == "pack"
    assert "演示模式" in window.status_label.cget("text")


def test_set_status(window):
    window.set_status("测试状态")
    assert window.status_label.cget("text") == "测试状态"
