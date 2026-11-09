"""统一的提示框 / 确认框封装（中文文案；测试中可整体替换本模块的函数）。

只使用 tkinter.messagebox（Windows 上按钮文字随系统语言显示为中文）。
确认类对话使用原生 askokcancel，默认按钮为「取消」，避免误操作。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox


def show_info(parent: tk.Misc | None, title: str, text: str) -> None:
    messagebox.showinfo(title, text, parent=parent)


def show_warning(parent: tk.Misc | None, title: str, text: str) -> None:
    messagebox.showwarning(title, text, parent=parent)


def show_error(parent: tk.Misc | None, title: str, text: str) -> None:
    messagebox.showerror(title, text, parent=parent)


def ask_confirm(
    parent: tk.Misc | None,
    title: str,
    text: str,
    *,
    confirm_text: str = "继续",
    cancel_text: str = "取消",
    danger: bool = False,
) -> bool:
    """是/否确认；返回用户是否点了确认（默认按钮是「取消」）。"""
    return bool(messagebox.askokcancel(title, text, parent=parent, default=messagebox.CANCEL))


__all__ = ["ask_confirm", "show_error", "show_info", "show_warning"]
