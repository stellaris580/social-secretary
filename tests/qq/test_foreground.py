"""前台窗口协调与恢复的离线测试。"""

from __future__ import annotations

import pytest

from social_secretary.collectors.qq.backend import SW_RESTORE, SW_SHOW
from social_secretary.collectors.qq.foreground import ForegroundGuard, activate_window

from .fake_uia import CHAT_HWND, NOTEPAD_HWND, PANEL_HWND, FakeBackend, make_window, panel_root


def _backend(*, foreground: int = NOTEPAD_HWND, minimized: bool = False) -> FakeBackend:
    windows = [
        make_window(PANEL_HWND, "QQ", root=panel_root()),
        make_window(CHAT_HWND, "测试会话"),
    ]
    backend = FakeBackend(windows=windows, foreground=foreground)
    if minimized:
        # 模拟用户原来的前台窗口是最小化的（少见但需要能恢复）
        backend._windows[NOTEPAD_HWND] = make_window(NOTEPAD_HWND, "原窗口", minimized=True)
        backend._windows[NOTEPAD_HWND].visible = True
    return backend


def test_guard_restores_foreground_and_cursor() -> None:
    backend = _backend()
    backend.cursor = (111, 222)
    warnings: list[str] = []
    with ForegroundGuard(backend, warning_callback=warnings.append) as guard:
        assert guard.original_hwnd == NOTEPAD_HWND
        assert activate_window(backend, PANEL_HWND, guard=guard) is True
        assert backend.foreground == PANEL_HWND
    assert guard.foreground_used is True
    assert guard.restored is True
    assert backend.foreground == NOTEPAD_HWND
    assert backend.cursor == (111, 222)
    assert backend.cursor_sets[-1] == (111, 222)
    assert warnings and "前台" in warnings[0]


def test_guard_without_activation_does_not_warn() -> None:
    backend = _backend()
    warnings: list[str] = []
    with ForegroundGuard(backend, warning_callback=warnings.append) as guard:
        pass
    assert guard.foreground_used is False
    assert guard.restored is True
    assert warnings == []


def test_guard_restores_minimized_original_window() -> None:
    backend = _backend(foreground=NOTEPAD_HWND, minimized=True)
    with ForegroundGuard(backend) as guard:
        backend.foreground = PANEL_HWND
    assert guard.restored is True
    assert (NOTEPAD_HWND, SW_RESTORE) in backend.show_window_calls
    assert backend.foreground == NOTEPAD_HWND


def test_guard_restores_even_when_body_raises() -> None:
    backend = _backend()
    with pytest.raises(RuntimeError):
        with ForegroundGuard(backend) as guard:
            backend.foreground = PANEL_HWND
            raise RuntimeError("boom")
    assert guard.restored is True
    assert backend.foreground == NOTEPAD_HWND


def test_guard_reports_failure_when_restore_is_denied() -> None:
    backend = _backend()
    signals: list[str] = []
    with ForegroundGuard(backend, warning_callback=signals.append) as guard:
        backend.foreground = PANEL_HWND
        backend.deny_foreground = 100  # 模拟系统拒绝恢复前台
    assert guard.restored is False
    assert any("未能自动恢复" in message for message in signals)


def test_activate_window_retries_until_success() -> None:
    backend = _backend()
    backend.deny_foreground = 2
    assert activate_window(backend, PANEL_HWND, attempts=4, poll_interval=0.0) is True
    assert len(backend.set_foreground_calls) >= 3
    assert backend.foreground == PANEL_HWND


def test_activate_window_returns_false_when_always_denied() -> None:
    backend = _backend()
    backend.deny_foreground = 100
    assert activate_window(backend, PANEL_HWND, attempts=3, poll_interval=0.0) is False
    assert backend.foreground == NOTEPAD_HWND


def test_activate_window_restores_minimized_target() -> None:
    backend = _backend()
    backend._windows[PANEL_HWND].minimized = True
    assert activate_window(backend, PANEL_HWND, attempts=2, poll_interval=0.0) is True
    assert backend.show_window_calls == [(PANEL_HWND, SW_RESTORE)]
    assert backend._windows[PANEL_HWND].minimized is False


def test_activation_warning_only_once() -> None:
    backend = _backend()
    warnings: list[str] = []
    with ForegroundGuard(backend, warning_callback=warnings.append) as guard:
        guard.before_activate(PANEL_HWND)
        guard.before_activate(PANEL_HWND)
        guard.before_activate(CHAT_HWND)
    assert len(warnings) == 1
    assert "前台" in warnings[0]


def test_guard_does_not_warn_when_target_already_foreground() -> None:
    backend = _backend(foreground=PANEL_HWND)
    warnings: list[str] = []
    with ForegroundGuard(backend, warning_callback=warnings.append) as guard:
        guard.before_activate(PANEL_HWND)
    assert warnings == []
    assert guard.foreground_used is False


def test_guard_warning_callback_exception_is_swallowed() -> None:
    backend = _backend()
    calls: list[str] = []

    def bad_callback(message: str) -> None:
        calls.append(message)
        raise RuntimeError("UI 回调异常")

    with ForegroundGuard(backend, warning_callback=bad_callback) as guard:
        activate_window(backend, PANEL_HWND, guard=guard)
    assert calls
    assert guard.restored is True


def test_guard_original_foreground_zero() -> None:
    backend = _backend(foreground=0)
    with ForegroundGuard(backend) as guard:
        backend.foreground = PANEL_HWND
    assert guard.restored is True


def test_activate_hidden_window_shows_before_foreground() -> None:
    """QQ 读完一个会话后可能被隐藏（非最小化）；必须先 SW_SHOW 才能置前。"""

    backend = _backend(foreground=NOTEPAD_HWND)
    backend._windows[PANEL_HWND].visible = False
    assert activate_window(backend, PANEL_HWND) is True
    assert (PANEL_HWND, SW_SHOW) in backend.show_window_calls
    assert backend.foreground == PANEL_HWND
