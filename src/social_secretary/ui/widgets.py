"""Tkinter 共享小部件：卡片、横幅、大按钮、可滚动容器、列表辅助。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable

from .theme import Theme

__all__ = [
    "Banner",
    "Card",
    "big_button",
    "clear_children",
    "hint",
    "label",
    "make_scrollable",
    "select_first",
]


def label(
    parent: tk.Misc,
    theme: Theme,
    text: str = "",
    *,
    kind: str = "base",
    color: str | None = None,
    bg: str | None = None,
    wrap: int | None = None,
    justify: str = "left",
    anchor: str = "w",
) -> tk.Label:
    """统一风格的文本标签。kind 对应 theme.fonts；color 可覆盖前景色。"""
    widget = tk.Label(
        parent,
        text=text,
        font=theme.font(kind),
        fg=color or theme.color("text"),
        bg=bg or theme.color("card"),
        justify=justify,
        anchor=anchor,
        wraplength=wrap or 0,
    )
    return widget


def hint(parent: tk.Misc, theme: Theme, text: str, *, wrap: int | None = None, bg: str | None = None) -> tk.Label:
    widget = label(parent, theme, text, kind="small", color=theme.color("muted"), wrap=wrap, bg=bg)
    return widget


def big_button(
    parent: tk.Misc,
    theme: Theme,
    text: str,
    command: Callable[[], None] | None = None,
    *,
    kind: str = "normal",
    width: int | None = None,
    state: str = "normal",
) -> tk.Button:
    """大按钮（最小高度约 42px）。kind: normal / primary / danger / link。"""
    if kind == "primary":
        bg, fg, active = theme.color("primary"), "#FFFFFF", theme.color("primary_dark")
        font = theme.font("button_bold")
    elif kind == "danger":
        bg, fg, active = theme.color("danger"), "#FFFFFF", theme.color("danger_dark")
        font = theme.font("button_bold")
    elif kind == "link":
        bg, fg, active = theme.color("bg"), theme.color("primary"), theme.color("primary_soft")
        font = theme.font("small")
        button = tk.Button(
            parent,
            text=text,
            command=command,
            font=font,
            fg=fg,
            bg=bg,
            activebackground=active,
            activeforeground=fg,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            anchor="w",
            padx=4,
            pady=0,
            highlightthickness=0,
            state=state,
        )
        if width:
            button.configure(width=width)
        return button
    else:
        bg, fg, active = "#FFFFFF", theme.color("text"), "#EEF3FB"
        font = theme.font("button")
    button = tk.Button(
        parent,
        text=text,
        command=command,
        font=font,
        fg=fg,
        bg=bg,
        activebackground=active,
        activeforeground="#FFFFFF" if kind in ("primary", "danger") else fg,
        relief="flat",
        borderwidth=0,
        cursor="hand2",
        padx=16,
        pady=8,
        highlightthickness=1,
        highlightbackground=theme.color("border"),
        highlightcolor=theme.color("primary"),
        state=state,
    )
    if width:
        button.configure(width=width)
    return button


class Card(tk.Frame):
    """白底圆角感卡片：1px 边框 + 标题 + 垂直内容区。"""

    def __init__(self, parent: tk.Misc, theme: Theme, title: str = "", *, padding: int = 16) -> None:
        super().__init__(
            parent,
            bg=theme.color("card"),
            highlightbackground=theme.color("border"),
            highlightthickness=1,
            bd=0,
        )
        self.theme = theme
        self._pad = padding
        self.body = tk.Frame(self, bg=theme.color("card"))
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)
        if title:
            self.title_label = label(self.body, theme, title, kind="card_title", bg=theme.color("card"))
            self.title_label.pack(fill="x", anchor="w", pady=(0, 8))
        else:
            self.title_label = None

    def add(self, widget: tk.Widget, **pack_options) -> tk.Widget:
        options = {"fill": "x", "anchor": "w", **pack_options}
        widget.pack(**options)
        return widget

    def row(self, padding: int = 0, fill: str = "x") -> tk.Frame:
        frame = tk.Frame(self.body, bg=self.theme.color("card"))
        frame.pack(fill=fill, pady=padding)
        return frame


class Banner(tk.Frame):
    """提示横幅。kind: warning / info / success。"""

    _STYLES = {
        "warning": ("warning_soft", "warning"),
        "info": ("primary_soft", "primary_dark"),
        "success": ("success_soft", "success"),
    }

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        text: str = "",
        *,
        kind: str = "warning",
        wrap: int | None = None,
    ) -> None:
        bg_name, fg_name = self._STYLES.get(kind, self._STYLES["warning"])
        super().__init__(parent, bg=theme.color(bg_name), highlightthickness=0, bd=0)
        self.theme = theme
        self.label = tk.Label(
            self,
            text=text,
            font=theme.font("small"),
            fg=theme.color(fg_name),
            bg=theme.color(bg_name),
            justify="left",
            anchor="w",
            wraplength=wrap or 0,
        )
        self.label.pack(fill="x", padx=12, pady=8)
        self._kind = kind

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)

    def text(self) -> str:
        return str(self.label.cget("text"))

    def set_kind(self, kind: str) -> None:
        bg_name, fg_name = self._STYLES.get(kind, self._STYLES["warning"])
        self._kind = kind
        self.configure(bg=self.theme.color(bg_name))
        self.label.configure(bg=self.theme.color(bg_name), fg=self.theme.color(fg_name))


def clear_children(widget: tk.Misc) -> None:
    """销毁子控件（用于刷新列表/卡片内容）。"""
    for child in list(widget.winfo_children()):
        child.destroy()


def make_scrollable(parent: tk.Misc, theme: Theme) -> tuple[tk.Canvas, tk.Frame]:
    """创建可滚动区域；返回 (canvas, inner_frame)，inner_frame 里放内容。"""
    container = tk.Frame(parent, bg=theme.color("bg"))
    container.pack(fill="both", expand=True)
    canvas = tk.Canvas(container, bg=theme.color("bg"), highlightthickness=0, bd=0)
    scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=theme.color("bg"))
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event) -> None:
        canvas.itemconfigure(window_id, width=event.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)

    def _on_mousewheel(event) -> None:
        delta = -1 * (event.delta // 120) if event.delta else 0
        canvas.yview_scroll(delta, "units")

    def _bind(_event=None) -> None:
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _unbind(_event=None) -> None:
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _bind)
    canvas.bind("<Leave>", _unbind)
    return canvas, inner


def select_first(listbox: tk.Listbox) -> None:
    if listbox.size():
        listbox.selection_clear(0, "end")
        listbox.selection_set(0)
        listbox.activate(0)


def fill_listbox(listbox: tk.Listbox, values: Iterable[str]) -> None:
    listbox.delete(0, "end")
    for value in values:
        listbox.insert("end", value)
