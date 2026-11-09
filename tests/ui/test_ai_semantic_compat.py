"""追加：与 app-core/intel 实际返回结构对齐的测试（分类键/provider/uncertainty_notes）。"""

from __future__ import annotations


def _summary(window):
    return window.pages["summary"]


def test_conversation_classification_keys_mapped_to_sections(window):
    page = _summary(window)
    data = {
        "ok": True,
        "provider": "local_rules",
        "conversation_title": "测试会话",
        "overview": "（合成）会话概述",
        "items": [],
        "questions": [
            {"kind": "question", "text": "问题一", "source_message_ids": ["msg-1"], "due_hint": ""}
        ],
        "requests": [
            {"kind": "request", "text": "请求一", "source_message_ids": ["msg-2"], "uncertain": True}
        ],
        "deadlines": [
            {"kind": "deadline", "text": "截止一", "source_message_ids": [], "due_hint": "周五"}
        ],
        "promises": [{"kind": "promise", "text": "承诺一", "source_message_ids": []}],
        "unresolved": [{"kind": "unresolved", "text": "未解决一", "source_message_ids": []}],
        "chitchat": [{"kind": "chitchat", "text": "闲聊一", "source_message_ids": []}],
        "uncertainty_notes": ["（合成）规则可能漏判"],
        "usage": {"calls": 1, "tokens": 0},
        "error": None,
    }
    dialog = page.show_ai_summary(data)
    assert dialog.section_titles == ["问题", "请求", "截止", "承诺", "未解决", "闲聊"]
    assert len(dialog.source_buttons) == 2
    # due_hint / uncertain 标注显示在条目文字里
    texts_list: list[str] = []

    def collect(node):
        for child in node.winfo_children():
            try:
                value = child.cget("text")
                if value:
                    texts_list.append(str(value))
            except Exception:  # noqa: BLE001
                pass
            collect(child)

    collect(dialog.sections_frame)
    texts = "\n".join(texts_list)
    assert "截止提示：周五" in texts
    assert "不确定" in texts
    assert "规则可能漏判" in dialog.uncertain_label.cget("text")


def test_daily_brief_sections_keys_mapped_to_chinese_titles(window):
    page = _summary(window)
    data = {
        "ok": True,
        "provider": "local_rules",
        "day": "2026-11-02",
        "sections": {
            "must_today": [{"text": "必须处理一", "source_message_ids": ["msg-1"]}],
            "soon": [{"text": "近期一", "source_message_ids": []}],
            "batch_reply": [{"text": "可集中回复一", "source_message_ids": []}],
            "fyi": [{"text": "仅供了解一", "source_message_ids": []}],
        },
        "caveats": ["（合成）请核对原文"],
        "usage": {"calls": 1, "tokens": 0},
        "error": None,
    }
    dialog = page.show_ai_summary(data, title="AI 每日简报")
    assert dialog.section_titles == ["今天必须处理", "近期", "可集中回复", "仅供了解"]
    assert "请核对原文" in dialog.uncertain_label.cget("text")


def test_external_llm_provider_label(window):
    page = _summary(window)
    dialog = page.show_ai_summary(
        {"provider": "external_llm", "overview": "（合成）", "sections": [], "uncertainty_notes": []}
    )
    assert "外部 AI" in dialog.provider_text()


def test_semantic_draft_accepts_text_and_uncertainty_notes(window, service):
    from social_secretary.ui.dialogs import MessageDetailDialog

    message = service.list_messages()[0]

    def fake_generate(message_id, *, style="友好", use_external=False):
        return {
            "ok": True,
            "provider": "external_llm",
            "message_id": message_id,
            "style": style,
            "text": f"（合成）{style}草稿正文",
            "context_message_ids": [message_id],
            "uncertainty_notes": ["（合成）请人工核对"],
            "usage": {"calls": 1, "tokens": 8},
            "error": None,
        }

    service.generate_semantic_draft = fake_generate
    dialog = MessageDetailDialog(window, message, service)
    try:
        result = dialog.generate_semantic_draft()
        assert result is not None and result["ok"] is True
        assert "外部 AI" in dialog.draft_provider_label.cget("text")
        assert "请人工核对" in dialog.draft_uncertainty_label.cget("text")
        assert "草稿正文" in dialog.draft_text.get("1.0", "end-1c")
    finally:
        dialog.destroy()
