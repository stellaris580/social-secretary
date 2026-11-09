"""回归：受限环境下 `list_top_windows` 不能只依赖 Toolhelp 进程列表。

背景（2026-11-01 真机发现）：本机 DSH 沙箱里 Toolhelp 快照只返回部分进程，
看不到 QQ 主窗口所属进程（pid 在窗口枚举里可见、QueryFullProcessImageName 也可用），
导致 `NativeBackend.list_top_windows()` 返回空、探针报“QQ 进程存在但无窗口”。
修复：窗口 pid 不在进程列表时，用窗口 pid 查询 exe 名做兜底匹配。
"""

from __future__ import annotations

import pytest

from social_secretary.collectors.qq import backend
from social_secretary.collectors.qq.backend import ProcessInfo, WindowInfo


def _win(hwnd: int, pid: int, *, width: int = 800, height: int = 600, visible: bool = True) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        pid=pid,
        title="QQ",
        class_name="Chrome_WidgetWin_1",
        rect=(0, 0, width, height),
        visible=visible,
        minimized=False,
    )


def test_window_enumeration_falls_back_to_exe_name_when_process_list_misses_pid(monkeypatch):
    """Toolhelp 看不到主窗口 pid 时，用窗口 pid 的 exe 名兜底，仍能找到窗口。"""
    known = ProcessInfo(pid=111, name="QQ.exe", exe="QQ.exe")
    target = _win(9001, 222)
    unrelated = _win(9002, 333)
    monkeypatch.setattr(backend.NativeBackend, "list_processes", lambda self, names: [known])
    monkeypatch.setattr(backend, "_enum_top_windows", lambda: [unrelated, target])
    monkeypatch.setattr(backend, "_process_exe_name", lambda pid: "QQ.exe" if pid == 222 else "other.exe")

    windows = backend.NativeBackend().list_top_windows(("QQ",))

    assert [window.hwnd for window in windows] == [9001]


def test_window_enumeration_keeps_known_pids_without_exe_lookup(monkeypatch):
    known = ProcessInfo(pid=111, name="QQ.exe", exe="QQ.exe")
    target = _win(9001, 111)
    monkeypatch.setattr(backend.NativeBackend, "list_processes", lambda self, names: [known])
    monkeypatch.setattr(backend, "_enum_top_windows", lambda: [target])

    def _boom(pid: int) -> str:  # pragma: no cover - 不应被调用
        raise AssertionError("已知 pid 不应再查询 exe 名")

    monkeypatch.setattr(backend, "_process_exe_name", _boom)
    assert [w.hwnd for w in backend.NativeBackend().list_top_windows(("QQ",))] == [9001]


def test_window_enumeration_drops_windows_without_matching_exe(monkeypatch):
    other = _win(9002, 333)
    monkeypatch.setattr(backend.NativeBackend, "list_processes", lambda self, names: [])
    monkeypatch.setattr(backend, "_enum_top_windows", lambda: [other])
    monkeypatch.setattr(backend, "_process_exe_name", lambda pid: "msedge.exe")

    assert backend.NativeBackend().list_top_windows(("QQ",)) == []


def test_process_exe_name_returns_empty_on_unknown_pid():
    # 不存在的 pid（0 也打不开）应安全返回空串而不是抛异常。
    assert backend._process_exe_name(0) == ""
