"""离线测试用的 fake UIA 节点树与 fake 后端。

目标：完全不依赖真实 QQ / uiautomation / Windows COM，在普通 pytest 进程里
驱动 ``QqMessageSource`` 的全部只读逻辑，并记录所有「动作」以便断言：
- 点击了哪些控件（必须只有搜索结果 ListItem，绝不点按钮/消息编辑框）；
- 写入/粘贴了哪些文本（只允许主面板搜索框）；
- 滚动了哪些位置（只允许消息区中心）；
- 前台窗口如何切换、是否恢复；剪贴板是否被恢复。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from social_secretary.collectors.qq.backend import (
    SW_RESTORE,
    SW_SHOW,
    ProcessInfo,
    WindowInfo,
)
from social_secretary.collectors.qq.nodes import Rect

FIXED_NOW = datetime(2026, 10, 31, 21, 30, 0, tzinfo=timezone(timedelta(hours=8)))
# 2026-10-31 是星期六（供星期几时间解析测试使用）。

NOTEPAD_HWND = 999  # 模拟「用户原来的前台窗口」（非 QQ 窗口）
PANEL_HWND = 100
CHAT_HWND = 200


@dataclass
class FakeWindowDef:
    hwnd: int
    title: str = ""
    pid: int = 4242
    class_name: str = "Chrome_WidgetWin_1"
    rect: Rect = (0, 0, 900, 700)
    visible: bool = True
    minimized: bool = False
    root: "FakeNode | None" = None
    root_factory: Callable[["FakeBackend"], "FakeNode | None"] | None = None
    active_when: Callable[["FakeBackend"], bool] | None = None

    def is_active(self, backend: "FakeBackend") -> bool:
        return self.active_when(backend) if self.active_when is not None else True


class FakeNode:
    """实现 ``nodes.UiaNode`` 协议的最小 fake 控件。"""

    def __init__(
        self,
        name: str = "",
        cls: str = "",
        ctype: str = "Pane",
        children: Sequence["FakeNode"] = (),
        rect: Rect | None = None,
        *,
        value: str | None = None,
        value_supported: bool = False,
        click_action: Callable[["FakeBackend"], None] | None = None,
        focusable: bool = True,
        offscreen: bool = False,
        enabled: bool = True,
    ) -> None:
        self._name = name
        self._class_name = cls
        self._control_type = ctype
        self._children: list[FakeNode] = list(children)
        self._rect = rect
        self._value = value
        self.value_supported = value_supported
        self.click_action = click_action
        self.focusable = focusable
        self._offscreen = offscreen
        self._enabled = enabled
        self.backend: FakeBackend | None = None

    # -- UiaNode 协议 --------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def class_name(self) -> str:
        return self._class_name

    @property
    def control_type(self) -> str:
        return self._control_type

    @property
    def children(self) -> Sequence["FakeNode"]:
        return tuple(self._children)

    @property
    def rect(self) -> Rect | None:
        return self._rect

    @property
    def is_offscreen(self) -> bool:
        return self._offscreen

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def get_value(self) -> str | None:
        if not self.value_supported:
            return None
        return self._value

    def set_value(self, value: str) -> bool:
        if self.backend is not None:
            self.backend.set_value_calls.append((self, value))
        if not self.value_supported:
            return False
        self._value = value
        return True

    def set_focus(self) -> bool:
        if not self.focusable:
            return False
        if self.backend is not None:
            self.backend.focus_calls.append(self)
            if self.backend.focus_fails:
                return False
            self.backend.focused = self
        return True

    def click(self) -> bool:
        if self.backend is not None:
            self.backend.clicks.append(self)
        if self.click_action is not None and self.backend is not None:
            self.click_action(self.backend)
        return True

    # -- 测试辅助 ------------------------------------------------------
    def attach(self, backend: "FakeBackend") -> None:
        self.backend = backend
        for child in self._children:
            child.attach(backend)

    def find(self, *, cls: str | None = None, ctype: str | None = None) -> "FakeNode | None":
        for node in self.walk(include_self=True):
            if cls is not None and node.class_name != cls:
                continue
            if ctype is not None and node.control_type != ctype:
                continue
            return node
        return None

    def walk(self, *, include_self: bool = False):
        if include_self:
            yield self
        for child in self._children:
            yield child
            yield from child.walk()

    def append(self, child: "FakeNode") -> "FakeNode":
        self._children.append(child)
        if self.backend is not None:
            child.attach(self.backend)
        return child

    def set_children(self, children: Sequence["FakeNode"]) -> None:
        self._children = list(children)
        if self.backend is not None:
            for child in self._children:
                child.attach(self.backend)


class FakeBackend:
    """实现 ``backend.UiaBackend`` 协议的 fake 后端（记录一切动作）。"""

    def __init__(
        self,
        *,
        now: datetime | None = None,
        processes: Sequence[ProcessInfo] | None = None,
        windows: Sequence[FakeWindowDef] = (),
        foreground: int = NOTEPAD_HWND,
        clipboard: str = "",
        no_qq: bool = False,
    ) -> None:
        self.now_value = now or FIXED_NOW
        default_process = [ProcessInfo(pid=4242, name="QQ.exe", exe="QQ.exe")]
        self._processes = [] if no_qq else list(processes if processes is not None else default_process)
        self._windows: dict[int, FakeWindowDef] = {window.hwnd: window for window in windows}
        self.foreground = foreground
        self.clipboard = clipboard
        self.deny_foreground = 0
        self.focus_fails = False
        self.focus_override: FakeNode | None = None
        self.on_wheel: Callable[["FakeBackend", int], None] | None = None
        self.state: dict[str, Any] = {}
        # 动作记录
        self.calls: list[str] = []
        self.clicks: list[FakeNode] = []
        self.set_value_calls: list[tuple[FakeNode, str]] = []
        self.focus_calls: list[FakeNode] = []
        self.paste_calls: list[str] = []
        self.wheels: list[tuple[int, int, int]] = []
        self.sleeps: list[float] = []
        self.set_foreground_calls: list[int] = []
        self.show_window_calls: list[tuple[int, int]] = []
        self.clipboard_sets: list[str] = []
        self.cursor = (5, 6)
        self.cursor_sets: list[tuple[int, int]] = []
        self.closed = False
        self.roots_served: list[int] = []

    # -- 进程 / 窗口 ---------------------------------------------------
    def list_processes(self, names: Sequence[str]) -> list[ProcessInfo]:
        self.calls.append("list_processes")
        wanted = {str(name).casefold().removesuffix(".exe") for name in names}
        return [
            process
            for process in self._processes
            if process.name.casefold().removesuffix(".exe") in wanted
        ]

    def list_top_windows(self, names: Sequence[str]) -> list[WindowInfo]:
        self.calls.append("list_top_windows")
        pids = {process.pid for process in self.list_processes(names)}
        result: list[WindowInfo] = []
        for window in self._windows.values():
            if window.pid not in pids or not window.is_active(self):
                continue
            result.append(
                WindowInfo(
                    hwnd=window.hwnd,
                    pid=window.pid,
                    title=window.title,
                    class_name=window.class_name,
                    rect=window.rect,
                    visible=window.visible,
                    minimized=window.minimized,
                )
            )
        return result

    def root_from_handle(self, hwnd: int) -> FakeNode | None:
        self.calls.append(f"root_from_handle:{hwnd}")
        self.roots_served.append(hwnd)
        window = self._windows.get(hwnd)
        if window is None:
            return None
        if window.root_factory is not None:
            root = window.root_factory(self)
        else:
            root = window.root
        if root is not None:
            root.attach(self)
        return root

    # -- 前台 / 窗口状态 -----------------------------------------------
    def foreground_hwnd(self) -> int:
        return self.foreground

    def set_foreground(self, hwnd: int) -> bool:
        self.calls.append(f"set_foreground:{hwnd}")
        self.set_foreground_calls.append(hwnd)
        if self.deny_foreground > 0:
            self.deny_foreground -= 1
            return False
        if not hwnd:
            return False
        self.foreground = hwnd
        return True

    def show_window(self, hwnd: int, cmd: int) -> bool:
        self.calls.append(f"show_window:{hwnd}:{cmd}")
        self.show_window_calls.append((hwnd, cmd))
        window = self._windows.get(hwnd)
        if window is not None and cmd in (SW_RESTORE, SW_SHOW):
            window.minimized = False
            window.visible = True
        return True

    def window_visible(self, hwnd: int) -> bool:
        window = self._windows.get(hwnd)
        return window.visible if window is not None else True

    def window_minimized(self, hwnd: int) -> bool:
        window = self._windows.get(hwnd)
        return window.minimized if window is not None else False

    def focused_element(self) -> FakeNode | None:
        if self.focus_override is not None:
            return self.focus_override
        return self.focused

    # -- 鼠标 / 光标 ---------------------------------------------------
    def wheel_at(self, x: int, y: int, notches: int) -> None:
        self.calls.append(f"wheel_at:{x},{y},{notches}")
        self.wheels.append((x, y, notches))
        if self.on_wheel is not None:
            self.on_wheel(self, notches)

    def cursor_position(self) -> tuple[int, int]:
        return self.cursor

    def set_cursor_position(self, x: int, y: int) -> None:
        self.cursor_sets.append((x, y))
        self.cursor = (x, y)

    # -- 剪贴板 / 搜索框输入 -------------------------------------------
    def clipboard_get_text(self) -> str:
        return self.clipboard

    def clipboard_set_text(self, text: str) -> bool:
        self.clipboard_sets.append(text)
        if self.state.get("fail_clipboard_set"):
            return False
        self.clipboard = text
        return True

    def paste_text_into_focused_control(self, text: str) -> None:
        self.calls.append("paste_text_into_focused_control")
        self.paste_calls.append(text)
        saved = self.clipboard
        self.clipboard = text
        self.clipboard = saved

    # -- 杂项 ----------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def now(self) -> datetime:
        return self.now_value

    def check_environment(self) -> dict[str, Any]:
        return {
            "backend": "fake",
            "python": "3.12.14",
            "platform": "win32",
            "uiautomation_importable": True,
            "uiautomation_version": "fake",
        }

    def close(self) -> None:
        self.closed = True

    # -- 测试辅助 ------------------------------------------------------
    @property
    def clicks_on_message_editor(self) -> list[FakeNode]:
        return [node for node in self.clicks if "qq-msg-editor" in node.class_name]

    @property
    def clicks_on_buttons(self) -> list[FakeNode]:
        return [node for node in self.clicks if node.control_type.lower() == "button"]


# ---------------------------------------------------------------------------
# 节点树构造辅助
# ---------------------------------------------------------------------------

def text_node(name: str, **kwargs: Any) -> FakeNode:
    return FakeNode(name=name, ctype="Text", **kwargs)


def pane_node(cls: str = "", children: Sequence[FakeNode] = (), **kwargs: Any) -> FakeNode:
    return FakeNode(cls=cls, ctype="Pane", children=children, **kwargs)


def bubble_node(
    texts: Sequence[str],
    *,
    mode: str = "efficiency",
    is_self: bool = False,
    time_text: str | None = None,
    sender: str | None = None,
    rect: Rect = (10, 10, 400, 40),
    bubble_cls: str | None = None,
    sender_cls: str | None = None,
) -> FakeNode:
    base = bubble_cls or ("msg-content-container" if mode == "efficiency" else "message-container")
    cls = base + (" container--self" if is_self else "")
    children: list[FakeNode] = []
    if sender is not None:
        children.append(
            FakeNode(name=sender, cls=sender_cls or f"{base}__sender", ctype="Text")
        )
    for line in texts:
        children.append(text_node(line))
    if time_text is not None:
        children.append(FakeNode(name=time_text, cls=f"{base}__time", ctype="Text"))
    return FakeNode(cls=cls, ctype="Pane", children=children, rect=rect)


def msg_area_node(
    bubbles: Sequence[FakeNode],
    *,
    rect: Rect = (10, 110, 500, 600),
    extra_children: Sequence[FakeNode] = (),
) -> FakeNode:
    return FakeNode(
        cls="chat-msg-area",
        ctype="Pane",
        children=[*extra_children, *bubbles],
        rect=rect,
    )


def search_result_node(name: str, *, click_action: Callable[[FakeBackend], None] | None = None) -> FakeNode:
    if click_action is None:
        def click_action(backend: FakeBackend) -> None:  # noqa: ANN001
            backend.state["current_chat"] = name

    return FakeNode(name=name, cls="search-result-item", ctype="ListItem", click_action=click_action)


def panel_root(
    *,
    mode: str = "efficiency",
    current_header: str = "测试会话",
    search_value: str = "",
    search_value_supported: bool = True,
    show_results: bool = False,
    results: Sequence[FakeNode] = (),
    msg_bubbles: Sequence[FakeNode] = (),
    include_msg_area: bool = True,
    editor_as_edit: bool = False,
    toggle: bool = True,
    extra_children: Sequence[FakeNode] = (),
) -> FakeNode:
    children: list[FakeNode] = []
    if toggle:
        children.append(
            FakeNode(
                name="切换为经典模式" if mode == "efficiency" else "切换为效率模式",
                cls="narrow-toggler",
                ctype="Button",
                rect=(700, 20, 740, 40),
            )
        )
    children.append(
        FakeNode(
            name="",
            cls="search-edit",
            ctype="Edit",
            value=search_value,
            value_supported=search_value_supported,
            rect=(20, 40, 300, 70),
        )
    )
    if show_results:
        children.extend(results)
    children.append(
        FakeNode(
            name=current_header,
            cls="chat-header__contact-name",
            ctype="Text",
            rect=(10, 80, 300, 100),
        )
    )
    if include_msg_area:
        children.append(msg_area_node(msg_bubbles, rect=(10, 110, 500, 600)))
    children.append(
        FakeNode(
            name="",
            cls="qq-msg-editor",
            ctype="Edit" if editor_as_edit else "Document",
            rect=(10, 610, 500, 680),
        )
    )
    children.extend(extra_children)
    return FakeNode(name="QQ", cls="Chrome_WidgetWin_1", ctype="Window", children=children)


def chat_window_root(
    *,
    current_header: str = "测试会话",
    msg_bubbles: Sequence[FakeNode] = (),
    include_msg_area: bool = True,
    mode: str = "classic",
) -> FakeNode:
    children: list[FakeNode] = [
        FakeNode(
            name=current_header,
            cls="chat-header__contact-name",
            ctype="Text",
            rect=(10, 10, 300, 30),
        )
    ]
    if include_msg_area:
        children.append(msg_area_node(msg_bubbles, rect=(10, 40, 500, 500)))
    children.append(
        FakeNode(name="", cls="qq-msg-editor", ctype="Document", rect=(10, 510, 500, 580))
    )
    return FakeNode(name=current_header, cls="Chrome_WidgetWin_1", ctype="Window", children=children)


def make_window(
    hwnd: int,
    title: str = "",
    *,
    root: FakeNode | None = None,
    root_factory: Callable[[FakeBackend], FakeNode | None] | None = None,
    pid: int = 4242,
    visible: bool = True,
    minimized: bool = False,
    rect: Rect = (0, 0, 900, 700),
    class_name: str = "Chrome_WidgetWin_1",
    active_when: Callable[[FakeBackend], bool] | None = None,
) -> FakeWindowDef:
    return FakeWindowDef(
        hwnd=hwnd,
        title=title,
        pid=pid,
        class_name=class_name,
        rect=rect,
        visible=visible,
        minimized=minimized,
        root=root,
        root_factory=root_factory,
        active_when=active_when,
    )


def fake_descendant_count(root: FakeNode, limit: int = 500) -> int:
    count = 0
    for _node in root.walk(include_self=True):
        count += 1
        if count >= limit:
            break
    return count


__all__ = [
    "FIXED_NOW",
    "NOTEPAD_HWND",
    "PANEL_HWND",
    "CHAT_HWND",
    "FakeBackend",
    "FakeNode",
    "FakeWindowDef",
    "bubble_node",
    "chat_window_root",
    "fake_descendant_count",
    "make_window",
    "msg_area_node",
    "pane_node",
    "panel_root",
    "search_result_node",
    "text_node",
]
