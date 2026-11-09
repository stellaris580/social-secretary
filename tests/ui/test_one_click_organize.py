"""P0：主界面减法 + 唯一主按钮「一键整理所有消息」测试。"""

from __future__ import annotations

from social_secretary.contracts import DailySummary


def test_main_ui_has_one_primary_organize_button(window):
    assert hasattr(window, "btn_organize")
    assert window.btn_organize.cget("text") == "一键整理所有消息"
    assert window.btn_organize.winfo_manager() == "pack"
    assert window.btn_organize.cget("bg") == window.theme.color("primary")
    # 旧入口不再出现在主界面（对象仍保留以兼容测试）
    for name in ("btn_scan", "btn_generate", "btn_quick_qq", "btn_qq_session_picker"):
        assert hasattr(window, name), name
        assert getattr(window, name).winfo_manager() == "", name
    # 管理授权会话/暂停采集仍可用，但暂停不在主操作区
    assert window.btn_manage_sources.winfo_manager() == "pack"
    assert window.btn_pause.winfo_manager() == "pack"
    assert window.btn_pause.master is window.status_bar
    settings = window.pages["settings"]
    assert settings.advanced_scan_button.winfo_manager() == "pack"
    assert settings.advanced_generate_button.winfo_manager() == "pack"


def test_organize_all_calls_service_once_and_shows_counts(window, service, monkeypatch, notify_records):
    # 不应弹出逐窗口/逐会话确认框
    from social_secretary.ui import main_window as main_window_module

    class ExplodingDialog:
        def __init__(self, *args, **kwargs):
            raise AssertionError("一键整理不应打开确认/会话选择对话框")

    monkeypatch.setattr(main_window_module, "QuickQQReadDialog", ExplodingDialog)
    monkeypatch.setattr(main_window_module, "QQSessionPickerDialog", ExplodingDialog)

    result = window.organize_all()
    assert result["ok"] is True
    assert window.last_organize_result is result
    calls = service.calls_to("organize_all")
    assert len(calls) == 1
    assert calls[0][2]["foreground_ack"] is True
    status = window.status_label.cget("text")
    assert "整理完成" in status
    assert "已授权 QQ 会话 3 个" in status
    assert "成功 2 个" in status
    assert "失败 0 个" in status
    assert "邮箱成功 1" in status
    assert window.current_page_key() == "summary"
    page = window.pages["summary"]
    assert page.summary() is not None
    assert isinstance(page.summary(), DailySummary)


def test_organize_partial_failure_does_not_claim_complete(window, service, notify_records):
    service.organize_qq_failed = 1
    result = window.organize_all()
    assert result["qq_failed"] == 1
    status = window.status_label.cget("text")
    assert "部分失败" in status
    assert "整理完成" not in status
    assert "失败" in status
    assert window.status_label.cget("fg") == window.theme.color("danger")
    assert "QQ 会话整理失败" in status  # 普通中文原因


def test_organize_unconfigured_sources_prompts_not_fake_success(window, service, notify_records):
    service.clear_demo_data()
    result = window.organize_all()
    assert result["ok"] is False
    status = window.status_label.cget("text")
    assert "还没有可整理的账号" in status
    assert "成功" not in status
    assert notify_records["warning"]
    assert not service.called("quick_qq_read")
    assert not service.called("run_scan")


def test_organize_only_mail_still_works(window, service, notify_records):
    service.clear_demo_data()
    service.add_mail_account("only@qq.com", "only@qq.com")
    result = window.organize_all()
    assert result["configured"]["mail"] is True
    assert result["configured"]["qq"] is False
    assert result["mail_success"] >= 1
    status = window.status_label.cget("text")
    assert "邮箱成功" in status
    assert "成功" in status
    assert window.current_page_key() == "summary"


def test_organize_service_failure_is_red_and_no_complete(window, service, notify_records):
    service.fail_method("organize_all", "演示整理故障")
    result = window.organize_all()
    assert result.get("ok") is False
    status = window.status_label.cget("text")
    assert "失败" in status
    assert "整理完成" not in status
    assert "成功" not in status
    assert window.status_label.cget("fg") == window.theme.color("danger")
    assert notify_records["error"]


def test_organize_busy_state(window, service):
    captured = {}
    original = service.organize_all

    def checking(*, foreground_ack=False):
        captured["state"] = str(window.btn_organize.cget("state"))
        captured["text"] = window.btn_organize.cget("text")
        return original(foreground_ack=foreground_ack)

    service.organize_all = checking
    window.organize_all()
    assert captured["state"] == "disabled"
    assert "正在整理所有消息" in captured["text"]
    assert str(window.btn_organize.cget("state")) == "normal"
    assert window.btn_organize.cget("text") == "一键整理所有消息"


def test_organize_does_not_call_per_session_quick_read_or_scan(window, service, notify_records):
    window.organize_all()
    assert not service.called("quick_qq_read")
    assert not service.called("run_scan")
    assert not service.called("sync_mail")
    assert not service.called("generate_conversation_semantic")
    assert not service.called("generate_daily_brief")


def test_manage_sources_opens_picker(window, monkeypatch):
    captured = {}

    def fake_picker(*, auto_confirm=False):
        captured["called"] = True
        return {"ok": True, "applied": False}

    monkeypatch.setattr(window, "open_qq_session_picker", fake_picker)
    window.btn_manage_sources.invoke()
    assert captured["called"] is True


def test_old_advanced_methods_still_callable(window, service):
    # 兼容：旧对象/方法仍可直接调用（高级入口在设置页）。
    window.generate_summary()
    assert service.called("generate_daily_summary")
    window.btn_pause.invoke()
    assert window.btn_pause.cget("text") in ("暂停采集", "恢复采集")
