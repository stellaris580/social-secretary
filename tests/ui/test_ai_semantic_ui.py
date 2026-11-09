"""B/C：AI 语义摘要 / 每日简报 / 草稿与消息中心补充功能测试。"""

from __future__ import annotations

from social_secretary.contracts import Category, ConsentScope


def _summary(window):
    return window.pages["summary"]


def _sources(window):
    return window.pages["sources"]


def _select_first(page):
    page.tree.selection_set("0")
    page._on_tree_select()


# ---------------------------------------------------------------------------
# 按钮 / 本地 provider
# ---------------------------------------------------------------------------
def test_summary_ai_buttons_exist(window):
    page = _summary(window)
    assert "AI 会话摘要" in page.ai_conversation_summary_button.cget("text")
    assert "AI 每日简报" in page.ai_daily_brief_button.cget("text")
    assert page.ai_conversation_summary_button.winfo_manager() == "pack"
    assert page.ai_daily_brief_button.winfo_manager() == "pack"


def test_conversation_semantic_local_provider_and_sections(window, service):
    page = _summary(window)
    dialog = page.open_conversation_semantic(show=False)
    assert dialog is not None
    assert "本地规则" in dialog.provider_text()
    assert dialog.section_titles and "问题" in dialog.section_titles
    assert not service.called("run_scan")
    calls = service.calls_to("generate_conversation_semantic")
    assert calls and calls[-1][2]["use_external"] is False


def test_daily_brief_local_sections(window, service):
    page = _summary(window)
    dialog = page.open_daily_brief(show=False)
    assert dialog is not None
    assert dialog.section_titles == ["今天必须处理", "近期", "可集中回复", "仅供了解"]
    assert "本地规则" in dialog.provider_text()
    assert not service.called("run_scan")


def test_daily_brief_empty_data_shows_error(window, service, notify_records):
    page = _summary(window)
    empty = {
        "ok": False,
        "provider": "local_rules",
        "sections": [],
        "error": "今天没有可生成简报的消息。",
        "uncertain": [],
    }
    dialog = page.show_ai_summary(empty, title="AI 每日简报")
    assert "生成失败：今天没有可生成简报的消息。" in dialog.status_label.cget("text")
    assert dialog.status_label.cget("fg") == window.theme.color("danger")


def test_show_ai_summary_renders_uncertainty_and_source_buttons(window):
    page = _summary(window)
    data = {
        "provider": "local_rules",
        "overview": "（合成）概述",
        "sections": [
            {
                "title": "问题",
                "items": [
                    {"text": "（合成）待确认事项", "source_message_ids": ["msg-1"]},
                    {"text": "（合成）无来源事项", "source_message_ids": []},
                ],
            }
        ],
        "uncertain": ["（合成）请核对原文"],
        "usage": {"calls": 1, "tokens": 0},
    }
    dialog = page.show_ai_summary(data)
    assert "请核对原文" in dialog.uncertain_label.cget("text")
    assert len(dialog.source_buttons) == 1
    assert "本地规则" in dialog.provider_text()
    assert "本次用量" in dialog.usage_label.cget("text")


def test_source_button_opens_message_detail(window, service, monkeypatch):
    page = _summary(window)
    data = {
        "provider": "local_rules",
        "sections": [
            {"title": "问题", "items": [{"text": "事项", "source_message_ids": ["msg-1"]}]}
        ],
    }
    opened: dict = {}
    monkeypatch.setattr(window, "open_message_detail", lambda mid: opened.setdefault("id", mid))
    dialog = page.show_ai_summary(data)
    dialog.source_buttons[0].invoke()
    assert opened["id"] == "msg-1"


# ---------------------------------------------------------------------------
# 外部 AI 门控
# ---------------------------------------------------------------------------
def test_external_ai_blocked_without_consent(window, service, notify_records):
    page = _summary(window)
    assert page.open_daily_brief(use_external=True, show=False) is None
    assert notify_records["warning"]
    assert not service.called("generate_daily_brief")


def test_external_ai_provider_label_after_consent(window, service):
    service.grant_consent(ConsentScope.EXTERNAL_AI)
    page = _summary(window)
    dialog = page.open_daily_brief(use_external=True, show=False)
    assert dialog is not None
    assert "外部 AI" in dialog.provider_text()
    calls = service.calls_to("generate_daily_brief")
    assert calls and calls[-1][2]["use_external"] is True


def test_semantic_failure_is_red_and_no_success(window, service, notify_records):
    service.fail_method("generate_conversation_semantic", "演示语义失败")
    page = _summary(window)
    dialog = page.open_conversation_semantic(show=False)
    assert dialog is not None
    assert "失败" in page.ai_status_label.cget("text")
    assert "成功" not in page.ai_status_label.cget("text")
    assert "失败" in dialog.status_label.cget("text")


