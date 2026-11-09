"""前台窗口协调：读取 QQ 前保存用户窗口，读取结束后恢复。

铁律相关背景：QQ NT 只在**前台**时构建完整 UIA 树，因此只读采集也必须短暂把
QQ 窗口置前。为了尽量不打扰用户：

1. 进入读取前记录当前前台窗口 hwnd、是否最小化、鼠标位置；
2. 第一次真正切换前台前，通过 ``warning_callback`` 让 UI 层向用户弹出提示
   （用户可据此暂停操作）；
3. 读取结束（包括异常/中途失败）时恢复鼠标位置与原来的前台窗口，
   若恢复失败则再次通过 ``warning_callback`` 提示用户手动切回。
"""

from __future__ import annotations

from collections.abc import Callable

from .backend import SW_RESTORE, SW_SHOW, UiaBackend

_WARNING_TAKEOVER = (
    "即将短暂占用 QQ 前台窗口以读取消息，读取完成后会自动恢复原来的前台窗口。"
    "读取期间请勿操作鼠标键盘。"
)
_WARNING_RESTORE_FAILED = "读取结束后未能自动恢复原来的前台窗口，请手动切换回原窗口。"


def activate_window(
    backend: UiaBackend,
    hwnd: int,
    *,
    attempts: int = 4,
    poll_interval: float = 0.25,
    guard: "ForegroundGuard | None" = None,
) -> bool:
    """把窗口恢复并置前，最多尝试 ``attempts`` 次；返回是否成功置前。"""

    if not hwnd:
        return False
    if guard is not None:
        guard.before_activate(hwnd)
    if backend.window_minimized(hwnd):
        backend.show_window(hwnd, SW_RESTORE)
    elif not backend.window_visible(hwnd):
        # QQ 在读完一个会话后可能被 Chromium 隐藏/收起到托盘（不是最小化）；
        # SetForegroundWindow 对隐藏窗口无效，必须先 SW_SHOW 再置前。
        backend.show_window(hwnd, SW_SHOW)
    if backend.foreground_hwnd() == hwnd:
        return True
    for _ in range(max(1, int(attempts))):
        backend.set_foreground(hwnd)
        if backend.foreground_hwnd() == hwnd:
            return True
        backend.sleep(poll_interval)
    return backend.foreground_hwnd() == hwnd


class ForegroundGuard:
    """上下文管理器：记录并恢复用户原来的前台窗口 / 最小化状态 / 鼠标位置。"""

    def __init__(
        self,
        backend: UiaBackend,
        *,
        warning_callback: Callable[[str], None] | None = None,
        restore_cursor: bool = True,
        restore_attempts: int = 3,
        poll_interval: float = 0.1,
    ) -> None:
        self._backend = backend
        self._warning_callback = warning_callback
        self._restore_cursor = restore_cursor
        self._restore_attempts = max(1, int(restore_attempts))
        self._poll_interval = poll_interval
        self._original_hwnd: int = 0
        self._was_minimized: bool = False
        self._cursor: tuple[int, int] | None = None
        self._activation_warned: bool = False
        self.foreground_used: bool = False
        self.restored: bool = False
        self.closed: bool = False

    @property
    def original_hwnd(self) -> int:
        return self._original_hwnd

    def __enter__(self) -> "ForegroundGuard":
        try:
            self._original_hwnd = self._backend.foreground_hwnd()
            if self._original_hwnd:
                self._was_minimized = self._backend.window_minimized(self._original_hwnd)
            if self._restore_cursor:
                self._cursor = self._backend.cursor_position()
        except Exception:  # pragma: no cover - 防御性：前台协调失败不影响主流程
            self._original_hwnd = 0
        return self

    def _warn(self, message: str) -> None:
        if self._warning_callback is not None:
            try:
                self._warning_callback(message)
            except Exception:  # pragma: no cover - UI 回调不应影响采集
                pass

    def before_activate(self, target_hwnd: int) -> None:
        """在即将切换前台前调用；只提示一次。"""

        try:
            current = self._backend.foreground_hwnd()
        except Exception:  # pragma: no cover
            current = 0
        if target_hwnd and current != target_hwnd:
            self.foreground_used = True
            if not self._activation_warned:
                self._activation_warned = True
                self._warn(_WARNING_TAKEOVER)

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # noqa: ANN001 - 上下文管理器协议
        if self.closed:
            return
        self.closed = True
        if self._restore_cursor and self._cursor is not None:
            try:
                self._backend.set_cursor_position(*self._cursor)
            except Exception:  # pragma: no cover
                pass
        original = self._original_hwnd
        if not original:
            self.restored = True
            return
        try:
            if self._was_minimized:
                self._backend.show_window(original, SW_RESTORE)
            elif not self._backend.window_visible(original):
                self._backend.show_window(original, SW_SHOW)
        except Exception:  # pragma: no cover
            pass
        for _attempt in range(self._restore_attempts):
            try:
                if self._backend.foreground_hwnd() == original:
                    self.restored = True
                    return
                self._backend.set_foreground(original)
                if self._backend.foreground_hwnd() == original:
                    self.restored = True
                    return
            except Exception:  # pragma: no cover
                pass
            self._backend.sleep(self._poll_interval)
        self.restored = self._backend.foreground_hwnd() == original
        if not self.restored:
            self._warn(_WARNING_RESTORE_FAILED)


__all__ = ["ForegroundGuard", "activate_window"]
