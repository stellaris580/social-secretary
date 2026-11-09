"""LocalDraftProvider 与 draft_styles 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from social_secretary.contracts import (
    DraftContext,
    DraftKind,
    DraftProvider,
    ReplyDraft,
)
from social_secretary.intel.drafts import (
    DRAFT_STYLES,
    LocalDraftProvider,
    detect_intent,
    draft_styles,
)

LOCAL_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 10, 28, 10, 0, tzinfo=LOCAL_TZ)

INTENT_SAMPLES = [
    ("question", "你觉得这件事怎么样？"),
    ("request", "麻烦你明天把报告发我"),
    ("invite", "周末一起吃饭吗？"),
    ("notice", "通知：周五系统维护"),
    ("urge", "尽快回复我"),
    ("thanks", "谢谢你帮忙"),
    ("greeting", "在吗"),
    ("other", "嗯嗯，好的"),
]


@pytest.fixture
def provider() -> LocalDraftProvider:
    return LocalDraftProvider(now_provider=lambda: FIXED_NOW)


def _context(style: str = "简洁") -> DraftContext:
    return DraftContext(conversation_title="项目群", style=style)


# ---------------------------------------------------------------------------
# 构造与协议
# ---------------------------------------------------------------------------

def test_noarg_provider_is_protocol():
    provider = LocalDraftProvider()
    assert provider.kind is DraftKind.LOCAL_RULES
    assert isinstance(provider, DraftProvider)


def test_draft_styles():
    styles = draft_styles()
    assert isinstance(styles, tuple)
    assert styles == DRAFT_STYLES
    assert len(styles) == 4
    assert len(set(styles)) == len(styles)
    assert set(styles) == {"简洁", "礼貌", "稍后回复", "确认收到"}
    assert all(isinstance(style, str) and style for style in styles)


def test_intent_detection():
    for intent, text in INTENT_SAMPLES:
        assert detect_intent(text) == intent, f"{text} -> {detect_intent(text)}"


# ---------------------------------------------------------------------------
# 候选用例：每种风格 × 每种意图都应返回 2~3 条
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style", DRAFT_STYLES)
@pytest.mark.parametrize(("intent", "text"), INTENT_SAMPLES, ids=[item[0] for item in INTENT_SAMPLES])
def test_propose_returns_two_or_three(provider, make_message, style, intent, text):
    message = make_message(text, sender="李雷", conversation_title="项目群")
    drafts = provider.propose(message, DraftContext(conversation_title="项目群", style=style))
    assert 2 <= len(drafts) <= 3
    ids = {draft.id for draft in drafts}
    assert len(ids) == len(drafts)
    for draft in drafts:
        assert isinstance(draft, ReplyDraft)
        assert draft.kind is DraftKind.LOCAL_RULES
        assert draft.style == style
        assert draft.message_id == message.id
        assert draft.text.strip()
        assert "{" not in draft.text and "}" not in draft.text
        assert draft.requires_external_consent is False
        assert draft.model_label
        assert draft.created_at is not None
        assert draft.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 意图相关的内容（不必逐字匹配，断言“像那么回事”）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("intent", "text", "keywords"),
    [
        ("question", "你觉得这件事怎么样？", ("回复", "答复", "告诉", "确认")),
        ("request", "麻烦你明天把报告发我", ("处理", "安排", "跟进", "记下", "收到")),
        ("invite", "周末一起吃饭吗？", ("时间", "安排", "参加", "回复")),
        ("notice", "通知：周五系统维护", ("收到", "了解", "明白", "通知", "告知")),
        ("urge", "尽快回复我", ("尽快", "马上", "抓紧", "跟进", "处理")),
        ("thanks", "谢谢你帮忙", ("不客气", "客气")),
        ("greeting", "在吗", ("在", "请说", "你好")),
    ],
)
def test_intent_reply_content(provider, make_message, intent, text, keywords):
    message = make_message(text, sender="李雷")
    drafts = provider.propose(message, _context("简洁"))
    joined = "".join(draft.text for draft in drafts)
    assert any(keyword in joined for keyword in keywords)


def test_polite_style_uses_name(provider, make_message):
    message = make_message("麻烦你明天把报告发我", sender="李雷")
    drafts = provider.propose(message, _context("礼貌"))
    assert any("李雷" in draft.text for draft in drafts)


def test_notice_style_mentions_conversation_title(provider, make_message):
    message = make_message("通知：周五系统维护", sender="李雷")
    drafts = provider.propose(message, _context("简洁"))
    assert any("项目群" in draft.text for draft in drafts)


def test_empty_sender_falls_back_to_title(make_message):
    provider = LocalDraftProvider()
    message = make_message("在吗", sender="")
    drafts = provider.propose(message, DraftContext(conversation_title="项目群", style="礼貌"))
    assert drafts
    assert any("项目群" in draft.text for draft in drafts)


# ---------------------------------------------------------------------------
# 风格兜底 / 不使用 user_note / 时间注入
# ---------------------------------------------------------------------------

def test_unknown_style_falls_back_to_default(provider, make_message):
    message = make_message("明天开会", sender="李雷")
    drafts = provider.propose(message, DraftContext(conversation_title="项目群", style="夸张风"))
    assert drafts
    assert all(draft.style == "简洁" for draft in drafts)


@pytest.mark.parametrize("style", ["", None])
def test_empty_style_falls_back(provider, make_message, style):
    message = make_message("明天开会", sender="李雷")
    drafts = provider.propose(message, DraftContext(conversation_title="项目群", style=style))
    assert all(draft.style == "简洁" for draft in drafts)


def test_user_note_is_not_leaked(provider, make_message):
    message = make_message("明天开会", sender="李雷")
    notes = "备注：带上水果和充电器"
    drafts = provider.propose(
        message, DraftContext(conversation_title="项目群", style="礼貌", user_note=notes)
    )
    assert all(notes not in draft.text for draft in drafts)


def test_created_at_uses_injected_now(make_message):
    provider = LocalDraftProvider(now_provider=lambda: FIXED_NOW)
    drafts = provider.propose(make_message("明天开会"), _context())
    assert all(draft.created_at == FIXED_NOW for draft in drafts)
