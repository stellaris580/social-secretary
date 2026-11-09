"""QQ UIA 后端：把 Win32 / uiautomation 的真实操作封装成可测试的接口。

约束（与 AGENTS.md 铁律一致）：
- 本模块只提供：进程/窗口枚举、UIA 树只读、鼠标滚轮（消息区滚动）、
  搜索框写值/聚焦、剪贴板回退（临时保存并恢复）、前台窗口切换/恢复。
- **不提供任何发送消息、点击发送按钮、向消息编辑框输入文本的能力**。
- uiautomation 采用延迟导入：仅导入本模块不会初始化 COM，离线测试可安全运行。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import time
from pathlib import Path
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .errors import QqSafetyViolation, QqUnavailable
from .nodes import Rect, UiaNode

SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    exe: str | None = None


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: Rect
    visible: bool
    minimized: bool

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def area(self) -> int:
        return self.width * self.height


@runtime_checkable
class UiaBackend(Protocol):
    """采集器依赖的全部「机器操作」；离线测试注入 fake 实现。"""

    def list_processes(self, names: Sequence[str]) -> list[ProcessInfo]: ...

    def list_top_windows(self, names: Sequence[str]) -> list[WindowInfo]: ...

    def root_from_handle(self, hwnd: int) -> UiaNode | None: ...

    def foreground_hwnd(self) -> int: ...

    def set_foreground(self, hwnd: int) -> bool: ...

    def show_window(self, hwnd: int, cmd: int) -> bool: ...

    def window_visible(self, hwnd: int) -> bool: ...

    def window_minimized(self, hwnd: int) -> bool: ...

    def focused_element(self) -> UiaNode | None: ...

    def wheel_at(self, x: int, y: int, notches: int) -> None:
        """在指定点滚动鼠标滚轮；notches>0 向上（历史），<0 向下（回到底部）。"""

    def cursor_position(self) -> tuple[int, int]: ...

    def set_cursor_position(self, x: int, y: int) -> None: ...

    def clipboard_get_text(self) -> str: ...

    def clipboard_set_text(self, text: str) -> bool: ...

    def paste_text_into_focused_control(self, text: str) -> None:
        """仅允许用于主面板搜索框（调用方必须先做焦点/类名安全校验）。"""

    def sleep(self, seconds: float) -> None: ...

    def now(self) -> datetime: ...

    def check_environment(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


def paste_with_clipboard_restore(
    text: str,
    *,
    get_text,
    set_text,
    send_select_all,
    send_paste,
    sleep=None,
) -> None:
    """保存 -> 写入 -> Ctrl+A/Ctrl+V -> 恢复 的用户剪贴板回退。

    拆成注入式小函数是为了可离线单测「恢复一定发生」（含异常路径）。
    """

    saved = get_text()
    try:
        if not set_text(text):
            raise QqUnavailable("无法写入系统剪贴板，已放弃搜索框剪贴板回退")
        if sleep is not None:
            sleep(0.05)
        send_select_all()
        if sleep is not None:
            sleep(0.05)
        send_paste()
        if sleep is not None:
            sleep(0.1)
    finally:
        set_text(saved)


# ---------------------------------------------------------------------------
# Win32 基础封装
# ---------------------------------------------------------------------------

_user32 = None
_kernel32 = None


_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def _win32():
    global _user32, _kernel32
    if _user32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        user32.mouse_event.restype = None
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _user32, _kernel32 = user32, kernel32
    return _user32, _kernel32


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _enum_processes() -> list[ProcessInfo]:
    """Toolhelp32 枚举进程（不用 WMI；只读）。"""

    _user32, kernel32 = _win32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) in (0, -1):
        return []
    result: list[ProcessInfo] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            exe = entry.szExeFile
            result.append(ProcessInfo(pid=int(entry.th32ProcessID), name=exe, exe=exe))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _enum_top_windows() -> list[WindowInfo]:
    user32, _kernel32 = _win32()
    results: list[WindowInfo] = []

    def _callback(hwnd, _lparam):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title_buf = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, title_buf, 1024)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append(
            WindowInfo(
                hwnd=int(hwnd),
                pid=int(pid.value),
                title=title_buf.value,
                class_name=class_buf.value,
                rect=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                visible=bool(user32.IsWindowVisible(hwnd)),
                minimized=bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    callback = _WNDENUMPROC(_callback)
    user32.EnumWindows(callback, 0)
    return results


def _process_exe_name(pid: int) -> str:
    """用 QueryFullProcessImageNameW 取进程 exe 文件名（不依赖 Toolhelp 枚举）。

    背景：某些受限环境（如 DSH 沙箱）里 Toolhelp 快照可能只返回部分进程，导致
    ``list_processes`` 看不到 QQ 主窗口所属进程；但窗口枚举 + 进程路径查询仍可用。
    """
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            ok = kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            )
            return Path(buf.value).name if ok else ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - 环境差异
        return ""


def _name_matches(exe_name: str, wanted: Sequence[str]) -> bool:
    base = (exe_name or "").casefold()
    if base.endswith(".exe"):
        base = base[:-4]
    return any(base == str(name).casefold().removesuffix(".exe") for name in wanted)


def _normalize_control_type(name: Any) -> str:
    value = str(name or "").strip()
    if value.endswith("Control") and value != "Control":
        value = value[: -len("Control")]
    return value or "Unknown"


class RealNode:
    """uiautomation Control 的只读包装（children 懒加载，异常安全）。"""

    def __init__(self, control, uia_module) -> None:
        self._control = control
        self._uia = uia_module
        self._children: list["RealNode"] | None = None

    @property
    def name(self) -> str:
        try:
            return str(self._control.Name or "")
        except Exception:  # pragma: no cover - COM 异常
            return ""

    @property
    def class_name(self) -> str:
        try:
            return str(self._control.ClassName or "")
        except Exception:  # pragma: no cover
            return ""

    @property
    def control_type(self) -> str:
        try:
            return _normalize_control_type(self._control.ControlTypeName)
        except Exception:  # pragma: no cover
            return "Unknown"

    # --- A1 第一阶段诊断只读属性（仅 --uia-identity-probe 使用；不参与生产采集/去重） ---
    @property
    def automation_id(self) -> str:
        """只读诊断：UIA AutomationId（不用于生产逻辑）。"""
        try:
            return str(self._control.AutomationId or "")
        except Exception:  # pragma: no cover - COM 异常
            return ""

    @property
    def runtime_id(self) -> tuple[int, ...]:
        """只读诊断：UIA RuntimeId（不用于生产逻辑）。"""
        try:
            value = self._control.GetRuntimeId()
            return tuple(int(item) for item in value) if value else ()
        except Exception:  # pragma: no cover
            return ()

    @property
    def native_window_handle(self) -> int:
        """只读诊断：原生窗口句柄（不用于生产逻辑）。"""
        try:
            return int(self._control.NativeWindowHandle or 0)
        except Exception:  # pragma: no cover
            return 0

    @property
    def framework_id(self) -> str:
        """只读诊断：UIA FrameworkId（不用于生产逻辑）。"""
        try:
            value = getattr(self._control, "FrameworkIdName", None)
            if value is None:
                value = getattr(self._control, "FrameworkId", "")
            return str(value or "")
        except Exception:  # pragma: no cover
            return ""

    @property
    def children(self) -> Sequence["RealNode"]:
        if self._children is None:
            try:
                self._children = [RealNode(child, self._uia) for child in self._control.GetChildren()]
            except Exception:  # pragma: no cover - UIA 树重建/COM 异常
                self._children = []
        return self._children

    @property
    def rect(self) -> Rect | None:
        try:
            rectangle = self._control.BoundingRectangle
            return (int(rectangle.left), int(rectangle.top), int(rectangle.right), int(rectangle.bottom))
        except Exception:  # pragma: no cover
            return None

    @property
    def is_offscreen(self) -> bool:
        try:
            return bool(self._control.IsOffscreen)
        except Exception:  # pragma: no cover
            return False

    @property
    def is_enabled(self) -> bool:
        try:
            return bool(self._control.IsEnabled)
        except Exception:  # pragma: no cover
            return False

    def get_value(self) -> str | None:
        try:
            pattern = self._control.GetPattern(self._uia.PatternId.ValuePattern)
            if pattern is None:
                return None
            return str(pattern.Value or "")
        except Exception:  # pragma: no cover
            return None

    def set_value(self, value: str) -> bool:
        try:
            pattern = self._control.GetPattern(self._uia.PatternId.ValuePattern)
            if pattern is None:
                return False
            try:
                if pattern.IsReadOnly:
                    return False
            except Exception:  # pragma: no cover
                pass
            pattern.SetValue(value)
            return True
        except Exception:  # pragma: no cover
            return False

    def set_focus(self) -> bool:
        try:
            self._control.SetFocus()
            return True
        except Exception:  # pragma: no cover
            return False

    def click(self) -> bool:
        """受控点击（仅由采集器对已验证类型的搜索结果/会话列表项调用）。

        只读边界：调用方必须先校验控件类型（搜索结果为 ListItem；会话列表项为
        recent-contact-item Group），绝不允许对发送按钮/消息编辑框调用。
        """
        try:
            self._control.Click()
            return True
        except Exception:  # pragma: no cover
            return False


class NativeBackend:
    """真实 Windows 后端（延迟导入 uiautomation；不启动/不修改 QQ）。"""

    def __init__(self, *, dpi_aware: bool = True, wheel_interval: float = 0.06) -> None:
        self._uia = None
        self._dpi_aware = dpi_aware
        self._wheel_interval = wheel_interval

    # -- uiautomation 延迟初始化 -------------------------------------------------
    def _ensure_uia(self):
        if self._uia is not None:
            return self._uia
        try:
            import uiautomation  # 局部导入：离线测试不触发 COM
        except Exception as exc:  # pragma: no cover - 环境缺失
            raise QqUnavailable(f"未能导入 uiautomation：{exc}") from exc
        try:
            # 主线程/工作线程都显式初始化 COM（已初始化时返回 S_FALSE，忽略即可）。
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except Exception:  # pragma: no cover
            pass
        if self._dpi_aware:
            try:
                uiautomation.SetProcessDpiAwareness(2)  # PerMonitorHighDPIAware
            except Exception:  # pragma: no cover
                pass
        self._uia = uiautomation
        return uiautomation

    # -- 进程 / 窗口 -------------------------------------------------------------
    def list_processes(self, names: Sequence[str]) -> list[ProcessInfo]:
        try:
            return [process for process in _enum_processes() if _name_matches(process.name, names)]
        except OSError:  # pragma: no cover - 沙箱限制
            return []

    def list_top_windows(self, names: Sequence[str]) -> list[WindowInfo]:
        processes = self.list_processes(names)
        known_pids = {process.pid for process in processes}
        try:
            windows = _enum_top_windows()
        except OSError:  # pragma: no cover
            return []
        matched: list[WindowInfo] = []
        for window in windows:
            if window.pid in known_pids:
                matched.append(window)
                continue
            # 兜底：受限环境下 Toolhelp 可能看不到主窗口进程，用窗口 pid 查 exe 名匹配。
            exe = _process_exe_name(window.pid)
            if exe and _name_matches(exe, names):
                matched.append(window)
        return matched

    def root_from_handle(self, hwnd: int) -> UiaNode | None:
        uia = self._ensure_uia()
        try:
            control = uia.ControlFromHandle(int(hwnd))
        except Exception:  # pragma: no cover - 窗口已销毁
            return None
        if control is None:
            return None
        return RealNode(control, uia)

    # -- 前台 / 窗口状态 ---------------------------------------------------------
    def foreground_hwnd(self) -> int:
        user32, _kernel32 = _win32()
        return int(user32.GetForegroundWindow() or 0)

    def set_foreground(self, hwnd: int) -> bool:
        user32, _kernel32 = _win32()
        try:
            return bool(user32.SetForegroundWindow(int(hwnd)))
        except OSError:  # pragma: no cover
            return False

    def show_window(self, hwnd: int, cmd: int) -> bool:
        user32, _kernel32 = _win32()
        try:
            return bool(user32.ShowWindow(int(hwnd), int(cmd)))
        except OSError:  # pragma: no cover
            return False

    def window_visible(self, hwnd: int) -> bool:
        user32, _kernel32 = _win32()
        try:
            return bool(user32.IsWindowVisible(int(hwnd)))
        except OSError:  # pragma: no cover
            return False

    def window_minimized(self, hwnd: int) -> bool:
        user32, _kernel32 = _win32()
        try:
            return bool(user32.IsIconic(int(hwnd)))
        except OSError:  # pragma: no cover
            return False

    def focused_element(self) -> UiaNode | None:
        uia = self._ensure_uia()
        try:
            control = uia.GetFocusedControl()
        except Exception:  # pragma: no cover
            return None
        if control is None:
            return None
        return RealNode(control, uia)

    # -- 鼠标 / 光标 -------------------------------------------------------------
    def wheel_at(self, x: int, y: int, notches: int) -> None:
        if not notches:
            return
        user32, _kernel32 = _win32()
        user32.SetCursorPos(int(x), int(y))
        direction = 1 if notches > 0 else -1
        for _ in range(abs(int(notches))):
            delta = WHEEL_DELTA * direction
            user32.mouse_event(
                wintypes.DWORD(MOUSEEVENTF_WHEEL),
                wintypes.DWORD(0),
                wintypes.DWORD(0),
                wintypes.DWORD(delta & 0xFFFFFFFF),
                ctypes.c_size_t(0),
            )
            time.sleep(self._wheel_interval)

    def cursor_position(self) -> tuple[int, int]:
        user32, _kernel32 = _win32()
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def set_cursor_position(self, x: int, y: int) -> None:
        user32, _kernel32 = _win32()
        user32.SetCursorPos(int(x), int(y))

    # -- 剪贴板 / 键盘（仅搜索框回退） -------------------------------------------
    def _clipboard_only_text(self) -> bool:
        uia = self._ensure_uia()
        try:
            formats = set(uia.GetClipboardFormats())
        except Exception:  # pragma: no cover
            return False
        # CF_TEXT=1, CF_UNICODETEXT=13；存在其他格式（图片/文件）时拒绝覆盖。
        return formats.issubset({1, 13})

    def clipboard_get_text(self) -> str:
        uia = self._ensure_uia()
        try:
            return str(uia.GetClipboardText() or "")
        except Exception:  # pragma: no cover
            return ""

    def clipboard_set_text(self, text: str) -> bool:
        uia = self._ensure_uia()
        try:
            return bool(uia.SetClipboardText(text))
        except Exception:  # pragma: no cover
            return False

    def paste_text_into_focused_control(self, text: str) -> None:
        """只允许搜索框使用：保存剪贴板 -> Ctrl+A/Ctrl+V -> 恢复剪贴板。"""

        if not self._clipboard_only_text():
            raise QqSafetyViolation(
                "系统剪贴板包含非文本内容（图片/文件等），为避免丢失您的剪贴板数据，"
                "已放弃剪贴板回退；请保持 QQ 主面板搜索框可用 ValuePattern 写入。"
            )
        uia = self._ensure_uia()

        def send_keys(keys: str) -> None:
            uia.SendKeys(keys, interval=0.01, waitTime=0.05)

        paste_with_clipboard_restore(
            text,
            get_text=self.clipboard_get_text,
            set_text=self.clipboard_set_text,
            send_select_all=lambda: send_keys("{Ctrl}a"),
            send_paste=lambda: send_keys("{Ctrl}v"),
            sleep=self.sleep,
        )

    # -- 杂项 -------------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def check_environment(self) -> dict[str, Any]:
        env: dict[str, Any] = {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": sys.platform,
            "dpi_aware": True,
        }
        try:
            uia = self._ensure_uia()
            env["uiautomation_version"] = str(getattr(uia, "VERSION", "") or getattr(uia, "__version__", ""))
            env["uiautomation_importable"] = True
        except Exception as exc:  # pragma: no cover
            env["uiautomation_importable"] = False
            env["uiautomation_error"] = type(exc).__name__
        return env

    def close(self) -> None:
        self._uia = None


__all__ = [
    "ProcessInfo",
    "WindowInfo",
    "UiaBackend",
    "NativeBackend",
    "RealNode",
    "paste_with_clipboard_restore",
    "SW_HIDE",
    "SW_SHOW",
    "SW_MINIMIZE",
    "SW_RESTORE",
]
