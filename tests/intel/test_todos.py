"""RuleTodoExtractor 待办提取用例：中文时间表达式、now 注入、防误判。

固定 now = 2026-10-28 10:00 +08:00（星期三）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from social_secretary.contracts import ExtractedTodo, RawMessage, TodoExtractor
from social_secretary.intel.rules import (
    RuleTodoExtractor,
    TodoConfig,
    parse_due_text,
)

LOCAL_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 10, 28, 10, 0, tzinfo=LOCAL_TZ)  # 周三


@pytest.fixture
def extractor() -> RuleTodoExtractor:
    return RuleTodoExtractor(now_provider=lambda: FIXED_NOW)


def _extract(extractor: RuleTodoExtractor, make_raw, text: str, **kwargs) -> list[ExtractedTodo]:
    return extractor.extract(make_raw(text, **kwargs))


# ---------------------------------------------------------------------------
# 时间表达式解析（≥20 组）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 相对日
        ("麻烦你明天下午3点前把报告发我", (2026, 10, 29, 15, 0)),
        ("后天上午10点开会，请准时参加", (2026, 10, 30, 10, 0)),
        ("请务必在周五前提交报销单", (2026, 10, 30, 18, 0)),
        ("周报周五前发我", (2026, 10, 30, 18, 0)),
        ("下周一上午9点半面试", (2026, 11, 2, 9, 30)),
        ("下下周一提交材料", (2026, 11, 9, 18, 0)),
        ("本周日晚上7点聚会", (2026, 11, 1, 19, 0)),
        ("今晚8点前回复我", (2026, 10, 28, 20, 0)),
        ("明早8点叫我起床", (2026, 10, 29, 8, 0)),
        ("周六聚餐", (2026, 10, 31, 18, 0)),
        ("周一上午9点例会", (2026, 11, 2, 9, 0)),
        # 绝对日期
        ("截止11月15日", (2026, 11, 15, 18, 0)),
        ("2026年11月15日截止", (2026, 11, 15, 18, 0)),
        ("12月1日前提交", (2026, 12, 1, 18, 0)),
        # N 天后 / 小时后 / 分钟后
        ("3天后交作业", (2026, 10, 31, 18, 0)),
        ("两天后提交材料", (2026, 10, 30, 18, 0)),
        ("2小时后开会", (2026, 10, 28, 12, 0)),
        ("半小时后给我回个电话", (2026, 10, 28, 10, 30)),
        ("30分钟后集合", (2026, 10, 28, 10, 30)),
        ("1小时后给我回电话", (2026, 10, 28, 11, 0)),
        # 只有钟点：未过点算今天、已过点算明天
        ("下午3点开会", (2026, 10, 28, 15, 0)),
        ("上午9点开会", (2026, 10, 29, 9, 0)),
        # 中午 12 点/中午 1 点
        ("明天中午12点吃饭", (2026, 10, 29, 12, 0)),
        ("明天中午1点开会", (2026, 10, 29, 13, 0)),
        # DDL
        ("DDL 是 下周三", (2026, 11, 4, 18, 0)),
    ],
)
def test_time_expressions(extractor, make_raw, text, expected):
    todos = _extract(extractor, make_raw, text)
    assert len(todos) == 1, f"{text} -> {todos}"
    due = todos[0].due_at
    assert due is not None
    assert (due.year, due.month, due.day, due.hour, due.minute) == expected
    assert due.tzinfo is not None
    assert due.utcoffset() == timedelta(hours=8)
    assert todos[0].due_text and todos[0].due_text in text


@pytest.mark.parametrize(
    ("text", "expected_due_text"),
    [
        ("明天下午3点前把报告发我", "明天下午3点前"),
        ("本周五下班前提交", "本周五下班前"),
        ("截止11月15日", "截止11月15日"),
        ("下周一上午9点半面试", "下周一上午9点半"),
        ("30分钟后集合", "30分钟后"),
    ],
)
def test_due_text_keeps_original_wording(extractor, make_raw, text, expected_due_text):
    todos = _extract(extractor, make_raw, text)
    assert len(todos) == 1
    assert todos[0].due_text == expected_due_text


def test_urgency_without_datetime(extractor, make_raw):
    todos = _extract(extractor, make_raw, "请尽快提交材料")
    assert len(todos) == 1
    assert todos[0].due_at is None
    assert todos[0].due_text == "尽快"
    assert todos[0].confidence >= 0.5


def test_reminder_without_time(extractor, make_raw):
    todos = _extract(extractor, make_raw, "记得带伞")
    assert len(todos) == 1
    assert todos[0].due_at is None
    assert todos[0].due_text is None
    assert todos[0].text == "记得带伞"


def test_reminder_and_activity(extractor, make_raw):
    todos = _extract(extractor, make_raw, "提醒我明天开会")
    assert len(todos) == 1
    assert todos[0].due_at is not None
    assert (todos[0].due_at.month, todos[0].due_at.day) == (10, 29)


def test_deadline_without_resolvable_day(extractor, make_raw):
    todos = _extract(extractor, make_raw, "月底前交房租")
    assert len(todos) == 1
    assert todos[0].due_at is None
    assert todos[0].due_text == "月底前"


# ---------------------------------------------------------------------------
# 多句 / 多待办 / 去重 / 上限
# ---------------------------------------------------------------------------

def test_multiple_sentences(extractor, make_raw):
    todos = _extract(extractor, make_raw, "明天上午10点开会。周五前提交报告。")
    assert len(todos) == 2
    assert (todos[0].due_at.month, todos[0].due_at.day) == (10, 29)
    assert (todos[1].due_at.month, todos[1].due_at.day) == (10, 30)


def test_duplicate_sentences_are_deduplicated(extractor, make_raw):
    todos = _extract(extractor, make_raw, "明天提交报告。明天提交报告。")
    assert len(todos) == 1


def test_max_todos_config(make_raw):
    extractor = RuleTodoExtractor(
        now_provider=lambda: FIXED_NOW, config=TodoConfig(max_todos=1)
    )
    todos = _extract(
        extractor,
        make_raw,
        "明天上午10点开会。周五前提交报告。下周一提交材料。",
    )
    assert len(todos) == 1


def test_todo_config_date_only_hour(make_raw):
    extractor = RuleTodoExtractor(
        now_provider=lambda: FIXED_NOW, config=TodoConfig(date_only_hour=9)
    )
    todos = _extract(extractor, make_raw, "截止11月15日")
    assert todos[0].due_at.hour == 9


# ---------------------------------------------------------------------------
# 不该生成待办的负例
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "收到，谢谢",
        "哈哈哈",
        "[图片]",
        "明天有空吗？",
        "明天一起吃饭吗？",
        "明天下午3点开会吗",
        "请提交报告",
        "今天天气不错",
        "周末愉快",
        "在吗",
    ],
)
def test_no_false_positive_todos(extractor, make_raw, text):
    assert _extract(extractor, make_raw, text) == []


def test_self_message_never_creates_todo(extractor, make_raw):
    todos = _extract(extractor, make_raw, "我明天下午3点把材料发给你", is_self=True)
    assert todos == []


def test_empty_text_never_creates_todo(extractor, make_raw):
    assert _extract(extractor, make_raw, "") == []
    assert _extract(extractor, make_raw, "   ") == []


def test_question_with_deadline_still_extracted(extractor, make_raw):
    todos = _extract(extractor, make_raw, "明天下午3点前能提交吗？")
    assert len(todos) == 1
    assert (todos[0].due_at.month, todos[0].due_at.day, todos[0].due_at.hour) == (10, 29, 15)


# ---------------------------------------------------------------------------
# now 注入 / 时区 / 协议 / 置信度
# ---------------------------------------------------------------------------

def test_now_injection_via_constructor(make_raw):
    extractor = RuleTodoExtractor(now_provider=lambda: FIXED_NOW)
    todos = _extract(extractor, make_raw, "明天下午3点前提交")
    assert (todos[0].due_at.month, todos[0].due_at.day) == (10, 29)


def test_now_injection_via_call_overrides_provider(make_raw):
    extractor = RuleTodoExtractor(now_provider=lambda: FIXED_NOW)
    other_now = datetime(2027, 1, 5, 10, 0, tzinfo=LOCAL_TZ)
    raw = make_raw("明天下午3点前提交")
    todos = extractor.extract(raw, now=other_now)
    assert (todos[0].due_at.year, todos[0].due_at.month, todos[0].due_at.day) == (2027, 1, 6)


def test_noarg_constructor_works(make_raw):
    extractor = RuleTodoExtractor()
    todos = extractor.extract(make_raw("明天下午3点前提交"))
    assert isinstance(todos, list)
    assert len(todos) == 1


def test_naive_now_is_treated_as_local(make_raw):
    naive = datetime(2026, 10, 28, 10, 0)
    extractor = RuleTodoExtractor()
    todos = extractor.extract(make_raw("明天下午3点前提交"), now=naive)
    assert todos[0].due_at.tzinfo is not None
    assert todos[0].due_at.utcoffset() == timedelta(hours=8)


def test_confidence_ranges(extractor, make_raw):
    strong = _extract(extractor, make_raw, "明天下午3点前把报告发我")[0]
    weak = _extract(extractor, make_raw, "记得带伞")[0]
    assert 0.5 <= weak.confidence < strong.confidence <= 0.95


def test_extractor_protocol(make_raw):
    extractor = RuleTodoExtractor()
    assert isinstance(extractor, TodoExtractor)
    assert isinstance(extractor.extract(make_raw("明天提交报告")), list)


# ---------------------------------------------------------------------------
# parse_due_text 辅助接口
# ---------------------------------------------------------------------------

def test_parse_due_text_basic():
    parsed = parse_due_text("明天下午3点前提交", now=FIXED_NOW)
    assert parsed is not None
    assert parsed.due_at is not None
    assert (parsed.due_at.month, parsed.due_at.day, parsed.due_at.hour) == (10, 29, 15)
    assert parsed.is_deadline is True
    assert parsed.due_text == "明天下午3点前"


def test_parse_due_text_deadline_only():
    parsed = parse_due_text("截止11月15日", now=FIXED_NOW)
    assert parsed is not None
    assert parsed.is_deadline is True
    assert (parsed.due_at.month, parsed.due_at.day) == (11, 15)


def test_parse_due_text_urgency_only():
    parsed = parse_due_text("请尽快处理", now=FIXED_NOW)
    assert parsed is not None
    assert parsed.due_at is None
    assert parsed.is_urgent is True
    assert parsed.due_text == "尽快"


def test_parse_due_text_returns_none_for_plain_chat():
    assert parse_due_text("收到，谢谢", now=FIXED_NOW) is None
    assert parse_due_text("哈哈哈", now=FIXED_NOW) is None


def test_parse_due_text_parses_bare_date():
    """parse_due_text 是纯时间解析接口：即使句子不含动作也会识别时间。"""
    parsed = parse_due_text("今天天气不错", now=FIXED_NOW)
    assert parsed is not None
    assert parsed.due_text == "今天"
    assert (parsed.due_at.month, parsed.due_at.day) == (10, 28)


def test_raw_message_factory_independent():
    """确保测试不依赖 fixture 也能构造 RawMessage。"""
    raw = RawMessage(conversation_key="k", sender="s", is_self=False, text="明天提交报告")
    assert RuleTodoExtractor(now_provider=lambda: FIXED_NOW).extract(raw)
