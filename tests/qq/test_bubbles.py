"""气泡解析（发送者/正文/时间/去重）的离线测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from social_secretary.collectors.qq.bubbles import (
    Bubble,
    MessageAccumulator,
    compute_content_hash,
    read_visible_bubbles,
)
from social_secretary.collectors.qq.profile import QqUiMode, load_profile
from social_secretary.contracts import ConversationKind, TimestampSource

from .fake_uia import FIXED_NOW, bubble_node, msg_area_node, panel_root, text_node

PROFILE = load_profile()


def _read(root, *, mode: str = "efficiency", kind: ConversationKind = ConversationKind.PRIVATE,
          title: str = "测试会话"):  # noqa: ANN001
    selectors = PROFILE.efficiency if mode == "efficiency" else PROFILE.classic
    return read_visible_bubbles(
        root,
        selectors,
        conversation_title=title,
        conversation_kind=kind,
        self_label="我",
        unknown_sender_label="未知成员",
        now=FIXED_NOW,
    )


def _area_root(children, *, rect=(10, 110, 500, 600)):  # noqa: ANN001
    return panel_root(msg_bubbles=children, include_msg_area=True)


def test_private_chat_bubble_body_and_sender() -> None:
    root = _area_root([
        bubble_node(["你好", "第二行"], mode="efficiency", rect=(10, 10, 400, 40)),
        bubble_node(["好的"], mode="efficiency", is_self=True, rect=(10, 50, 400, 80)),
    ])
    snapshot = _read(root, mode="efficiency")
    assert snapshot.msg_area_found
    assert len(snapshot.bubbles) == 2
    first, second = snapshot.bubbles
    assert first.text == "你好第二行"
    assert first.is_self is False
    assert first.sender == "测试会话"  # 私聊对方 = 会话标题
    assert first.sender_resolved is True
    assert first.timestamp is None
    assert first.timestamp_source is TimestampSource.COLLECTED
    assert second.is_self is True
    assert second.sender == "我"
    assert second.text == "好的"


def test_classic_mode_pattern() -> None:
    root = _area_root([bubble_node(["经典模式消息"], mode="classic")])
    snapshot = _read(root, mode="classic")
    assert [bubble.text for bubble in snapshot.bubbles] == ["经典模式消息"]


def test_bubble_time_node_is_parsed_and_excluded_from_body() -> None:
    root = _area_root([bubble_node(["今晚见"], mode="efficiency", time_text="21:05")])
    snapshot = _read(root)
    bubble = snapshot.bubbles[0]
    assert bubble.text == "今晚见"
    assert bubble.raw_time_text == "21:05"
    assert bubble.timestamp == datetime(2026, 10, 31, 21, 5, tzinfo=FIXED_NOW.tzinfo)
    assert bubble.timestamp_source is TimestampSource.UI


def test_time_divider_applies_to_following_bubbles() -> None:
    root = _area_root([
        text_node("昨天 20:00", cls="time-label", rect=(10, 0, 100, 10)),
        bubble_node(["第一条"], rect=(10, 10, 400, 40)),
        bubble_node(["第二条"], rect=(10, 50, 400, 80)),
    ])
    snapshot = _read(root)
    expected = datetime(2026, 10, 30, 20, 0, tzinfo=FIXED_NOW.tzinfo)
    assert [bubble.timestamp for bubble in snapshot.bubbles] == [expected, expected]
    assert snapshot.bubbles[0].raw_time_text == "昨天 20:00"


def test_relative_time_hint() -> None:
    root = _area_root([bubble_node(["刚发的"], time_text="5分钟前")])
    bubble = _read(root).bubbles[0]
    assert bubble.timestamp_source is TimestampSource.RELATIVE
    assert bubble.timestamp == FIXED_NOW - timedelta(minutes=5)


def test_group_chat_sender_node_is_extracted_and_excluded_from_body() -> None:
    root = _area_root([bubble_node(["收到"], sender="小明：", mode="efficiency")])
    snapshot = _read(root, kind=ConversationKind.GROUP)
    bubble = snapshot.bubbles[0]
    assert bubble.sender == "小明"
    assert bubble.sender_resolved is True
    assert bubble.text == "收到"  # 昵称不能混进正文


def test_group_chat_without_sender_node_uses_unknown_label() -> None:
    root = _area_root([bubble_node(["无昵称"], mode="efficiency")])
    bubble = _read(root, kind=ConversationKind.GROUP).bubbles[0]
    assert bubble.sender == "未知成员"
    assert bubble.sender_resolved is False


def test_self_marker_in_descendant_class() -> None:
    node = bubble_node(["我发的"], mode="efficiency")
    node.children[0]._class_name += " container--self"  # type: ignore[attr-defined]
    root = _area_root([node])
    bubble = _read(root).bubbles[0]
    assert bubble.is_self is True
    assert bubble.sender == "我"


def test_no_msg_area_returns_not_found_without_bubbles() -> None:
    root = panel_root(include_msg_area=False)
    snapshot = _read(root)
    assert snapshot.msg_area_found is False
    assert snapshot.bubbles == ()


def test_empty_text_media_bubble_is_dropped_and_counted() -> None:
    root = _area_root([bubble_node([], mode="efficiency"), bubble_node(["文字"], mode="efficiency")])
    snapshot = _read(root)
    assert [bubble.text for bubble in snapshot.bubbles] == ["文字"]
    assert snapshot.empty_text_dropped == 1
    assert snapshot.bubble_nodes_seen == 2


def test_nested_content_wrapper_is_not_double_counted() -> None:
    inner = text_node("嵌套正文")
    wrapper = type(inner)(cls="message-container__content", ctype="Pane", children=[inner])
    outer = type(inner)(
        cls="message-container",
        ctype="Pane",
        children=[wrapper],
        rect=(10, 10, 400, 40),
    )
    root = _area_root([outer])
    snapshot = _read(root, mode="classic")
    assert len(snapshot.bubbles) == 1
    assert snapshot.bubbles[0].text == "嵌套正文"


def test_virtualized_duplicate_same_rect_collapsed() -> None:
    root = _area_root([
        bubble_node(["重复"], rect=(10, 10, 400, 40)),
        bubble_node(["重复"], rect=(10, 10, 400, 40)),
    ])
    snapshot = _read(root)
    assert [bubble.text for bubble in snapshot.bubbles] == ["重复"]
    assert snapshot.duplicates_dropped == 1


def test_same_text_at_different_rect_is_kept() -> None:
    root = _area_root([
        bubble_node(["哈哈"], rect=(10, 10, 400, 40)),
        bubble_node(["哈哈"], rect=(10, 50, 400, 80)),
    ])
    snapshot = _read(root)
    assert len(snapshot.bubbles) == 2


def test_lone_time_looking_text_is_treated_as_body() -> None:
    root = _area_root([bubble_node(["12:30"])])
    bubble = _read(root).bubbles[0]
    assert bubble.text == "12:30"
    assert bubble.timestamp is None


# ---------------------------------------------------------------------------
# 历史读取累加器
# ---------------------------------------------------------------------------

def _bubble(
    text: str,
    *,
    sender: str = "对方",
    is_self: bool = False,
    timestamp: datetime | None = None,
    rect=(10, 10, 400, 40),  # noqa: ANN001
) -> Bubble:
    return Bubble(
        index=0,
        text=text,
        is_self=is_self,
        sender=sender,
        sender_resolved=True,
        timestamp=timestamp,
        timestamp_source=TimestampSource.UI if timestamp else TimestampSource.COLLECTED,
        raw_time_text=None,
        class_name="message-container",
        rect=rect,
    )


def test_accumulator_overlapping_scroll_dedupes_and_prepends() -> None:
    accumulator = MessageAccumulator(conversation_key="测试会话")
    first = accumulator.add_visible(
        (_bubble("C", rect=(10, 10, 400, 40)), _bubble("D", rect=(10, 50, 400, 80))),
        scrolled_up=False,
    )
    assert [bubble.text for bubble in first] == ["C", "D"]
    second = accumulator.add_visible(
        (
            _bubble("A", rect=(10, 10, 400, 40)),
            _bubble("B", rect=(10, 50, 400, 80)),
            _bubble("C", rect=(10, 90, 400, 120)),
            _bubble("D", rect=(10, 130, 400, 160)),
        ),
        scrolled_up=True,
    )
    assert [bubble.text for bubble in second] == ["A", "B"]
    assert [bubble.text for bubble in accumulator.messages] == ["A", "B", "C", "D"]
    assert accumulator.duplicates_dropped == 2
    assert accumulator.passes == 2
    assert [bubble.text for bubble in accumulator.latest(2)] == ["C", "D"]


def test_accumulator_preserves_legit_repeated_content() -> None:
    accumulator = MessageAccumulator(conversation_key="测试会话")
    first = accumulator.add_visible(
        (_bubble("OK", rect=(10, 10, 400, 40)), _bubble("OK", rect=(10, 50, 400, 80))),
        scrolled_up=False,
    )
    assert [bubble.text for bubble in first] == ["OK", "OK"]
    # 上滚后视口重复暴露这两条，不应再增加。
    second = accumulator.add_visible(
        (
            _bubble("OK", rect=(10, 10, 400, 40)),
            _bubble("OK", rect=(10, 50, 400, 80)),
            _bubble("更早", rect=(10, 90, 400, 120)),
        ),
        scrolled_up=True,
    )
    assert [bubble.text for bubble in second] == ["更早"]
    assert len(accumulator.messages) == 3


def test_accumulator_keeps_same_text_with_different_timestamp_bucket() -> None:
    accumulator = MessageAccumulator(conversation_key="测试会话")
    t1 = datetime(2026, 10, 31, 21, 5, tzinfo=FIXED_NOW.tzinfo)
    t2 = datetime(2026, 10, 31, 21, 6, tzinfo=FIXED_NOW.tzinfo)
    new_items = accumulator.add_visible(
        (_bubble("OK", timestamp=t1), _bubble("OK", timestamp=t2)),
        scrolled_up=False,
    )
    assert len(new_items) == 2


def test_content_hash_matches_contract_fields() -> None:
    timestamp = datetime(2026, 10, 31, 21, 5, 1, tzinfo=FIXED_NOW.tzinfo)
    same_minute = datetime(2026, 10, 31, 21, 5, 59, tzinfo=FIXED_NOW.tzinfo)
    args = dict(
        conversation_key="测试会话",
        sender="对方",
        is_self=False,
        text="你好",
        timestamp=timestamp,
    )
    base = compute_content_hash(**args)
    assert base == compute_content_hash(**args)
    assert base == compute_content_hash(**(args | {"timestamp": same_minute}))
    assert base != compute_content_hash(**(args | {"text": "你好！"}))
    assert base != compute_content_hash(**(args | {"sender": "另一个人"}))
    assert base != compute_content_hash(**(args | {"is_self": True}))
    assert base != compute_content_hash(**(args | {"conversation_key": "别的会话"}))
    assert base != compute_content_hash(**(args | {"timestamp": timestamp + timedelta(minutes=1)}))
    assert base != compute_content_hash(**(args | {"timestamp": None}))


__all__ = ["QqUiMode", "pytest"]
