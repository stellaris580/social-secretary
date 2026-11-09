"""P2 B2：消息中心「处理状态」与筛选测试。"""

from __future__ import annotations

import tkinter as tk

from social_secretary.contracts import MessageWorkStatus


def _page(window):
    return window.pages["sources"]


def _select_first(page):
    page.tree.selection_set("0")
    page._on_tree_select()


def test_work_status_hooks_exist(window):
    page = _page(window)
    assert isinstance(page.status_filter_var, tk.StringVar)
    assert callable(page.set_status_filter)
    assert callable(page.mark_status)
    assert set(page.status_buttons) == {
        MessageWorkStatus.UNHANDLED.value,
        MessageWorkStatus.NEEDS_REPLY.value,
        MessageWorkStatus.DONE.value,
        MessageWorkStatus.NO_REPLY.value,
    }
    assert page.work_status_counts_label is not None
    assert page.status_filter_var.get() == ""  # 默认全部处理状态，不隐藏


def test_default_unhandled_and_no_auto_change(window, service):
    page = _page(window)
    assert page.row_count() == 6
    assert not service.called("set_message_work_status")
    assert "处理状态：" in page.work_status_counts_label.cget("text")
    assert "未处理 6" in page.work_status_counts_label.cget("text")
    assert "重要且未处理/待回复：2 条" in page.work_status_counts_label.cget("text")
    assert all(str(button.cget("state")) == "disabled" for button in page.status_buttons.values())


def test_mark_needs_reply_persists_and_updates_counts(window, service):
    page = _page(window)
    _select_first(page)
    assert all(str(button.cget("state")) == "normal" for button in page.status_buttons.values())
    selected_id = page.selected_message_id()
    result = page.mark_status("needs_reply")
    assert result is not None and result["ok"] is True
    assert service.calls_to("set_message_work_status")[-1][1] == (selected_id, "needs_reply")
    # 列表徽章更新
    page.set_status_filter("needs_reply")
    assert page.row_count() == 1
    values = page.tree.item(page.tree.get_children()[0], "values")
    assert values[6] == "待回复"
    # 计数更新
    assert "待回复 1" in page.work_status_counts_label.cget("text")
    assert "未处理 5" in page.work_status_counts_label.cget("text")
    assert "已标记为「待回复」" in page.work_status_feedback_label.cget("text")


def test_mark_done_and_no_reply(window, service):
    page = _page(window)
    _select_first(page)
    assert page.mark_status("done")["ok"] is True
    assert page.mark_status("no_reply")["ok"] is True
    # 同一条消息先后改成 done→no_reply，最终只有 no_reply 一条
    page.set_status_filter("done")
    assert page.row_count() == 0
    page.set_status_filter("no_reply")
    assert page.row_count() == 1
    page.set_status_filter("")
    assert page.row_count() == 6


def test_status_filter_values(window):
    page = _page(window)
    assert page.set_status_filter("unhandled") is True
    assert page.status_filter_var.get() == "unhandled"
    assert page.row_count() == 6
    assert page.set_status_filter("bad-value") is False
    page.set_status_filter("")
    assert page.row_count() == 6


def test_source_filter_qq_and_mail(window):
    page = _page(window)
    page.source_combo.current(2)  # 邮箱
    page._reload_messages()
    assert page.row_count() == 2
    assert set(page.message_ids()) == {"msg-2", "msg-6"}
    page.source_combo.current(1)  # QQ
    page._reload_messages()
    assert set(page.message_ids()) == {"msg-1", "msg-3", "msg-4", "msg-5"}


def test_mark_status_failure_is_red_and_no_success(window, service, notify_records):
    page = _page(window)
    _select_first(page)
    service.fail_method("set_message_work_status", "演示保存失败")
    result = page.mark_status("needs_reply")
    assert result is None
    feedback = page.work_status_feedback_label.cget("text")
    assert "标记失败" in feedback
    assert "成功" not in feedback
    assert page.work_status_feedback_label.cget("fg") == window.theme.color("danger")
    assert notify_records["error"]
    assert service.get_settings() is not None


def test_mark_without_selection_shows_info(window, service, notify_records):
    page = _page(window)
    assert page.mark_status("done") is None
    assert notify_records["info"]
    assert not service.called("set_message_work_status")


def test_demo_marker_visible_in_rows(window):
    page = _page(window)
    children = page.tree.get_children()
    assert children
    for iid in children:
        values = page.tree.item(iid, "values")
        assert values[0] == "演示"
        assert values[1] in ("QQ", "邮箱")
        assert values[6] in ("未处理", "待回复", "已处理", "无需回复")


def test_filters_and_marks_never_trigger_scan_or_account_probe(window, service):
    page = _page(window)
    page.set_status_filter("unhandled")
    page.source_combo.current(2)
    page._reload_messages()
    page.tree.selection_set("0")
    page._on_tree_select()
    page.mark_status("done")
    assert not service.called("run_scan")
    assert not service.called("test_account")
    assert not service.called("quick_qq_read")
    assert not service.called("sync_mail")
