"""剪贴板回退（保存/恢复）与搜索框输入安全校验的离线测试。"""

from __future__ import annotations

import pytest
from dataclasses import replace

from social_secretary.collectors.qq.backend import paste_with_clipboard_restore
from social_secretary.collectors.qq.errors import QqSafetyViolation, QqUnavailable

from .fake_uia import (
    FIXED_NOW,
    NOTEPAD_HWND,
    PANEL_HWND,
    FakeBackend,
    FakeNode,
    make_window,
    panel_root,
    search_result_node,
)
from social_secretary.collectors.qq.collector import QqMessageSource
from social_secretary.collectors.qq.profile import load_profile
from social_secretary.contracts import Conversation, ConversationKind

ALLOWLIST = ("测试会话",)


def _conversation(target: str = "测试会话", kind: ConversationKind = ConversationKind.PRIVATE) -> Conversation:
    return Conversation(
        id="qq:test",
        account_id="qq-local",
        kind=kind,
        title=target,
        external_key=target,
    )


def _backend(*, search_value_supported: bool = True) -> FakeBackend:
    def factory(backend: FakeBackend) -> FakeNode:
        current = backend.state.get("current_chat", "其他会话")
        return panel_root(
            mode="efficiency",
            current_header=current,
            search_value_supported=search_value_supported,
            show_results=True,
            results=[search_result_node("测试会话")],
        )

    return FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )


def _source(backend: FakeBackend, **kwargs: object) -> QqMessageSource:  # noqa: ANN001
    profile = load_profile()
    profile = replace(profile, behavior=replace(profile.behavior, min_tree_nodes=1, poll_interval_ms=1))
    return QqMessageSource(
        backend=backend,
        profile=profile,
        allowlist=ALLOWLIST,
        sleep_scale=0,
        **kwargs,
    )


def test_paste_helper_saves_and_restores_clipboard() -> None:
    calls: list[tuple[str, str]] = []
    clipboard = {"value": "用户原有内容"}

    def get_text() -> str:
        calls.append(("get", clipboard["value"]))
        return clipboard["value"]

    def set_text(text: str) -> bool:
        calls.append(("set", text))
        clipboard["value"] = text
        return True

    paste_with_clipboard_restore(
        "测试会话",
        get_text=get_text,
        set_text=set_text,
        send_select_all=lambda: calls.append(("select_all", "")),
        send_paste=lambda: calls.append(("paste", "")),
    )
    assert [action for action, _value in calls] == ["get", "set", "select_all", "paste", "set"]
    assert clipboard["value"] == "用户原有内容"


def test_paste_helper_restores_clipboard_on_exception() -> None:
    calls: list[tuple[str, str]] = []

    def get_text() -> str:
        calls.append(("get", ""))
        return "原内容"

    def set_text(text: str) -> bool:
        calls.append(("set", text))
        return True

    def boom() -> None:
        calls.append(("paste", ""))
        raise RuntimeError("粘贴失败")

    with pytest.raises(RuntimeError):
        paste_with_clipboard_restore(
            "测试会话",
            get_text=get_text,
            set_text=set_text,
            send_select_all=lambda: None,
            send_paste=boom,
        )
    assert calls[-1] == ("set", "原内容")


def test_paste_helper_raises_and_restores_when_clipboard_write_fails() -> None:
    calls: list[tuple[str, str]] = []

    def get_text() -> str:
        calls.append(("get", ""))
        return "原内容"

    def set_text(text: str) -> bool:
        calls.append(("set", text))
        return False

    with pytest.raises(QqUnavailable):
        paste_with_clipboard_restore(
            "测试会话",
            get_text=get_text,
            set_text=set_text,
            send_select_all=lambda: None,
            send_paste=lambda: None,
        )
    assert calls == [("get", ""), ("set", "测试会话"), ("set", "原内容")]


def test_value_pattern_preferred_over_clipboard() -> None:
    backend = _backend(search_value_supported=True)
    backend.clipboard = "用户剪贴板内容"
    source = _source(backend)
    messages = source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert messages == []
    search = backend.set_value_calls[0][0]
    assert "search-edit" in search.class_name
    assert backend.set_value_calls[0][1] == "测试会话"
    assert backend.paste_calls == []
    assert source.last_stats.search_input_method == "value_pattern"
    assert source.last_stats.clipboard_fallback_used is False
    assert backend.clipboard == "用户剪贴板内容"


def test_clipboard_fallback_records_and_restores_clipboard() -> None:
    backend = _backend(search_value_supported=False)
    backend.clipboard = "用户剪贴板内容"
    source = _source(backend)
    source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.paste_calls == ["测试会话"]
    assert backend.clipboard == "用户剪贴板内容"  # fake backend 模拟了保存/恢复
    assert source.last_stats.search_input_method == "clipboard"
    assert source.last_stats.clipboard_fallback_used is True
    assert any("剪贴板" in warning for warning in source.last_stats.warnings)
    assert backend.focus_calls
    assert "search-edit" in backend.focus_calls[0].class_name


def test_clipboard_fallback_rejected_when_focus_is_not_search_box() -> None:
    backend = _backend(search_value_supported=False)
    backend.focus_override = FakeNode(name="", cls="qq-msg-editor", ctype="Edit")
    source = _source(backend)
    with pytest.raises(QqSafetyViolation):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.paste_calls == []
    assert backend.clicks == []


def test_clipboard_fallback_rejected_when_focus_fails() -> None:
    backend = _backend(search_value_supported=False)
    backend.focus_fails = True
    source = _source(backend)
    with pytest.raises(QqSafetyViolation):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.paste_calls == []


def test_message_editor_edit_is_never_used_as_search_box() -> None:
    """只有消息编辑框（Edit 类型）而没有合法搜索框时，必须拒绝输入。"""

    from social_secretary.collectors.qq.errors import QqWindowUnavailable

    editor = FakeNode(name="", cls="qq-msg-editor", ctype="Edit", rect=(10, 610, 500, 680))
    header = FakeNode(name="其他会话", cls="chat-header__contact-name", ctype="Text")
    root = FakeNode(name="QQ", cls="Chrome_WidgetWin_1", ctype="Window", children=[header, editor])
    backend = FakeBackend(windows=[make_window(PANEL_HWND, "QQ", root=root)])
    source = _source(backend)
    with pytest.raises(QqWindowUnavailable):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    assert backend.set_value_calls == []
    assert backend.paste_calls == []
    assert backend.clicks == []


def test_search_failure_does_not_touch_message_editor() -> None:
    from social_secretary.collectors.qq.errors import QqOpenChatFailed

    def factory(backend: FakeBackend) -> FakeNode:
        return panel_root(
            mode="efficiency",
            current_header="其他会话",
            search_value_supported=True,
            show_results=False,
        )

    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root_factory=factory)],
        foreground=NOTEPAD_HWND,
    )
    source = _source(backend)
    with pytest.raises(QqOpenChatFailed):
        source.fetch_messages(_conversation(), foreground_ack=True, limit=10)
    for node, _value in backend.set_value_calls:
        assert "qq-msg-editor" not in node.class_name
        assert "search-edit" in node.class_name
    assert backend.paste_calls == []
    assert backend.clicks_on_message_editor == []
    assert backend.clicks_on_buttons == []


def test_fixed_now_matches_expected_timezone() -> None:
    assert FIXED_NOW.utcoffset().total_seconds() == 8 * 3600
