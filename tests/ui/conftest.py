"""UI 测试公共配置：FakeService、共享 Tk 根窗口、弹窗替身 fixture。

说明：新界面使用 Python 标准库 Tkinter（不使用 pytest-qt，也不再需要 PySide6/PyQt）。
Tk 在同一进程里反复创建/销毁多个 Tk() 根窗口容易触发 Tcl 初始化异常，因此测试共享
一个 session 级 Tk 根窗口，每个测试只重建根窗口里的子树。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fake_service import FakeService  # noqa: E402


def _disable_legacy_extension_points() -> bool:
    """Windows-only：禁用当前 pytest 进程的遗留扩展点（AppInit DLL 一类注入）。

    背景（宿主环境，非本项目代码问题）：目标机器装有 Dell「Alienware Sound Center /
    Nahimic OSD」，它通过 Windows 遗留扩展点向 GUI 进程注入 NahimicOSD.dll。若同一个
    pytest 进程里更早的测试 import 过 PySide6（旧 e2e 测试遗留），随后创建 Tk 窗口会
    在注入的 DLL 里崩溃（0xC0000005）。在测试会话开始前禁用扩展点可避免该宿主问题；
    Tkinter 界面不依赖这些扩展点，失败时静默跳过。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        class _Policy(ctypes.Structure):
            _fields_ = [("Flags", ctypes.c_ulong)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetProcessMitigationPolicy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        kernel32.SetProcessMitigationPolicy.restype = ctypes.c_int
        policy = _Policy(1)  # PROCESS_MITIGATION_EXTENSION_POINT_DISABLE_POLICY: Enable = 1
        return bool(kernel32.SetProcessMitigationPolicy(6, ctypes.byref(policy), ctypes.sizeof(policy)))
    except Exception:  # noqa: BLE001 - 加固失败不应影响测试
        return False


_disable_legacy_extension_points()

# 全仓库跑测试时，tests/mail 等更早的测试会先创建再销毁一个临时 Tk() 探测 GUI；
# 之后 UI 会话再创建 Tk()，本机 Tcl/Tk 偶发无法重新定位 tk.tcl（"Can't find a usable tk.tcl"），
# 导致整个 tests/ui 在 fixture 初始化阶段报错。解决办法：在收集阶段（任何测试执行前）
# 就先创建 UI 会话共享的隐藏根窗口，后续测试只复用它。
_EARLY_TK_ROOT: tk.Tk | None = None


def _create_early_tk_root() -> None:
    global _EARLY_TK_ROOT
    try:
        root = tk.Tk()
        root.withdraw()
        _EARLY_TK_ROOT = root
    except Exception:  # noqa: BLE001 - 环境无 GUI 时退回 fixture 内创建
        _EARLY_TK_ROOT = None


_create_early_tk_root()


@pytest.fixture(scope="session")
def tk_root():
    """整个测试会话共享一个隐藏的 Tk 根窗口。"""
    global _EARLY_TK_ROOT
    root = _EARLY_TK_ROOT
    try:
        usable = root is not None and root.winfo_exists()
    except Exception:  # noqa: BLE001
        usable = False
    if not usable:
        root = tk.Tk()
        root.withdraw()
        _EARLY_TK_ROOT = root
    yield root
    try:
        root.destroy()
    except Exception:  # noqa: BLE001 - 已销毁
        pass
    _EARLY_TK_ROOT = None


def _destroy_root_children(tk_root) -> None:
    for child in list(tk_root.winfo_children()):
        try:
            child.destroy()
        except Exception:  # noqa: BLE001
            pass
    try:
        tk_root.protocol("WM_DELETE_WINDOW", lambda: None)
        tk_root.withdraw()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def cleanup_root(tk_root):
    """测试结束后清理共享根窗口里的全部控件。"""
    yield _destroy_root_children
    _destroy_root_children(tk_root)


@pytest.fixture()
def service() -> FakeService:
    """全新的内存假服务（已加载合成演示数据）。"""
    return FakeService()


@pytest.fixture()
def window(tk_root, cleanup_root, service):
    """创建主窗口（挂在共享根窗口上，隐藏状态），测试结束时清理。"""
    from social_secretary.ui.main_window import AppWindow

    win = AppWindow(service, root=tk_root)
    tk_root.withdraw()
    yield win


@pytest.fixture()
def shown_window(window, tk_root):
    """显示出来的主窗口（用于需要渲染的测试）。"""
    window.show()
    window.root.update()
    yield window
    try:
        tk_root.withdraw()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def notify_records(monkeypatch):
    """替换 ui.notify 的弹窗函数，避免测试被模态窗口阻塞并记录调用。"""
    from social_secretary.ui import notify

    records: dict[str, object] = {
        "info": [],
        "warning": [],
        "error": [],
        "confirm": [],
        "confirm_result": True,
    }

    def show_info(parent, title, text):
        records["info"].append((title, text))

    def show_warning(parent, title, text):
        records["warning"].append((title, text))

    def show_error(parent, title, text):
        records["error"].append((title, text))

    def ask_confirm(parent, title, text, *, confirm_text="继续", cancel_text="取消", danger=False):
        records["confirm"].append((title, text, confirm_text, danger))
        return bool(records["confirm_result"])

    monkeypatch.setattr(notify, "show_info", show_info)
    monkeypatch.setattr(notify, "show_warning", show_warning)
    monkeypatch.setattr(notify, "show_error", show_error)
    monkeypatch.setattr(notify, "ask_confirm", ask_confirm)
    return records


@pytest.fixture()
def evidence_dir() -> Path:
    path = REPO_ROOT / "evidence" / "ui"
    path.mkdir(parents=True, exist_ok=True)
    return path
