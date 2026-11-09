"""待办页：从消息里提取的待办，可标记完成 / 忽略（只改本机记录）。"""

from __future__ import annotations

import tkinter as tk

from ...contracts import TodoItem, TodoStatus
from .. import notify
from ..formatting import format_due, shorten
from ..widgets import Card, big_button, clear_children, hint, label

__all__ = ["TodoPage"]


class TodoPage(tk.Frame):
    """待办列表页。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self.rows: list[dict] = []

        header = tk.Frame(self, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 6))
        label(header, theme, "待办", kind="h1", bg=theme.color("bg")).pack(side="left")
        self.show_done_var = tk.IntVar(value=0)
        self.show_done_check = tk.Checkbutton(
            header,
            text="显示已完成 / 已忽略",
            variable=self.show_done_var,
            command=self.refresh,
            font=theme.font("base"),
            bg=theme.color("bg"),
            activebackground=theme.color("bg"),
            cursor="hand2",
        )
        self.show_done_check.pack(side="right")

        self.summary_label = hint(self, theme, "", bg=theme.color("bg"))
        self.summary_label.pack(fill="x", padx=18)

        card = Card(self, theme, "今天要处理的事")
        card.pack(fill="both", expand=True, padx=18, pady=(8, 10))
        self.rows_frame = tk.Frame(card.body, bg=theme.color("card"))
        self.rows_frame.pack(fill="both", expand=True)
        self.empty_label = tk.Label(
            self.rows_frame,
            text="今天还没有识别到待办。\n生成今日摘要后，像「周五前把方案发我」这样的消息会被整理到这里。",
            font=theme.font("base"),
            fg=theme.color("muted"),
            bg=theme.color("card"),
            justify="center",
            pady=30,
        )
        hint(
            card.body,
            theme,
            "「完成」和「忽略」只改变本机记录，不会通知对方，也不会修改 QQ 或邮箱。",
            bg=theme.color("card"),
        ).pack(fill="x", pady=(8, 0))

        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            todos = self.service.list_todos(include_done=self.show_done_var.get() == 1)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "读取待办失败",
                f"暂时读不到待办列表：{exc}\n\n可以点左侧其它页面再回来重试；如果一直失败，请导出诊断信息。",
            )
            return
        self._render(todos)

    def _render(self, todos: list[TodoItem]) -> None:
        clear_children(self.rows_frame)
        self.rows = []
        if not todos:
            self.empty_label = tk.Label(
                self.rows_frame,
                text="今天还没有识别到待办。\n生成今日摘要后，像「周五前把方案发我」这样的消息会被整理到这里。",
                font=self.theme.font("base"),
                fg=self.theme.color("muted"),
                bg=self.theme.color("card"),
                justify="center",
                pady=30,
            )
            self.empty_label.pack(fill="x")
            self.summary_label.configure(text="当前没有待办。")
            return

        for index, todo in enumerate(todos):
            row = self._build_row(todo)
            row.pack(fill="x", pady=(0, 4))
            if index < len(todos) - 1:
                tk.Frame(self.rows_frame, bg=self.theme.color("border"), height=1).pack(fill="x", pady=(0, 4))

        open_count = sum(1 for todo in todos if todo.status == TodoStatus.OPEN)
        self.summary_label.configure(text=f"共 {len(todos)} 条待办，其中 {open_count} 条待处理。按截止时间先后排列。")

    def _build_row(self, todo: TodoItem) -> tk.Frame:
        theme = self.theme
        row = tk.Frame(self.rows_frame, bg=theme.color("card"))
        text_col = tk.Frame(row, bg=theme.color("card"))
        text_col.pack(side="left", fill="x", expand=True, padx=4, pady=6)
        tk.Label(
            text_col,
            text=shorten(todo.text, 120),
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("card"),
            anchor="w",
            justify="left",
            wraplength=720,
        ).pack(fill="x", anchor="w")

        due = format_due(todo.due_at, todo.due_text)
        meta_parts = [f"截止：{due}" if (todo.due_at or todo.due_text) else due]
        meta_parts.append(f"来自：{todo.conversation_title}")
        if todo.confidence < 0.6:
            meta_parts.append("机器提取，请核对")
        hint(text_col, theme, "　·　".join(meta_parts), wrap=720, bg=theme.color("card")).pack(
            fill="x", anchor="w", pady=(3, 0)
        )
        if todo.status != TodoStatus.OPEN:
            state = "已完成" if todo.status == TodoStatus.DONE else "已忽略"
            hint(text_col, theme, state, bg=theme.color("card")).pack(fill="x", anchor="w", pady=(2, 0))

        button_col = tk.Frame(row, bg=theme.color("card"))
        button_col.pack(side="right", padx=4)
        done_button = None
        dismiss_button = None
        if todo.status == TodoStatus.OPEN:
            done_button = big_button(
                button_col, theme, "完成", lambda todo_id=todo.id: self._update_status(todo_id, TodoStatus.DONE)
            )
            done_button.pack(side="left", padx=(0, 6))
            dismiss_button = big_button(
                button_col,
                theme,
                "忽略",
                lambda todo_id=todo.id: self._update_status(todo_id, TodoStatus.DISMISSED),
            )
            dismiss_button.pack(side="left")

        self.rows.append(
            {
                "todo": todo,
                "frame": row,
                "done_button": done_button,
                "dismiss_button": dismiss_button,
            }
        )
        return row

    def _update_status(self, todo_id: str, status: TodoStatus) -> None:
        try:
            self.service.update_todo_status(todo_id, status)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "更新待办失败",
                f"这次修改保存失败：{exc}\n\n可以重试一次；如果一直失败，请导出诊断信息。",
            )
            return
        self.refresh()
        self.window.refresh_status()

    def todo_ids(self) -> list[str]:
        return [row["todo"].id for row in self.rows]


__all__ = ["TodoPage"]
