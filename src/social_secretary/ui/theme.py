"""Tkinter 界面主题：颜色、字体、ttk 样式与通用尺寸。

设计目标（面向无编程基础的普通用户）：
- 浅色、留白充足、正文 11pt 起、主要按钮高约 42px；
- 中文字体优先（微软雅黑 → 苹方 → Noto → Segoe UI 回退）；
- 纯本地实现：只使用 Python 标准库 tkinter/ttk 与系统字体，不联网、无外部资源。

为什么用 Tkinter 而不是 PySide6：目标用户本机安装的 Dell「Alienware Sound Center /
Nahimic OSD」会向 Qt GUI 进程注入 DLL，导致任何 QApplication 初始化直接崩溃（本机实测）。
Tkinter 不触发该注入，且属于 Python 标准库、无需额外依赖、安装包更小。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

APP_TITLE = "AI 社交秘书（只读整理，不会发送消息）"

COLORS: dict[str, str] = {
    "bg": "#F3F6FB",
    "sidebar": "#FFFFFF",
    "card": "#FFFFFF",
    "border": "#DCE3EE",
    "text": "#1E2430",
    "muted": "#5B6676",
    "faint": "#8B95A5",
    "primary": "#1F5FD6",
    "primary_dark": "#1749A6",
    "primary_soft": "#E6EEFC",
    "danger": "#C62828",
    "danger_dark": "#A11F1F",
    "danger_soft": "#FDECEC",
    "warning": "#8A5300",
    "warning_soft": "#FFF3DF",
    "success": "#1B7A3D",
    "success_soft": "#E8F6EC",
    "demo": "#8A5300",
    "demo_soft": "#FFE8BD",
    "row_alt": "#F8FAFD",
}

FONT_FAMILY_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Segoe UI",
)

_base_scale = 1.0


def font_scale() -> float:
    return _base_scale


def set_font_scale(scale: float) -> None:
    """运行期调整界面字号（可访问性）；调用后需要重建/刷新界面。"""
    global _base_scale
    _base_scale = max(0.8, min(2.0, float(scale)))


def _pt(points: float) -> int:
    return max(7, round(points * _base_scale))


class Theme:
    """一次界面实例的主题：字体族、字号与颜色。"""

    def __init__(self, root: tk.Misc) -> None:
        available = set(tkfont.families(root))
        self.family = next(
            (name for name in FONT_FAMILY_CANDIDATES if name in available), "Segoe UI"
        )
        self.colors = COLORS
        self.fonts: dict[str, tuple] = {
            "base": (self.family, _pt(11)),
            "small": (self.family, _pt(10)),
            "tiny": (self.family, _pt(9)),
            "bold": (self.family, _pt(11), "bold"),
            "h1": (self.family, _pt(20), "bold"),
            "h2": (self.family, _pt(13), "bold"),
            "card_title": (self.family, _pt(12), "bold"),
            "count": (self.family, _pt(24), "bold"),
            "button": (self.family, _pt(11)),
            "button_bold": (self.family, _pt(11), "bold"),
            "nav": (self.family, _pt(12)),
            "nav_active": (self.family, _pt(12), "bold"),
            "mono": (self.family, _pt(11)),
        }
        self._root = root
        self._setup_ttk_styles()

    def color(self, name: str) -> str:
        return self.colors[name]

    def font(self, name: str) -> tuple:
        return self.fonts[name]

    def _setup_ttk_styles(self) -> None:
        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover - 极端环境下回退默认主题
            pass
        style.configure(
            "Vertical.TScrollbar",
            background="#C4CEDD",
            troughcolor=self.colors["bg"],
            bordercolor=self.colors["bg"],
            arrowcolor=self.colors["muted"],
            width=12,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#FFFFFF",
            background="#FFFFFF",
            foreground=self.colors["text"],
            borderwidth=1,
            relief="solid",
            padding=5,
            font=self.fonts["base"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#FFFFFF")],
            foreground=[("readonly", self.colors["text"])],
        )
        style.configure("TSpinbox", fieldbackground="#FFFFFF", foreground=self.colors["text"], padding=4)
        style.configure("TCheckbutton", background=self.colors["card"], foreground=self.colors["text"], font=self.fonts["base"])
        self.style = style


def apply_theme(root: tk.Misc) -> Theme:
    """创建主题对象并设置全局默认字体（只对 ttk 生效；tk 控件用显式字体）。"""
    theme = Theme(root)
    try:
        root.option_add("*Font", theme.font("base"))
        root.option_add("*TCombobox*Listbox.font", theme.font("base"))
    except tk.TclError:  # pragma: no cover
        pass
    return theme


__all__ = ["APP_TITLE", "COLORS", "Theme", "apply_theme", "font_scale", "set_font_scale"]