# ---------------------------------------------------------------------------
# 语义草稿（消息详情）
# ---------------------------------------------------------------------------
def test_semantic_draft_three_styles(window, service, monkeypatch):
    from social_secretary.ui.dialogs import MessageDetailDialog

    message = service.list_messages()[0]
    dialog = MessageDetailDialog(window, message, service)
    try:
        for style in ("正式", "简洁", "友好"):
            dialog.draft_style_combo.set(style)
            result = dialog.generate_semantic_draft()
            assert result is not None and result["ok"] is True
            assert style in dialog.draft_provider_label.cget("text")
            assert "本地规则" in dialog.draft_provider_label.cget("text")
            assert dialog.draft_text.get("1.0", "end-1c").strip()
            calls = service.calls_to("generate_semantic_draft")
            assert calls[-1][1][1] == style
        assert "上下文" in dialog.draft_provider_label.cget("text")
        assert not service.called("run_scan")
    finally:
        dialog.destroy()


def test_semantic_draft_failure_no_success(window, service, notify_records):
    from social_secretary.ui.dialogs import MessageDetailDialog

    message = service.list_messages()[0]
    service.fail_method("generate_semantic_draft", "演示草稿失败")
    dialog = MessageDetailDialog(window, message, service)
    try:
        result = dialog.generate_semantic_draft()
        assert result is not None and result["ok"] is False
        text = dialog.draft_status_label.cget("text")
        assert "失败" in text
        assert "成功" not in text
        assert dialog.draft_status_label.cget("fg") == window.theme.color("danger")
        assert notify_records["error"]
    finally:
        dialog.destroy()


# ---------------------------------------------------------------------------
# 消息中心补充：手动重要程度 / 合并 / 日期筛选
# ---------------------------------------------------------------------------
def test_importance_buttons_and_manual_category(window, service):
    page = _sources(window)
    _select_first(page)
    assert set(page.importance_buttons) == {
        Category.IMPORTANT.value,
        Category.NORMAL.value,
        Category.FYI.value,
    }
    assert all(str(button.cget("state")) == "normal" for button in page.importance_buttons.values())
    selected = page.selected_message_id()
    result = page.mark_category(Category.NORMAL.value)
    assert result is not None and result["ok"] is True
    assert service.calls_to("set_message_category")[-1][1] == (selected, Category.NORMAL.value)
    values = page.tree.item(page.tree.get_children()[0], "values")
    assert "普通" in values


def test_mark_category_failure_is_red(window, service, notify_records):
    page = _sources(window)
    _select_first(page)
    service.fail_method("set_message_category", "演示分类失败")
    assert page.mark_category(Category.FYI.value) is None
    text = page.work_status_feedback_label.cget("text")
    assert "失败" in text
    assert "成功" not in text
    assert notify_records["error"]


def test_merge_context_toggle_is_display_only(window, service):
    page = _sources(window)
    assert page.merge_context_var.get() is False
    assert page.row_count() == 6
    page.merge_context_var.set(True)
    page._reload_messages()
    merged_rows = page.row_count()
    assert merged_rows < 6
    assert any("合并" in page.tree.item(iid, "values")[7] for iid in page.tree.get_children())
    # 合并只影响显示，底层消息数量不变
    assert len(page.message_ids()) == 6
    assert not service.called("run_scan")
    page.merge_context_var.set(False)
    page._reload_messages()
    assert page.row_count() == 6


def test_date_filter_today_and_all(window):
    page = _sources(window)
    assert page.date_combo.cget("values")
    page.date_combo.current(1)  # 今天
    page._on_date_filter_changed()
    assert page.date_filter_var.get() == "today"
    assert page.row_count() == 6
    page.date_combo.current(0)
    page._on_date_filter_changed()
    assert page.row_count() == 6


def test_combined_filters_do_not_trigger_scan(window, service):
    page = _sources(window)
    page.set_status_filter("unhandled")
    page.source_combo.current(1)  # QQ
    page._reload_messages()
    page.category_combo.current(1)  # 重要
    page._reload_messages()
    assert page.row_count() >= 0
    assert not service.called("run_scan")
    assert not service.called("test_account")
    assert not service.called("generate_daily_brief")


def test_category_buttons_disabled_without_selection(window):
    page = _sources(window)
    # 刷新后无选中
    page._reload_messages()
    assert all(str(button.cget("state")) == "disabled" for button in page.importance_buttons.values())
