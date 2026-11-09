"""QqMessageSource 端到端离线测试（fake UIA 树，不依赖真实 QQ）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from social_secretary.collectors.qq.collector import ChatTarget, QqMessageSource
from social_secretary.collectors.qq.errors import (
    QqForegroundNotAcknowledged,
    QqHeaderMismatch,
    QqHeaderUnavailable,
    QqNotRunning,
    QqOpenChatFailed,
    QqTreeNotReady,
    QqWindowUnavailable,
)
from social_secretary.collectors.qq.profile import QqUiMode, load_profile
from social_secretary.collectors.qq.whitelist import normalize_title
from social_secretary.contracts import (
    Conversation,
    ConversationKind,
    MessageSource,
    TimestampSource,
)

from .fake_uia import (
    CHAT_HWND,
    FIXED_NOW,
    NOTEPAD_HWND,
    PANEL_HWND,
    FakeBackend,
    FakeNode,
    bubble_node,
    chat_window_root,
    make_window,
    panel_root,
    search_result_node,
    text_node,
)

ALLOWLIST = ("测试会话",)


def _test_profile(**behavior_overrides):  # noqa: ANN001
    profile = load_profile()
    overrides = {"min_tree_nodes": 1, "poll_interval_ms": 1}
    overrides.update(behavior_overrides)
    return replace(profile, behavior=replace(profile.behavior, **overrides))


def _conversation(target: str = "测试会话", kind: ConversationKind = ConversationKind.PRIVATE) -> Conversation:
    return Conversation(
        id=f"qq:{normalize_title(target)}",
        account_id="qq-local",
        kind=kind,
        title=target,
        external_key=target,
    )


def _source(backend: FakeBackend, *, profile=None, allowlist=ALLOWLIST, **kwargs) -> QqMessageSource:  # noqa: ANN001
    return QqMessageSource(
        backend=backend,
        profile=profile or _test_profile(),
        allowlist=allowlist,
        sleep_scale=0,
        **kwargs,
    )


def _efficiency_backend(*, header: str = "测试会话", bubbles=(), include_msg_area: bool = True,
                        foreground: int = NOTEPAD_HWND) -> FakeBackend:  # noqa: ANN001
    root = panel_root(
        mode="efficiency",
        current_header=header,
        msg_bubbles=bubbles,
        include_msg_area=include_msg_area,
    )
    return FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root=root)],
        foreground=foreground,
    )


# ---------------------------------------------------------------------------
# 环境 / 连接检测
# ---------------------------------------------------------------------------

def test_test_connection_qq_not_running() -> None:
    backend = FakeBackend(no_qq=True, windows=[])
    source = _source(backend)
    result = source.test_connection()
    assert result.ok is False
    assert "未检测到 QQ 进程" in result.detail
    assert result.discovered["process_count"] == 0
    assert backend.set_foreground_calls == []
    assert backend.show_window_calls == []


def test_test_connection_running_panel() -> None:
    backend = _efficiency_backend()
    source = _source(backend)
    result = source.test_connection()
    assert result.ok is True
    assert result.discovered["panel_found"] is True
    assert result.discovered["mode"] == "efficiency"
    # 非侵入式检测：不切前台
    assert backend.set_foreground_calls == []
    assert backend.show_window_calls == []
    assert backend.clicks == []


def test_test_connection_process_without_panel() -> None:
    backend = FakeBackend(windows=[], foreground=NOTEPAD_HWND)
    source = _source(backend)
    result = source.test_connection()
    assert result.ok is False
    assert "主面板" in result.detail
    assert backend.set_foreground_calls == []


def test_test_connection_sparse_background_tree() -> None:
    sparse = FakeNode(name="QQ", cls="Chrome_WidgetWin_1", ctype="Window", children=[])
    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root=sparse)],
        foreground=NOTEPAD_HWND,
    )
    result = _source(backend).test_connection()
    assert result.ok is True
    assert result.discovered["tree_dense"] is False


def test_fetch_raises_when_qq_not_running() -> None:
    backend = FakeBackend(no_qq=True, windows=[])
    source = _source(backend)
    with pytest.raises(QqNotRunning) as excinfo:
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert excinfo.value.code == "qq_not_running"
    assert backend.foreground == NOTEPAD_HWND


# ---------------------------------------------------------------------------
# 效率模式 happy path
# ---------------------------------------------------------------------------

def _messages() -> list[FakeNode]:
    return [
        bubble_node(["你好"], mode="efficiency", rect=(10, 10, 400, 40)),
        bubble_node(["好的"], mode="efficiency", is_self=True, rect=(10, 50, 400, 80)),
    ]


def test_fetch_messages_efficiency_happy_path() -> None:
    backend = _efficiency_backend(bubbles=_messages())
    warnings: list[str] = []
    source = _source(backend, warning_callback=warnings.append)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)

    assert [message.text for message in result] == ["你好", "好的"]
    assert [message.is_self for message in result] == [False, True]
    assert [message.sender for message in result] == ["测试会话", "我"]
    assert all(message.timestamp is None for message in result)
    assert all(message.timestamp_source is TimestampSource.COLLECTED for message in result)
    assert all(message.conversation_key == "测试会话" for message in result)
    assert all(message.external_id.startswith("qq:") for message in result)
    assert all(message.extras["parser"] == "qq_uia" for message in result)
    assert "效率" in result[0].source_locator


    stats = source.last_stats
    assert stats.mode == "efficiency"
    assert stats.chat_hwnd == PANEL_HWND
    assert stats.embedded_chat is True
    assert stats.bubbles_kept == 2
    assert stats.bubbles_seen >= 2
    assert stats.foreground_used is True
    assert stats.foreground_restored is True
    assert stats.search_input_method == "none"

    # 只读：没有点击、没有输入、没有剪贴板
    assert backend.clicks == []
    assert backend.set_value_calls == []
    assert backend.paste_calls == []
    assert backend.foreground == NOTEPAD_HWND
    assert backend.cursor == (5, 6)
    assert any("前台" in warning for warning in warnings)


def test_identical_visible_messages_keep_unique_external_ids() -> None:
    """回归：相同发送者连续发送完全相同文字时，不得因 external_id 相同被误去重。"""
    bubbles = [
        bubble_node(["收到，谢谢。"], mode="efficiency", is_self=True, rect=(10, 40 + i * 40, 400, 70 + i * 40))
        for i in range(3)
    ]
    backend = _efficiency_backend(bubbles=bubbles)
    source = _source(backend)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["收到，谢谢。"] * 3
    external_ids = [message.external_id for message in result]
    assert all(external_ids), external_ids
    assert len(set(external_ids)) == 3
    assert len({message.source_locator for message in result}) == 3
    assert source.last_stats.bubbles_kept == 3


def test_fetch_messages_restores_minimized_original_window() -> None:
    backend = _efficiency_backend(bubbles=_messages())
    backend._windows[NOTEPAD_HWND] = make_window(NOTEPAD_HWND, "原窗口", minimized=True)
    source = _source(backend)
    source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    from social_secretary.collectors.qq.backend import SW_RESTORE

    assert (NOTEPAD_HWND, SW_RESTORE) in backend.show_window_calls
    assert backend.foreground == NOTEPAD_HWND


# ---------------------------------------------------------------------------
# 经典模式
# ---------------------------------------------------------------------------

def _classic_messages() -> list[FakeNode]:
    return [
        bubble_node(["你好"], mode="classic", rect=(10, 10, 400, 40)),
        bubble_node(["好的"], mode="classic", is_self=True, rect=(10, 50, 400, 80)),
    ]


def _classic_backend(bubbles=None) -> FakeBackend:  # noqa: ANN001
    bubbles = bubbles if bubbles is not None else _classic_messages()
    panel = panel_root(mode="classic", current_header="其他会话", include_msg_area=False)
    chat = chat_window_root(current_header="测试会话", msg_bubbles=bubbles)
    return FakeBackend(
        windows=[
            make_window(PANEL_HWND, "QQ", root=panel),
            make_window(CHAT_HWND, "测试会话", root=chat),
        ],
        foreground=NOTEPAD_HWND,
    )


def test_fetch_messages_classic_separate_window() -> None:
    backend = _classic_backend(bubbles=_classic_messages())
    source = _source(backend)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    stats = source.last_stats
    assert stats.mode == "classic"
    assert stats.chat_hwnd == CHAT_HWND
    assert stats.embedded_chat is False
    assert backend.foreground == NOTEPAD_HWND
    assert backend.clicks == []
    assert backend.set_value_calls == []


# ---------------------------------------------------------------------------
# 通过搜索打开会话
# ---------------------------------------------------------------------------

def _search_backend(bubbles=None, *, result_present: bool = True) -> FakeBackend:  # noqa: ANN001
    bubbles = bubbles or _messages()

    def factory(backend: FakeBackend) -> FakeNode:
        current = backend.state.get("current_chat", "其他会话")
        results = [search_result_node("测试会话")] if result_present else []
        return panel_root(
            mode="efficiency",
            current_header=current,
            show_results=True,
            results=results,
            msg_bubbles=bubbles,
        )

    return FakeBackend(
        windows=[
            make_window(
                PANEL_HWND,
                "QQ",
                root_factory=factory,
            )
        ],
        foreground=NOTEPAD_HWND,
    )


def test_fetch_messages_opens_chat_from_conversation_list_without_search() -> None:
    """可见会话列表项直接打开优先于搜索，且只点击 recent-contact-item。"""

    def item_node() -> FakeNode:
        info = FakeNode(cls="item__info", ctype="Group", children=[FakeNode(name="测试会话", ctype="Text")])
        content = FakeNode(cls="item__content", ctype="Group", children=[info])
        return FakeNode(
            cls="recent-contact-item",
            ctype="Group",
            children=[content],
            click_action=lambda backend: backend.state.__setitem__("current_chat", "测试会话"),
        )

    def factory(backend: FakeBackend) -> FakeNode:
        current = backend.state.get("current_chat", "其他会话")
        return panel_root(
            mode="efficiency",
            current_header=current,
            msg_bubbles=_messages(),
            extra_children=[item_node()],
        )

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    source = _source(backend)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    assert len(backend.clicks) == 1
    assert backend.clicks[0].class_name == "recent-contact-item"
    assert backend.clicks[0].control_type.lower() == "group"
    assert backend.set_value_calls == []
    assert backend.paste_calls == []
    assert backend.clicks_on_message_editor == []
    assert backend.foreground == NOTEPAD_HWND


def test_fetch_messages_opens_chat_via_search_and_clicks_only_list_item() -> None:
    backend = _search_backend()
    source = _source(backend)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    stats = source.last_stats
    assert stats.search_input_method == "value_pattern"
    assert len(backend.clicks) == 1
    clicked = backend.clicks[0]
    assert clicked.control_type.lower() == "listitem"
    assert "search-result" in clicked.class_name
    assert backend.clicks_on_buttons == []
    assert backend.clicks_on_message_editor == []
    written = [node for node, _value in backend.set_value_calls]
    assert written and all("qq-msg-editor" not in node.class_name for node in written)
    assert backend.paste_calls == []
    assert backend.foreground == NOTEPAD_HWND


def test_fetch_messages_search_result_missing_raises_open_chat_failed() -> None:
    backend = _search_backend(result_present=False)
    source = _source(backend)
    with pytest.raises(QqOpenChatFailed):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.foreground == NOTEPAD_HWND
    assert backend.clicks == []


def test_fetch_messages_header_mismatch_after_open_aborts() -> None:
    def factory(backend: FakeBackend) -> FakeNode:
        current = backend.state.get("current_chat", "其他会话")
        return panel_root(
            mode="efficiency",
            current_header="别人的会话" if current != "其他会话" else current,
            show_results=True,
            results=[search_result_node("测试会话")],
        )

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    source = _source(backend)
    with pytest.raises(QqOpenChatFailed):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.foreground == NOTEPAD_HWND


# ---------------------------------------------------------------------------
# 头部校验
# ---------------------------------------------------------------------------

def test_validate_header_mismatch_raises() -> None:
    backend = _efficiency_backend(header="别人的会话")
    source = _source(backend)
    source._mode = QqUiMode.EFFICIENCY
    with pytest.raises(QqHeaderMismatch) as excinfo:
        source._validate_header(
            backend, ChatTarget(hwnd=PANEL_HWND, embedded=True, title="测试会话"), "测试会话"
        )
    assert excinfo.value.code == "header_mismatch"
    assert "header_name_length" in excinfo.value.detail


def test_validate_header_missing_raises() -> None:
    root = FakeNode(
        name="QQ",
        cls="Chrome_WidgetWin_1",
        ctype="Window",
        children=[FakeNode(name="", cls="search-edit", ctype="Edit")],
    )
    backend = FakeBackend(windows=[make_window(PANEL_HWND, "QQ", root=root)])
    source = _source(backend)
    source._mode = QqUiMode.EFFICIENCY
    with pytest.raises(QqHeaderUnavailable):
        source._validate_header(
            backend, ChatTarget(hwnd=PANEL_HWND, embedded=True, title="测试会话"), "测试会话"
        )


# ---------------------------------------------------------------------------
# 树未就绪 / 结构缺失：绝不能当作「没有新消息」
# ---------------------------------------------------------------------------

def test_sparse_tree_raises_tree_not_ready_not_empty_messages() -> None:
    backend = _efficiency_backend(bubbles=_messages())
    profile = _test_profile(min_tree_nodes=1000, tree_ready_attempts=2)
    source = _source(backend, profile=profile)
    with pytest.raises(QqTreeNotReady) as excinfo:
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert excinfo.value.code == "tree_not_ready"
    assert backend.foreground == NOTEPAD_HWND


def test_missing_msg_area_raises_tree_not_ready() -> None:
    backend = _efficiency_backend(bubbles=(), include_msg_area=False)
    source = _source(backend)
    with pytest.raises(QqTreeNotReady) as excinfo:
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert "消息区" in excinfo.value.message
    assert backend.foreground == NOTEPAD_HWND


def test_sparse_panel_without_search_box_raises_window_unavailable() -> None:
    sparse = FakeNode(name="QQ", cls="Chrome_WidgetWin_1", ctype="Window", children=[])
    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root=sparse)],
        foreground=NOTEPAD_HWND,
    )
    source = _source(backend)
    with pytest.raises(QqWindowUnavailable):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.foreground == NOTEPAD_HWND


def test_panel_tree_becomes_ready_after_activation_retries() -> None:
    """模拟置前 + 等待无障碍树重建：前几次读到稀疏树，之后变成完整树。"""

    sparse = FakeNode(name="QQ", cls="Chrome_WidgetWin_1", ctype="Window", children=[])
    calls = {"count": 0}

    def factory(backend: FakeBackend) -> FakeNode:
        calls["count"] += 1
        if calls["count"] >= 4:
            return panel_root()
        return sparse

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    profile = _test_profile(panel_ready_attempts=3, revive_tree_wait_ms=0, poll_interval_ms=1)
    source = _source(backend, profile=profile)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert result == []
    assert backend.set_foreground_calls  # 曾为构建 UIA 树短暂置前
    assert source.last_stats.foreground_restored is True
    assert backend.foreground == NOTEPAD_HWND
    assert calls["count"] >= 4


# ---------------------------------------------------------------------------
# 滚动历史 + 去重 + 条数上限
# ---------------------------------------------------------------------------

def _paged_backend(pages: dict[int, list[FakeNode]], *, header: str = "测试会话") -> FakeBackend:
    def factory(backend: FakeBackend) -> FakeNode:
        page = backend.state.get("page", 0)
        return panel_root(mode="efficiency", current_header=header, msg_bubbles=pages.get(page, []))

    def on_wheel(backend: FakeBackend, notches: int) -> None:
        page = backend.state.get("page", 0)
        backend.state["page"] = max(0, min(len(pages) - 1, page + (1 if notches > 0 else -1)))

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    backend.on_wheel = on_wheel
    return backend


def _page_bubbles(*texts: str) -> list[FakeNode]:
    return [
        bubble_node([text], mode="efficiency", rect=(10, 10 + index * 40, 400, 40 + index * 40))
        for index, text in enumerate(texts)
    ]


def test_scroll_history_dedupes_and_returns_chronological_limit() -> None:
    pages = {
        0: _page_bubbles("消息3", "消息4"),
        1: _page_bubbles("消息2", "消息3", "消息4"),
        2: _page_bubbles("消息1", "消息2", "消息3", "消息4"),
    }
    backend = _paged_backend(pages)
    source = _source(backend, scroll_notches=1)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=3)
    assert [message.text for message in result] == ["消息2", "消息3", "消息4"]
    stats = source.last_stats
    assert stats.scroll_passes >= 2
    assert stats.duplicates_dropped >= 2
    # 上滚后必须回到底部，别把用户聊天停在历史位置
    up = [notches for _x, _y, notches in backend.wheels if notches > 0]
    down = [notches for _x, _y, notches in backend.wheels if notches < 0]
    assert up and down and sum(up) == -sum(down)
    assert backend.state["page"] == 0
    # 只滚动消息区中心（不点鼠标、不写输入框）
    assert backend.clicks == []
    assert backend.set_value_calls == []


def test_scroll_stops_at_max_stale_passes() -> None:
    backend = _paged_backend({0: _page_bubbles("唯一消息")})
    source = _source(backend, max_scroll_passes=10)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=100)
    assert [message.text for message in result] == ["唯一消息"]
    assert source.last_stats.scroll_passes <= 3


def test_limit_is_capped_by_messages_hard_cap() -> None:
    backend = _efficiency_backend(bubbles=_messages())
    source = _source(backend, max_messages=2)
    with pytest.raises(QqForegroundNotAcknowledged):
        source.fetch_messages(_conversation(), limit=50)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=50)
    assert len(result) <= 2


def test_fetch_messages_limit_zero_returns_empty_without_touching_qq() -> None:
    backend = _efficiency_backend(bubbles=_messages())
    source = _source(backend)
    assert source.fetch_messages(_conversation(), foreground_ack=True, limit=0) == []
    assert backend.calls == []


# ---------------------------------------------------------------------------
# since 过滤
# ---------------------------------------------------------------------------

def test_since_filters_by_ui_timestamps() -> None:
    bubbles = [
        text_node("20:00", cls="time-label", rect=(10, 0, 100, 10)),
        bubble_node(["旧消息"], time_text="20:00", rect=(10, 10, 400, 40)),
        bubble_node(["新消息"], time_text="20:05", rect=(10, 50, 400, 80)),
    ]
    backend = _efficiency_backend(bubbles=bubbles)
    source = _source(backend)
    since = datetime(2026, 10, 31, 20, 3, tzinfo=FIXED_NOW.tzinfo)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10, since=since)
    assert [message.text for message in result] == ["新消息"]
    assert result[0].timestamp == datetime(2026, 10, 31, 20, 5, tzinfo=FIXED_NOW.tzinfo)
    assert result[0].timestamp_source is TimestampSource.UI


def test_since_keeps_messages_without_timestamp() -> None:
    backend = _efficiency_backend(bubbles=_messages())
    source = _source(backend)
    since = datetime(2026, 10, 31, 23, 59, tzinfo=FIXED_NOW.tzinfo)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10, since=since)
    assert len(result) == 2  # 无时间戳不能武断丢弃（由核心层按 COLLECTED 处理）


# ---------------------------------------------------------------------------
# 降级路径
# ---------------------------------------------------------------------------

def test_header_switch_during_scroll_returns_partial_with_warning() -> None:
    def factory(backend: FakeBackend) -> FakeNode:
        header = "测试会话" if backend.state.get("page", 0) == 0 else "其他会话"
        bubbles = _messages() if backend.state.get("page", 0) == 0 else []
        return panel_root(mode="efficiency", current_header=header, msg_bubbles=bubbles)

    def on_wheel(backend: FakeBackend, notches: int) -> None:
        if notches > 0:
            backend.state["page"] = 1

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    backend.on_wheel = on_wheel
    warnings: list[str] = []
    source = _source(backend, warning_callback=warnings.append)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    assert any("切换" in warning for warning in warnings)
    assert backend.foreground == NOTEPAD_HWND


def test_focus_stolen_during_scroll_stops_with_partial_result() -> None:
    def on_wheel(backend: FakeBackend, notches: int) -> None:
        if notches > 0:
            backend.foreground = NOTEPAD_HWND
            backend.deny_foreground = 100

    backend = _efficiency_backend(bubbles=_messages())
    backend.on_wheel = on_wheel
    warnings: list[str] = []
    source = _source(backend, warning_callback=warnings.append)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    assert any("抢占" in warning for warning in warnings)
    assert any("无法恢复" in warning for warning in warnings)


def test_window_disappears_during_scroll_returns_partial() -> None:
    def factory(backend: FakeBackend) -> FakeNode | None:
        if backend.state.get("gone"):
            return None
        return panel_root(mode="efficiency", current_header="测试会话", msg_bubbles=_messages())

    def on_wheel(backend: FakeBackend, notches: int) -> None:
        if notches > 0:
            backend.state["gone"] = True

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    backend.on_wheel = on_wheel
    warnings: list[str] = []
    source = _source(backend, warning_callback=warnings.append)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    assert any("消失" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# 模式覆盖 / 生命周期 / 协议兼容
# ---------------------------------------------------------------------------

def test_mode_override_forces_classic() -> None:
    backend = _classic_backend(bubbles=_classic_messages())
    source = _source(backend, mode_override=QqUiMode.CLASSIC)
    result = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert [message.text for message in result] == ["你好", "好的"]
    assert source.last_stats.mode == "classic"


def test_collector_is_message_source_protocol() -> None:
    source = _source(_efficiency_backend())
    assert isinstance(source, MessageSource)
    assert source.kind.value == "qq"


def test_close_is_idempotent_and_fetch_after_close_raises() -> None:
    from social_secretary.collectors.qq.errors import QqClosed

    backend = _efficiency_backend()
    source = _source(backend)
    source.close()
    source.close()
    # 注入的 backend 由调用方负责关闭；采集器只负责自身生命周期。
    with pytest.raises(QqClosed):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)


def test_group_chat_fetch_uses_group_sender_semantics() -> None:
    bubbles = [bubble_node(["收到"], sender="小明：", mode="efficiency")]
    backend = _efficiency_backend(bubbles=bubbles)
    source = _source(backend)
    result = source.fetch_messages(
        _conversation("测试会话", ConversationKind.GROUP), foreground_ack=True, limit=10
    )
    assert result[0].sender == "小明"
    assert result[0].text == "收到"
    assert result[0].extras["sender_resolved"] is True
