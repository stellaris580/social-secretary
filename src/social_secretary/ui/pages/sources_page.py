"""消息中心（原消息来源页）：筛选、查看来源、手动维护「处理状态」。

处理状态只写本机数据库，不改变 QQ/邮箱里的原始状态，也不会触发任何采集/发送。
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk

from ...contracts import AccountKind, Category, Message, MessageWorkStatus
from .. import notify
from ..dialogs import SourceDetailDialog
from ..formatting import category_label, format_datetime, sender_label, shorten
from ..widgets import Card, big_button, hint, label

__all__ = ["SourcesPage"]

_CATEGORY_CHOICES = (
    ("全部分类", ""),
    ("重要", Category.IMPORTANT.value),
    ("普通", Category.NORMAL.value),
    ("仅供了解", Category.FYI.value),
)

_SOURCE_CHOICES = (
    ("全部来源", ""),
    ("QQ", "qq"),
    ("邮箱", "mail"),
)

_WORK_STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    ("全部处理状态", ""),
    ("未处理", MessageWorkStatus.UNHANDLED.value),
    ("待回复", MessageWorkStatus.NEEDS_REPLY.value),
    ("已处理", MessageWorkStatus.DONE.value),
    ("无需回复", MessageWorkStatus.NO_REPLY.value),
)

_WORK_STATUS_LABELS = {
    MessageWorkStatus.UNHANDLED.value: "未处理",
    MessageWorkStatus.NEEDS_REPLY.value: "待回复",
    MessageWorkStatus.DONE.value: "已处理",
    MessageWorkStatus.NO_REPLY.value: "无需回复",
}

_DATE_CHOICES = (
    ("全部日期", "all"),
    ("今天", "today"),
    ("近 7 天", "7d"),
)

_IMPORTANCE_BUTTONS = (
    ("重要", Category.IMPORTANT.value),
    ("普通", Category.NORMAL.value),
    ("仅供了解", Category.FYI.value),
)


class SourcesPage(tk.Frame):
    """消息中心页面（来源核对 + 处理状态）。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self._messages: list[Message] = []
        self._accounts: list[tuple[str, str]] = []
        self._conversations = []
        self._conversation_map: dict[str, object] = {}
        self._account_kind_map: dict[str, AccountKind] = {}
        self._account_ids: list[str] = [""]
        self._conversation_ids: list[str] = [""]
        self._selected_message: Message | None = None
        self._row_messages: list[Message] = []
        self._row_groups: list[list[Message]] = []
        self.date_filter_var = tk.StringVar(value="all")
        self.merge_context_var = tk.BooleanVar(value=False)
        self.last_detail_dialog: SourceDetailDialog | None = None

        header = tk.Frame(self, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 6))
        label(header, theme, "消息中心", kind="h1", bg=theme.color("bg")).pack(side="left")
        hint(
            header,
            theme,
            "核对来源、筛选处理状态；状态只改本机记录，不影响 QQ/邮箱原始状态。",
            bg=theme.color("bg"),
        ).pack(side="right")

        filter_card = Card(self, theme, "筛选")
        filter_card.pack(fill="x", padx=18, pady=(0, 8))
        filter_row = tk.Frame(filter_card.body, bg=theme.color("card"))
        filter_row.pack(fill="x")

        hint(filter_row, theme, "账号：", bg=theme.color("card")).pack(side="left")
        self.account_combo = ttk.Combobox(filter_row, state="readonly", width=18, font=theme.font("base"))
        self.account_combo.pack(side="left", padx=(0, 8))
        self.account_combo.bind("<<ComboboxSelected>>", self._on_account_changed)

        hint(filter_row, theme, "会话：", bg=theme.color("card")).pack(side="left")
        self.conversation_combo = ttk.Combobox(
            filter_row, state="readonly", width=26, font=theme.font("base")
        )
        self.conversation_combo.pack(side="left", padx=(0, 8))
        self.conversation_combo.bind("<<ComboboxSelected>>", self._reload_messages)

        hint(filter_row, theme, "重要程度：", bg=theme.color("card")).pack(side="left")
        self.category_combo = ttk.Combobox(
            filter_row,
            values=[name for name, _value in _CATEGORY_CHOICES],
            state="readonly",
            width=10,
            font=theme.font("base"),
        )
        self.category_combo.current(0)
        self.category_combo.pack(side="left", padx=(0, 8))
        self.category_combo.bind("<<ComboboxSelected>>", self._reload_messages)

        refresh_button = big_button(filter_row, theme, "刷新", self.refresh)
        refresh_button.pack(side="right")

        second_row = tk.Frame(filter_card.body, bg=theme.color("card"))
        second_row.pack(fill="x", pady=(8, 0))
        hint(second_row, theme, "日期：", bg=theme.color("card")).pack(side="left")
        self.date_combo = ttk.Combobox(
            second_row,
            values=[name for name, _value in _DATE_CHOICES],
            state="readonly",
            width=9,
            font=theme.font("base"),
        )
        self.date_combo.current(0)
        self.date_combo.pack(side="left", padx=(0, 8))
        self.date_combo.bind("<<ComboboxSelected>>", self._on_date_filter_changed)

        hint(second_row, theme, "来源：", bg=theme.color("card")).pack(side="left")
        self.source_combo = ttk.Combobox(
            second_row,
            values=[name for name, _value in _SOURCE_CHOICES],
            state="readonly",
            width=10,
            font=theme.font("base"),
        )
        self.source_combo.current(0)
        self.source_combo.pack(side="left", padx=(0, 8))
        self.source_combo.bind("<<ComboboxSelected>>", self._reload_messages)

        hint(second_row, theme, "处理状态：", bg=theme.color("card")).pack(side="left")
        self.status_filter_var = tk.StringVar(value="")
        self.status_combo = ttk.Combobox(
            second_row,
            values=[name for name, _value in _WORK_STATUS_CHOICES],
            state="readonly",
            width=12,
            font=theme.font("base"),
        )
        self.status_combo.current(0)
        self.status_combo.pack(side="left", padx=(0, 10))
        self.status_combo.bind("<<ComboboxSelected>>", self._on_status_filter_changed)

        tk.Checkbutton(
            second_row,
            text="按会话合并",
            variable=self.merge_context_var,
            command=self._reload_messages,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
        ).pack(side="left", padx=(4, 0))
        hint(
            second_row,
            theme,
            "（默认关；开启后相邻同会话消息合并显示，不改变后端数据）",
            bg=theme.color("card"),
        ).pack(side="left", padx=(4, 0))

        self.work_status_counts_label = hint(filter_card.body, theme, "", bg=theme.color("card"))
        self.work_status_counts_label.pack(fill="x", pady=(8, 0))
        self.count_label = hint(filter_card.body, theme, "", bg=theme.color("card"))
        self.count_label.pack(fill="x", pady=(2, 0))

        table_card = Card(self, theme, "消息列表")
        table_card.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        columns = ("flag", "source", "time", "conversation", "sender", "category", "work", "summary")
        self.tree = ttk.Treeview(
            table_card.body,
            columns=columns,
            show="headings",
            height=11,
            selectmode="browse",
        )
        headings = {
            "flag": ("标记", 60),
            "source": ("来源", 60),
            "time": ("时间", 115),
            "conversation": ("会话/联系人", 150),
            "sender": ("发送者", 105),
            "category": ("重要程度", 80),
            "work": ("处理状态", 90),
            "summary": ("内容摘要", 300),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "summary"))
        self.tree.tag_configure("demo", foreground=theme.color("demo"))
        self.tree.tag_configure("real", foreground=theme.color("text"))
        scrollbar = ttk.Scrollbar(table_card.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_tree_select())
        self.tree.bind("<Double-1>", lambda _event: self.open_detail())

        action_row = tk.Frame(table_card.body, bg=theme.color("card"))
        action_row.pack(fill="x", pady=(8, 0))
        self.detail_button = big_button(action_row, theme, "查看来源详情", self.open_detail)
        self.detail_button.configure(state="disabled")
        self.detail_button.pack(side="left")
        hint(action_row, theme, "双击某一行也可以查看来源详情。", bg=theme.color("card")).pack(
            side="left", padx=(10, 0)
        )

        status_card = Card(self, theme, "处理状态（只改本机记录，不影响 QQ/邮箱原始状态）")
        status_card.pack(fill="x", padx=18, pady=(0, 8))
        self.selected_message_label = hint(
            status_card.body, theme, "先在上方选中一条消息，再标记处理状态。", wrap=900, bg=theme.color("card")
        )
        self.selected_message_label.pack(fill="x")
        button_row = tk.Frame(status_card.body, bg=theme.color("card"))
        button_row.pack(fill="x", pady=(8, 0))
        self.status_buttons: dict[str, tk.Button] = {}
        for text, value in (
            ("未处理", MessageWorkStatus.UNHANDLED.value),
            ("待回复", MessageWorkStatus.NEEDS_REPLY.value),
            ("已处理", MessageWorkStatus.DONE.value),
            ("无需回复", MessageWorkStatus.NO_REPLY.value),
        ):
            button = big_button(button_row, theme, text, lambda v=value: self.mark_status(v))
            button.configure(state="disabled")
            button.pack(side="left", padx=(0, 8))
            self.status_buttons[value] = button

        importance_row = tk.Frame(status_card.body, bg=theme.color("card"))
        importance_row.pack(fill="x", pady=(8, 0))
        hint(importance_row, theme, "手动重要程度：", bg=theme.color("card")).pack(side="left")
        self.importance_buttons: dict[str, tk.Button] = {}
        for text, value in _IMPORTANCE_BUTTONS:
            button = big_button(importance_row, theme, text, lambda v=value: self.mark_category(v))
            button.configure(state="disabled")
            button.pack(side="left", padx=(0, 8))
            self.importance_buttons[value] = button
        hint(
            importance_row,
            theme,
            "只改本机分类，用于纠正机器判断；不影响 QQ/邮箱原始数据。",
            bg=theme.color("card"),
        ).pack(side="left", padx=(4, 0))

        self.work_status_feedback_label = hint(
            status_card.body,
            theme,
            "标记只保存在本机；程序不会替你回复、不会改变 QQ/邮箱里的消息。",
            wrap=900,
            bg=theme.color("card"),
        )
        self.work_status_feedback_label.pack(fill="x", pady=(8, 0))

        hint(
            self,
            theme,
            "来源详情只显示账号类型、会话、时间来源和定位信息，不包含密码或授权码。",
            bg=theme.color("bg"),
        ).pack(fill="x", padx=18, pady=(0, 10))

        self.refresh()

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            accounts = self.service.list_accounts()
            self._conversations = self.service.list_conversations()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "读取消息中心失败",
                f"暂时读不到账号与会话列表：{exc}\n\n可以点「刷新」重试；如果一直失败，请导出诊断信息。",
            )
            return

        self._accounts = [(account.id, account.display_name) for account in accounts]
        self._account_kind_map = {account.id: account.kind for account in accounts}
        self._account_ids = [""] + [account_id for account_id, _name in self._accounts]
        self._conversation_map = {conversation.id: conversation for conversation in self._conversations}

        current_account = self._current_account_id()
        self.account_combo.configure(values=["全部账号"] + [name for _id, name in self._accounts])
        index = self._account_ids.index(current_account) if current_account in self._account_ids else 0
        self.account_combo.current(index)
        self._reload_conversations()
        self._reload_counts()
        self._reload_messages()

    def _current_account_id(self) -> str:
        index = self.account_combo.current()
        if 0 <= index < len(self._account_ids):
            return self._account_ids[index]
        return ""

    def _current_conversation_id(self) -> str:
        index = self.conversation_combo.current()
        if 0 <= index < len(self._conversation_ids):
            return self._conversation_ids[index]
        return ""

    def _current_category(self) -> Category | None:
        index = self.category_combo.current()
        if index <= 0 or index >= len(_CATEGORY_CHOICES):
            return None
        value = _CATEGORY_CHOICES[index][1]
        return Category(value) if value else None

    def _current_source(self) -> str:
        index = self.source_combo.current()
        if 0 <= index < len(_SOURCE_CHOICES):
            return _SOURCE_CHOICES[index][1]
        return ""

    def _current_work_status(self) -> MessageWorkStatus | None:
        value = self.status_filter_var.get()
        if not value:
            index = self.status_combo.current()
            if 0 <= index < len(_WORK_STATUS_CHOICES):
                value = _WORK_STATUS_CHOICES[index][1]
        if not value:
            return None
        try:
            return MessageWorkStatus(value)
        except ValueError:
            return None

    def set_status_filter(self, value: str) -> bool:
        """设置处理状态筛选（空字符串=全部）；只重查列表，不触发任何采集。"""
        normalized = str(value or "")
        valid_values = {item_value for _name, item_value in _WORK_STATUS_CHOICES}
        if normalized not in valid_values:
            return False
        self.status_filter_var.set(normalized)
        for index, (_name, item_value) in enumerate(_WORK_STATUS_CHOICES):
            if item_value == normalized:
                self.status_combo.current(index)
                break
        self._reload_messages()
        return True

    def _on_status_filter_changed(self, _event=None) -> None:
        index = self.status_combo.current()
        value = _WORK_STATUS_CHOICES[index][1] if 0 <= index < len(_WORK_STATUS_CHOICES) else ""
        self.status_filter_var.set(value)
        self._reload_messages()

    def _on_account_changed(self, _event=None) -> None:
        self._reload_conversations()
        self._reload_messages()

    def _reload_conversations(self) -> None:
        account_id = self._current_account_id()
        self._conversation_ids = [""]
        titles = ["全部会话"]
        for conversation in self._conversations:
            if account_id and conversation.account_id != account_id:
                continue
            self._conversation_ids.append(conversation.id)
            titles.append(conversation.title)
        self.conversation_combo.configure(values=titles)
        self.conversation_combo.current(0)

    def _current_date_filter(self) -> str:
        index = self.date_combo.current()
        if 0 <= index < len(_DATE_CHOICES):
            return _DATE_CHOICES[index][1]
        return "all"

    def _on_date_filter_changed(self, _event=None) -> None:
        index = self.date_combo.current()
        if 0 <= index < len(_DATE_CHOICES):
            self.date_filter_var.set(_DATE_CHOICES[index][1])
        self._reload_messages()

    def _message_matches_date(self, message: Message) -> bool:
        mode = self._current_date_filter()
        if mode == "all":
            return True
        moment = message.timestamp or message.collected_at
        if moment is None:
            return False
        moment_date = moment.astimezone().date()
        today = datetime.now().astimezone().date()
        if mode == "today":
            return moment_date == today
        if mode == "7d":
            return today - timedelta(days=7) <= moment_date <= today
        return True

    def _reload_messages(self, _event=None) -> None:
        category = self._current_category()
        work_status = self._current_work_status()
        try:
            try:
                messages = self.service.list_messages(
                    day=None,
                    category=category,
                    limit=300,
                    work_status=work_status,
                )
            except TypeError:
                # 兼容旧核心：本地过滤处理状态。
                messages = self.service.list_messages(day=None, category=category, limit=300)
                if work_status is not None:
                    messages = [
                        message
                        for message in messages
                        if MessageWorkStatus(getattr(message, "work_status", MessageWorkStatus.UNHANDLED))
                        == work_status
                    ]
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "读取消息失败",
                f"暂时读不到消息列表：{exc}\n\n可以点「刷新」重试。",
            )
            return

        account_id = self._current_account_id()
        conversation_id = self._current_conversation_id()
        source = self._current_source()
        filtered: list[Message] = []
        for message in messages:
            conversation = self._conversation_map.get(message.conversation_id)
            if account_id and conversation is not None and conversation.account_id != account_id:
                continue
            if conversation_id and message.conversation_id != conversation_id:
                continue
            if source:
                kind = self._account_kind_map.get(message.account_id)
                is_mail = kind == AccountKind.EMAIL
                if (source == "mail") != is_mail:
                    continue
            if not self._message_matches_date(message):
                continue
            filtered.append(message)
        self._messages = filtered
        self._fill_tree(filtered)
        self._reload_counts()

    def _fill_tree(self, messages: list[Message]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        # 按会话合并（默认关）：只影响显示，不改后端数据。
        if self.merge_context_var.get():
            groups: list[list[Message]] = []
            for message in messages:
                if groups and groups[-1][0].conversation_id == message.conversation_id:
                    groups[-1].append(message)
                else:
                    groups.append([message])
        else:
            groups = [[message] for message in messages]
        self._row_groups = groups
        self._row_messages = [group[0] for group in groups]
        for index, group in enumerate(groups):
            message = group[0]
            source_label = "邮箱" if self._account_kind_map.get(message.account_id) == AccountKind.EMAIL else "QQ"
            work_status = MessageWorkStatus(
                getattr(message, "work_status", MessageWorkStatus.UNHANDLED)
            )
            summary = shorten(message.text, 60)
            if len(group) > 1:
                summary = f"[合并 {len(group)} 条] 最近：{summary}"
            demo_flags = {item.is_demo for item in group}
            flag = "演示" if demo_flags == {True} else ("真实" if demo_flags == {False} else "混合")
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                tags=("demo" if flag == "演示" else "real",),
                values=(
                    flag,
                    source_label,
                    format_datetime(message.timestamp),
                    message.conversation_title,
                    f"{len(group)} 条" if len(group) > 1 else sender_label(message.sender, message.is_self),
                    category_label(message.category),
                    _WORK_STATUS_LABELS.get(work_status.value, str(work_status.value)),
                    summary,
                ),
            )
        demo_count = sum(1 for message in messages if message.is_demo)
        suffix = f"（其中演示 {demo_count} 条）" if demo_count else ""
        if messages:
            merged = "，按会话合并显示" if self.merge_context_var.get() and len(groups) != len(messages) else ""
            self.count_label.configure(
                text=f"共 {len(messages)} 条消息{suffix}{merged}。选中一条可查看来源或标记处理状态。"
            )
        else:
            self.count_label.configure(text="当前筛选条件下没有消息。可以调整筛选或先到「设置与隐私」读取消息。")
        self._update_detail_button()
        self._selected_message = None
        self._set_status_buttons_enabled(False)
        self.selected_message_label.configure(text="先在上方选中一条消息，再标记处理状态。")

    def _reload_counts(self) -> None:
        try:
            counts = self.service.message_work_status_counts()
        except Exception:  # noqa: BLE001
            counts = {}
        if not isinstance(counts, dict):
            counts = {}
        if not counts:
            return
        parts = []
        for value, label_text in (
            (MessageWorkStatus.UNHANDLED.value, "未处理"),
            (MessageWorkStatus.NEEDS_REPLY.value, "待回复"),
            (MessageWorkStatus.DONE.value, "已处理"),
            (MessageWorkStatus.NO_REPLY.value, "无需回复"),
        ):
            parts.append(f"{label_text} {int(counts.get(value, 0) or 0)}")
        important = counts.get("important_unhandled")
        suffix = f"；重要且未处理/待回复：{int(important)} 条" if isinstance(important, int) else ""
        self.work_status_counts_label.configure(text="处理状态：" + " · ".join(parts) + suffix)

    # ------------------------------------------------------------------
    # 选中 / 处理状态
    # ------------------------------------------------------------------
    def _on_tree_select(self) -> None:
        self._update_detail_button()
        selection = self.tree.selection()
        if not selection:
            self._selected_message = None
            self._set_status_buttons_enabled(False)
            return
        try:
            index = int(selection[0])
        except (TypeError, ValueError):
            index = self.tree.index(selection[0])
        if not (0 <= index < len(self._row_messages)):
            return
        message = self._row_messages[index]
        self._selected_message = message
        group_size = 1
        if 0 <= index < len(self._row_groups):
            group_size = len(self._row_groups[index])
        work_status = MessageWorkStatus(getattr(message, "work_status", MessageWorkStatus.UNHANDLED))
        merged_note = f" ｜ 合并上下文 {group_size} 条" if group_size > 1 else ""
        self.selected_message_label.configure(
            text=(
                f"已选中：{message.conversation_title} ｜ {sender_label(message.sender, message.is_self)}"
                f" ｜ 当前状态：{_WORK_STATUS_LABELS.get(work_status.value, work_status.value)}"
                f" ｜ 重要程度：{category_label(message.category)}"
                f"（{'演示' if message.is_demo else '真实'}）{merged_note}"
            )
        )
        self._set_status_buttons_enabled(True)

    def _set_status_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.status_buttons.values():
            button.configure(state=state)
        for button in self.importance_buttons.values():
            button.configure(state=state)

    def mark_status(self, value: str) -> dict | None:
        """把当前选中消息标记为指定处理状态（只改本机记录）。"""
        message = self._selected_message
        if message is None:
            notify.show_info(
                self.window.root, "还没有选中消息", "请先在上方列表选中一条消息，再标记处理状态。"
            )
            return None
        try:
            status = MessageWorkStatus(value)
        except ValueError:
            notify.show_error(self.window.root, "标记失败", f"未知的处理状态：{value}")
            return None
        try:
            self.service.set_message_work_status(message.id, status)
        except Exception as exc:  # noqa: BLE001
            self.work_status_feedback_label.configure(
                text=f"标记失败：{exc}。可以稍后重试；如果一直失败，请导出诊断信息。",
                fg=self.theme.color("danger"),
            )
            notify.show_error(
                self.window.root,
                "标记处理状态失败",
                f"这次标记失败：{exc}\n\n可以稍后重试；如果一直失败，请导出诊断信息。",
            )
            return None
        label_text = _WORK_STATUS_LABELS.get(status.value, status.value)
        self.work_status_feedback_label.configure(
            text=f"已标记为「{label_text}」（仅本机记录；不会改变 QQ/邮箱原始状态）。",
            fg=self.theme.color("muted"),
        )
        self._reload_messages()
        for index, item in enumerate(self._row_messages):
            if item.id == message.id:
                self.tree.selection_set(str(index))
                self.tree.see(str(index))
                self._on_tree_select()
                break
        result = {
            "ok": True,
            "message_id": message.id,
            "work_status": status.value,
        }
        return result

    def mark_category(self, value: str) -> dict | None:
        """把当前选中消息手动标记为指定重要程度（只改本机分类）。"""
        message = self._selected_message
        if message is None:
            notify.show_info(
                self.window.root, "还没有选中消息", "请先在上方列表选中一条消息，再标记重要程度。"
            )
            return None
        try:
            category = Category(value)
        except ValueError:
            notify.show_error(self.window.root, "标记失败", f"未知的重要程度：{value}")
            return None
        try:
            self.service.set_message_category(message.id, category)
        except Exception as exc:  # noqa: BLE001
            self.work_status_feedback_label.configure(
                text=f"标记重要程度失败：{exc}。可以稍后重试；如果一直失败，请导出诊断信息。",
                fg=self.theme.color("danger"),
            )
            notify.show_error(
                self.window.root,
                "标记重要程度失败",
                f"这次标记失败：{exc}\n\n可以稍后重试；如果一直失败，请导出诊断信息。",
            )
            return None
        label_text = category_label(category)
        self.work_status_feedback_label.configure(
            text=f"已把重要程度改为「{label_text}」（仅本机分类；不会改变 QQ/邮箱原始数据）。",
            fg=self.theme.color("muted"),
        )
        self._reload_messages()
        for index, item in enumerate(self._row_messages):
            if item.id == message.id:
                self.tree.selection_set(str(index))
                self.tree.see(str(index))
                self._on_tree_select()
                break
        try:
            self.window.pages["summary"].refresh()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "message_id": message.id, "category": category.value}

    def _update_detail_button(self) -> None:
        has_selection = bool(self.tree.selection())
        self.detail_button.configure(state="normal" if has_selection else "disabled")

    # ------------------------------------------------------------------
    # 来源详情
    # ------------------------------------------------------------------
    def selected_message_id(self) -> str:
        selection = self.tree.selection()
        if not selection:
            return ""
        try:
            index = int(selection[0])
        except (TypeError, ValueError):
            return ""
        if 0 <= index < len(self._row_messages):
            return self._row_messages[index].id
        return ""

    def open_detail(self, message_id: str | None = None) -> SourceDetailDialog | None:
        target = message_id or self.selected_message_id()
        if not target:
            notify.show_info(self.window.root, "还没有选中消息", "请先在列表里选一条消息，再点「查看来源详情」。")
            return None
        try:
            detail = self.service.get_source_detail(target)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "读取来源详情失败",
                f"这条消息的来源暂时读不到：{exc}\n\n可以换一条再试；如果一直失败，请导出诊断信息。",
            )
            return None
        dialog = SourceDetailDialog(self.window, detail)
        self.last_detail_dialog = dialog
        dialog.show()
        return dialog

    # ------------------------------------------------------------------
    def row_count(self) -> int:
        return len(self.tree.get_children())

    def message_ids(self) -> list[str]:
        return [message.id for message in self._messages]
