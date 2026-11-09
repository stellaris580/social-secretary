"""P0-18：聊天导入（实验）页测试。"""

from __future__ import annotations

import tkinter as tk

SAMPLE = "[2026-11-01 10:00] 张三: 你好\n李四: 收到，下午见\n乱码行没有冒号"


def _page(window):
    return window.pages["import"]


def test_import_page_in_nav_before_settings(window):
    keys = list(window.nav_buttons)
    assert "import" in keys
    assert keys.index("import") == keys.index("settings") - 1
    assert window.nav_buttons["import"].cget("text") == "聊天导入（实验）"


def test_import_page_hooks_and_experimental_notice(window):
    page = _page(window)
    for name in ("set_text", "load_file", "parse_preview", "confirm_import", "status_text", "preview_stats"):
        assert callable(getattr(page, name)), name
    assert page.tree is not None
    texts = []

    def walk(node):
        for child in node.winfo_children():
            try:
                value = child.cget("text")
                if value:
                    texts.append(str(value))
            except Exception:  # noqa: BLE001
                pass
            walk(child)

    walk(page)
    joined = "\n".join(texts)
    for keyword in ("实验性", "不会自动读取 QQ", "只解析你手动选择/粘贴", "只保存在本机", "不联网", "不保证兼容"):
        assert keyword in joined, keyword


def test_preview_without_text_is_error_and_does_not_call_service(window, service):
    page = _page(window)
    result = page.parse_preview()
    assert result["ok"] is False
    assert "聊天文本为空" in page.status_text() or "请先" in page.status_text()
    assert not service.called("import_chat_text")


def test_parse_preview_counts_only_and_does_not_write(window, service):
    page = _page(window)
    page.set_text(SAMPLE)
    before = len(service.list_messages())
    result = page.parse_preview()
    assert result["ok"] is True
    stats = page.preview_stats()
    assert stats["ok"] is True
    assert stats["dry_run"] is True
    assert stats["conversation_count"] == 1
    assert stats["message_count"] == 2
    assert stats["skipped_count"] == 1
    assert stats["warning_count"] == 1
    assert stats["imported_count"] == 0
    calls = service.calls_to("import_chat_text")
    assert calls and calls[-1][2]["dry_run"] is True
    assert len(service.list_messages()) == before  # 预览不写库
    # 不展示正文：列表行只含元信息/字数
    rows = page.tree.get_children()
    assert len(rows) == 2
    values = page.tree.item(rows[0], "values")
    assert "你好" not in " ".join(str(item) for item in values)


def test_preview_failure_is_red_and_never_says_success(window, service):
    service.fail_method("import_chat_text", "演示解析故障")
    page = _page(window)
    page.set_text(SAMPLE)
    result = page.parse_preview()
    assert result["ok"] is False
    text = page.status_text()
    assert "失败" in text
    assert "成功" not in text
    assert page.status_label.cget("fg") == window.theme.color("danger")


def test_confirm_import_requires_preview(window, service):
    page = _page(window)
    page.set_text(SAMPLE)
    result = page.confirm_import()
    assert result["ok"] is False
    assert "解析预览" in page.status_text()
    assert not service.called("import_chat_text")


def test_confirm_import_writes_and_refreshes_sources(window, service):
    page = _page(window)
    page.set_text(SAMPLE)
    preview = page.parse_preview()
    assert preview["ok"] is True
    before = len(service.list_messages())
    result = page.confirm_import()
    assert result["ok"] is True
    assert result["imported_count"] == 2
    assert result["duplicate_count"] == 0
    assert len(service.list_messages()) == before + 2
    assert "已导入 2 条消息" in page.status_text()
    assert "成功" not in page.status_text()
    assert service.calls_to("import_chat_text")[-1][2]["dry_run"] is False


def test_import_duplicate_second_time_zero_new(window, service):
    page = _page(window)
    page.set_text(SAMPLE)
    page.parse_preview()
    page.confirm_import()
    page.set_text(SAMPLE)
    page.parse_preview()
    result = page.confirm_import()
    assert result["ok"] is True
    assert result["imported_count"] == 0
    assert result["duplicate_count"] == 2


def test_confirm_import_failure_never_says_success(window, service, notify_records):
    page = _page(window)
    page.set_text(SAMPLE)
    page.parse_preview()
    service.fail_method("import_chat_text", "演示导入故障")
    result = page.confirm_import()
    assert result["ok"] is False
    text = page.status_text()
    assert "失败" in text
    assert "成功" not in text
    assert page.status_label.cget("fg") == window.theme.color("danger")
    assert notify_records["error"]


def test_load_file_reads_text_and_sets_source(window, tmp_path):
    file = tmp_path / "聊天记录.txt"
    file.write_text(SAMPLE, encoding="utf-8")
    page = _page(window)
    assert page.load_file(file) is True
    assert "张三: 你好" in page.text_box.get("1.0", "end-1c")
    assert "聊天记录.txt" in page.source_label.cget("text")
    preview = page.parse_preview()
    assert preview["ok"] is True
    stats = page.preview_stats()
    assert stats["source_name"] == "聊天记录.txt"


def test_load_file_rejects_unsupported_suffix(window, tmp_path):
    file = tmp_path / "记录.exe"
    file.write_text("x", encoding="utf-8")
    page = _page(window)
    assert page.load_file(file) is False
    assert "不支持的文件类型" in page.status_text()


def test_set_text_resets_previous_preview(window):
    page = _page(window)
    page.set_text(SAMPLE)
    page.parse_preview()
    assert page.preview_stats()["ok"] is True
    page.set_text("王五: 新文本")
    assert page.preview_stats() == {}
    assert page.import_button.cget("state") == "disabled"


def test_capability_card_mentions_experimental_import(window):
    settings = window.pages["settings"]
    texts = []

    def walk(node):
        for child in node.winfo_children():
            try:
                value = child.cget("text")
                if value:
                    texts.append(str(value))
            except Exception:  # noqa: BLE001
                pass
            walk(child)

    walk(settings)
    joined = "\n".join(texts)
    assert "聊天导入" in joined
    assert "实验性" in joined
    assert "不会自动读取 QQ" in joined
