"""RuleClassifier 分类用例：重要 / 普通 / 仅了解 / 己方 / 群公告 / 广告 / 边界。"""

from __future__ import annotations

from datetime import datetime

import pytest

from social_secretary.contracts import Category, Classifier, ConversationKind, Priority
from social_secretary.intel.rules import ClassifierConfig, RuleClassifier


def classify(
    classifier: RuleClassifier,
    raw,
    *,
    kind: ConversationKind = ConversationKind.PRIVATE,
    priority: Priority = Priority.NORMAL,
):
    return classifier.classify(
        raw,
        conversation_title="测试会话",
        conversation_kind=kind,
        conversation_priority=priority,
    )


@pytest.fixture
def classifier() -> RuleClassifier:
    return RuleClassifier()


# ---------------------------------------------------------------------------
# 重要
# ---------------------------------------------------------------------------

def test_direct_at_me_important(classifier, make_raw):
    decision = classify(classifier, make_raw("@我 麻烦你今天下班前把周报发我"))
    assert decision.category is Category.IMPORTANT
    assert decision.score >= 4.0
    assert any("@我" in reason for reason in decision.reasons)


def test_named_mention_important():
    classifier = RuleClassifier(self_names=["张三"])
    decision = classify(
        classifier,
        # 注意：这里直接用 RawMessage 工厂的等价构造，避免依赖 fixture 作用域
        _raw("@张三 请明天下午3点前确认方案"),
        kind=ConversationKind.GROUP,
    )
    assert decision.category is Category.IMPORTANT
    assert any("张三" in reason for reason in decision.reasons)


def test_request_with_weekday_deadline_important(classifier, make_raw):
    decision = classify(classifier, make_raw("请务必在周五前提交报销单"))
    assert decision.category is Category.IMPORTANT
    assert any("截止" in reason or "截止时间" in reason for reason in decision.reasons)


def test_urgency_normal_then_high_priority_important(classifier, make_raw):
    raw = make_raw("尽快回复我")
    normal = classify(classifier, raw)
    high = classify(classifier, raw, priority=Priority.HIGH)
    assert normal.category is Category.NORMAL  # 单看文字还不够“重要”
    assert high.category is Category.IMPORTANT  # 高优先级会话提升
    assert high.score > normal.score


def test_mail_deadline_important(classifier, make_raw):
    decision = classify(
        classifier,
        make_raw("您的账单将于11月15日截止，请及时缴费"),
        kind=ConversationKind.MAIL,
    )
    assert decision.category is Category.IMPORTANT
    assert decision.score >= 4.0


# ---------------------------------------------------------------------------
# 普通
# ---------------------------------------------------------------------------

def test_plain_chat_normal(classifier, make_raw):
    decision = classify(classifier, make_raw("今天天气不错"))
    assert decision.category is Category.NORMAL


def test_private_request_without_time_normal(classifier, make_raw):
    decision = classify(classifier, make_raw("麻烦帮我拿一下快递"))
    assert decision.category is Category.NORMAL


def test_mail_question_normal(classifier, make_raw):
    decision = classify(
        classifier,
        make_raw("请问报销流程怎么走？"),
        kind=ConversationKind.MAIL,
    )
    assert decision.category is Category.NORMAL


def test_group_question_normal(classifier, make_raw):
    decision = classify(
        classifier,
        make_raw("明天下午3点开会吗"),
        kind=ConversationKind.GROUP,
    )
    assert decision.category is Category.NORMAL


def test_default_reason_present(classifier, make_raw):
    decision = classify(classifier, make_raw("嗯嗯"))
    assert decision.reasons
    assert isinstance(decision.reasons, tuple)


# ---------------------------------------------------------------------------
# 仅了解 / 低价值
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["好的", "收到", "嗯嗯", "OK", "知道了", "好的，收到", "谢谢"])
def test_short_ack_fyi(classifier, make_raw, text):
    decision = classify(classifier, make_raw(text))
    assert decision.category is Category.FYI


@pytest.mark.parametrize("text", ["哈哈哈", "[图片]", "嘿嘿，😂"])
def test_chat_noise_fyi(classifier, make_raw, text):
    decision = classify(classifier, make_raw(text))
    assert decision.category is Category.FYI


def test_marketing_fyi(classifier, make_raw):
    decision = classify(classifier, make_raw("【限时优惠】点击链接领取，先到先得"))
    assert decision.category is Category.FYI


def test_forward_link_fyi(classifier, make_raw):
    decision = classify(classifier, make_raw("转发：https://example.com/abc 看看这个"))
    assert decision.category is Category.FYI


def test_group_broadcast_not_important(classifier, make_raw):
    decision = classify(
        classifier,
        make_raw("@所有人 明天下午3点开会，请准时参加"),
        kind=ConversationKind.GROUP,
    )
    assert decision.category is not Category.IMPORTANT
    assert any("所有人" in reason for reason in decision.reasons)


def test_group_announcement_ad_fyi(classifier, make_raw):
    decision = classify(
        classifier,
        make_raw("@所有人【通知】群规更新，请勿发广告"),
        kind=ConversationKind.GROUP,
    )
    assert decision.category is Category.FYI


# ---------------------------------------------------------------------------
# 己方消息与边界
# ---------------------------------------------------------------------------

def test_self_message_demoted(classifier, make_raw):
    decision = classify(classifier, make_raw("请务必明天下午3点前提交", is_self=True))
    assert decision.category is not Category.IMPORTANT
    assert any("己方" in reason for reason in decision.reasons)


def test_self_message_short_ack_still_fyi(classifier, make_raw):
    decision = classify(classifier, make_raw("好的", is_self=True))
    assert decision.category is Category.FYI


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text_normal(classifier, make_raw, text):
    decision = classify(classifier, make_raw(text))
    assert decision.category is Category.NORMAL
    assert decision.reasons


def test_high_priority_can_raise_important(classifier, make_raw):
    raw = make_raw("明天开会请准时参加")
    assert classify(classifier, raw).category is Category.IMPORTANT
    lowered = classify(classifier, raw, priority=Priority.LOW)
    assert lowered.category is not Category.IMPORTANT


def test_important_reasons_are_explainable(classifier, make_raw):
    decision = classify(classifier, make_raw("请务必明天下午3点前提交报告"))
    assert decision.category is Category.IMPORTANT
    assert len(decision.reasons) >= 2
    assert any("（" in reason and "+" in reason for reason in decision.reasons)


def test_custom_thresholds_are_configurable(make_raw):
    classifier = RuleClassifier(ClassifierConfig(important_threshold=99.0))
    decision = classify(classifier, make_raw("请务必明天下午3点前提交报告"))
    assert decision.category is not Category.IMPORTANT


def test_classifier_is_protocol_and_noarg():
    assert isinstance(RuleClassifier(), Classifier)


def test_classify_accepts_all_conversation_kinds(classifier, make_raw):
    for kind in (ConversationKind.PRIVATE, ConversationKind.GROUP, ConversationKind.MAIL):
        decision = classify(classifier, make_raw("明天下午3点开会，请准时参加"), kind=kind)
        assert isinstance(decision.reasons, tuple)
        assert decision.category in (Category.IMPORTANT, Category.NORMAL, Category.FYI)


# ---------------------------------------------------------------------------
# 内部小工具（放最后，避免影响上面的可读性）
# ---------------------------------------------------------------------------

def _raw(text: str):
    from social_secretary.contracts import RawMessage

    return RawMessage(conversation_key="qq:1", sender="小明", is_self=False, text=text)
