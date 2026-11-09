"""UI 入口契约：``create_main_window(service)`` 与 ``run_ui(service)``（Tkinter）。"""

from __future__ import annotations


def test_create_main_window_returns_window_with_seven_pages(tk_root, cleanup_root, service):
    from social_secretary.ui import create_main_window

    window = create_main_window(service, root=tk_root)
    assert window.root.winfo_exists()
    assert "AI 社交秘书" in window.root.title()
    assert len(window.pages) == 7
    assert window.current_page_key() == "summary"
    assert set(window.nav_buttons) == {
        "summary",
        "inbox",
        "todos",
        "drafts",
        "sources",
        "import",
        "settings",
    }


def test_nav_titles_are_simplified_chinese(window):
    titles = [window.nav_buttons[key].cget("text") for key in window.nav_buttons]
    assert titles == [
        "今日摘要",
        "收件箱",
        "待办",
        "回复草稿",
        "消息来源",
        "聊天导入（实验）",
        "设置与隐私",
    ]


def test_ui_package_exports_contract_functions():
    import social_secretary.ui as ui

    assert callable(ui.create_main_window)
    assert callable(ui.run_ui)


def test_run_ui_returns_exit_code_and_shows_window(tk_root, cleanup_root, service, monkeypatch):
    import social_secretary.ui as ui

    service.update_settings(first_run_completed=True)
    captured: dict = {}
    real_create = ui.create_main_window

    def wrapped(service_):
        window = real_create(service_, root=tk_root)
        captured["window"] = window
        return window

    monkeypatch.setattr(ui, "create_main_window", wrapped)
    result = ui.run_ui(service, _mainloop=lambda: 0)
    assert result == 0
    window = captured["window"]
    window.root.update()  # 处理 after(0) 的首次运行检查
    assert window.root.winfo_exists()
    assert window.root.state() in {"normal", "zoomed"}


def test_run_ui_does_not_shutdown_service(tk_root, cleanup_root, service, monkeypatch):
    import social_secretary.ui as ui

    service.update_settings(first_run_completed=True)
    captured: dict = {}
    real_create = ui.create_main_window

    def wrapped(service_):
        window = real_create(service_, root=tk_root)
        captured["window"] = window
        return window

    monkeypatch.setattr(ui, "create_main_window", wrapped)
    ui.run_ui(service, _mainloop=lambda: 0)
    captured["window"].root.update()
    assert service.called("shutdown") is False, "服务关闭必须由调用方（app.py）负责"
