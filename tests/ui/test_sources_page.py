"""消息来源页测试：列表、筛选、来源详情弹窗（Tkinter）。"""

from __future__ import annotations

from social_secretary.ui.pages import sources_page as sources_page_module


def _page(window):
    return window.pages["sources"]


def test_sources_table_lists_all_messages(window):
    page = _page(window)
    assert page.row_count() == 6
    assert len(page.message_ids()) == 6
    assert len(page.account_combo.cget("values")) == 3  # 全部账号 + 2 个账号
    assert "共 6 条消息" in page.count_label.cget("text")
    assert str(page.detail_button.cget("state")) == "disabled"


def test_filter_by_account(window):
    page = _page(window)
    page.account_combo.current(2)  # QQ 邮箱（示例）
    page._on_account_changed()
    assert page.row_count() == 2
    assert set(page.message_ids()) == {"msg-2", "msg-6"}


def test_filter_by_category(window):
    page = _page(window)
    page.category_combo.current(1)  # 重要
    page._reload_messages()
    assert page.row_count() == 2


def test_filter_by_conversation(window):
    page = _page(window)
    page.conversation_combo.current(1)  # 小张（示例）
    page._reload_messages()
    assert page.row_count() == 1
    assert page.message_ids() == ["msg-1"]


def test_open_source_detail_dialog(window, service, monkeypatch):
    page = _page(window)
    opened: dict = {}

    class FakeDetailDialog:
        def __init__(self, window_, detail):
            opened["detail"] = detail

        def show(self):
            return True

    monkeypatch.setattr(sources_page_module, "SourceDetailDialog", FakeDetailDialog)
    page.tree.selection_set("0")
    page.open_detail()

    assert page.last_detail_dialog is not None
    detail = opened["detail"]
    assert detail.message_id
    assert detail.conversation_title
    assert "示例" in detail.conversation_title or "示例" in (detail.origin_label or "")


def test_open_detail_without_selection_shows_info(window, notify_records):
    page = _page(window)
    assert page.open_detail() is None
    assert notify_records["info"]


def test_open_detail_error_shows_error(window, service, notify_records):
    page = _page(window)
    service.fail_method("get_source_detail", "演示来源读取失败")
    page.tree.selection_set("0")
    assert page.open_detail() is None
    assert notify_records["error"]
