"""回复草稿页测试：生成本机草稿、编辑保存、复制、外部 AI 同意门控（Tkinter）。"""

from __future__ import annotations

from social_secretary.contracts import DraftKind
from social_secretary.ui.pages import drafts_page as drafts_page_module


def _page(window):
    return window.pages["drafts"]


def test_lists_replyable_messages(window):
    page = _page(window)
    assert len(page.messages) == 5  # fake 的 6 条消息里有 5 条不是「我」发的
    assert page.current_message() is not None


def test_generate_local_drafts(window, service):
    page = _page(window)
    page.generate_local_button.invoke()
    window.root.update()
    assert service.called("generate_drafts")
    assert len(page.drafts()) == 1
    assert page.drafts()[0].kind == DraftKind.LOCAL_RULES
    assert page.draft_list.size() == 1
    assert page.editor_text().strip()
    assert "未联网" in page.status_label.cget("text")
    assert str(page.copy_button.cget("state")) == "normal"
    assert str(page.save_button.cget("state")) == "normal"


def test_style_selection_is_passed_to_service(window, service):
    page = _page(window)
    page.style_combo.set("礼貌")
    page.generate_local_button.invoke()
    window.root.update()
    calls = service.calls_to("generate_drafts")
    assert calls[-1][1][1] == ["礼貌"]
    assert page.drafts()[0].style == "礼貌"


def test_edit_and_save_draft(window, service):
    page = _page(window)
    page.generate_local_button.invoke()
    window.root.update()
    page.set_editor_text("我自己改的回复内容")
    page.save_button.invoke()
    window.root.update()
    calls = service.calls_to("save_draft_edit")
    assert calls
    assert calls[-1][1][1] == "我自己改的回复内容"
    assert "已保存" in page.status_label.cget("text")


def test_copy_draft_to_clipboard(window, service):
    page = _page(window)
    page.generate_local_button.invoke()
    window.root.update()
    expected = page.editor_text()
    page.copy_button.invoke()
    window.root.update()
    assert window.root.clipboard_get() == expected
    assert "复制" in page.status_label.cget("text")
    assert "粘贴" in page.status_label.cget("text")


def test_copy_empty_draft_shows_info(window, notify_records):
    page = _page(window)
    page.copy_draft()
    assert notify_records["info"]


def test_external_ai_blocked_without_consent(window, service, notify_records):
    page = _page(window)
    assert service.get_settings().external_ai_consent is False
    service.reset_calls()
    page.generate_external_button.invoke()
    window.root.update()
    assert notify_records["warning"]
    assert service.called("generate_drafts") is False
    assert "阻止" in page.status_label.cget("text") or "未同意" in page.status_label.cget("text")


def test_external_ai_cancelled_at_upload_confirm(window, service, monkeypatch):
    service.update_settings(external_ai_consent=True)
    page = _page(window)
    service.reset_calls()
    seen: dict = {}

    class RejectedUpload:
        def __init__(self, window_, **kwargs):
            seen.update(kwargs)

        def show(self):
            return False

    monkeypatch.setattr(drafts_page_module, "ExternalAIUploadDialog", RejectedUpload)
    page.generate_external_button.invoke()
    window.root.update()
    assert service.called("generate_drafts") is False
    assert seen  # 确认弹窗被创建，并带上上传范围信息
    assert "取消" in page.status_label.cget("text")


def test_external_ai_generates_after_upload_confirm(window, service, monkeypatch):
    service.update_settings(external_ai_consent=True)
    page = _page(window)
    service.reset_calls()

    class AcceptedUpload:
        def __init__(self, window_, **kwargs):
            pass

        def show(self):
            return True

    monkeypatch.setattr(drafts_page_module, "ExternalAIUploadDialog", AcceptedUpload)
    page.generate_external_button.invoke()
    window.root.update()
    assert service.called("generate_drafts")
    assert page.drafts()
    assert page.drafts()[0].kind == DraftKind.EXTERNAL_LLM
    assert "外部 AI" in page.status_label.cget("text")


def test_drafts_page_has_no_send_button(window):
    page = _page(window)
    texts: list[str] = []

    def walk(widget):
        for child in widget.winfo_children():
            try:
                texts.append(str(child.cget("text")))
            except Exception:  # noqa: BLE001
                pass
            walk(child)

    walk(page)
    assert "复制到剪贴板" in texts
    # 没有任何按钮/标签的完整文字是「发送」，页面上只有「不会发送」这类说明
    assert "发送" not in [text.strip() for text in texts]
