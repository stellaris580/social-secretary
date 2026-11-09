"""待办页测试：渲染、完成、忽略、显示已处理、错误提示。"""

from __future__ import annotations

from social_secretary.contracts import TodoStatus


def _page(window):
    return window.pages["todos"]


def test_todo_page_lists_open_todos(window):
    page = _page(window)
    assert len(page.rows) == 3
    assert set(page.todo_ids()) == {"todo-1", "todo-2", "todo-3"}
    assert "待处理" in page.summary_label.cget("text")


def test_mark_done_updates_service_and_list(window, service):
    page = _page(window)
    page.rows[0]["done_button"].invoke()
    window.root.update()
    assert service.called("update_todo_status")
    calls = service.calls_to("update_todo_status")
    assert calls[-1][1][1] == TodoStatus.DONE
    assert len(page.rows) == 2


def test_dismiss_updates_service(window, service):
    page = _page(window)
    page.rows[0]["dismiss_button"].invoke()
    window.root.update()
    calls = service.calls_to("update_todo_status")
    assert calls[-1][1][1] == TodoStatus.DISMISSED
    assert len(page.rows) == 2


def test_show_done_checkbox_includes_done_items(window):
    page = _page(window)
    page.show_done_var.set(1)
    page.refresh()
    assert len(page.rows) == 4
    assert "共 4 条待办" in page.summary_label.cget("text")
    assert "todo-4" in page.todo_ids()


def test_update_failure_shows_error_and_keeps_list(window, service, notify_records):
    page = _page(window)
    service.fail_method("update_todo_status", "演示更新失败")
    page.rows[0]["done_button"].invoke()
    window.root.update()
    assert notify_records["error"]
    assert len(page.rows) == 3


def test_empty_state(window, service):
    for todo_id in ("todo-1", "todo-2", "todo-3"):
        service.update_todo_status(todo_id, TodoStatus.DONE)
    page = _page(window)
    page.refresh()
    assert page.rows == []
    assert "当前没有待办" in page.summary_label.cget("text")
