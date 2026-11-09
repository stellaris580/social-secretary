"""今日摘要页：分类计数卡片 + 消息列表 + 叙述与注意事项。"""

from __future__ import annotations

import tkinter as tk

from ...contracts import Category, DailySummary, Message, ScanStatus
from .. import notify
from ..dialogs import SemanticSummaryDialog
from ..formatting import (
    category_hint,
    category_label,
    format_date,
    format_datetime,
    scan_status_label,
    sender_label,
    shorten,
)
from ..widgets import Banner, Card, clear_children, hint, label, make_scrollable

CATEGORY_ORDER = (Category.IMPORTANT, Category.NORMAL, Category.FYI)


class CategoryCard(tk.Frame):
    """单个分类卡片：标题 + 条数 + 消息列表。"""

    def __init__(self, parent: tk.Misc, window, category: Category) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.category = category
        self.messages: list[Message] = []
        self.card = Card(self, theme, category_label(category), padding=12)
        self.card.pack(fill="both", expand=True)

        count_row = tk.Frame(self.card.body, bg=theme.color("card"))
        count_row.pack(fill="x", pady=(0, 4))
        self.count_label = label(count_row, theme, "0 条", kind="count", bg=theme.color("card"))
        self.count_label.pack(side="left")
        hint(count_row, theme, category_hint(category), wrap=220, bg=theme.color("card")).pack(
            side="left", padx=(8, 0)
        )

        self.listbox = tk.Listbox(
            self.card.body,
            height=9,
            font=theme.font("small"),
            bg="#FFFFFF",
            fg=theme.color("text"),
            selectbackground=theme.color("primary_soft"),
            selectforeground=theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
            activestyle="none",
            exportselection=False,
        )
        self.listbox.pack(fill="both", expand=True, pady=(4, 0))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.empty_label = hint(
            self.card.body,
            theme,
            "今天这一类没有需要显示的消息",
            bg=theme.color("card"),
        )

    # -- 数据 ---------------------------------------------------------
    def set_messages(self, messages: tuple[Message, ...] | list[Message]) -> None:
        self.messages = list(messages)
        self.listbox.delete(0, "end")
        self.count_label.configure(text=f"{len(self.messages)} 条")
        if not self.messages:
            self.listbox.pack_forget()
            self.empty_label.pack(fill="x", pady=6)
            return
        self.empty_label.pack_forget()
        self.listbox.pack(fill="both", expand=True, pady=(4, 0))
        for message in self.messages:
            self.listbox.insert("end", self._item_text(message))

    def _item_text(self, message: Message) -> str:
        when = format_datetime(message.timestamp, empty="时间未知")
        who = sender_label(message.sender, message.is_self)
        return f"{when} ｜ {who}：{shorten(message.text, 24)}"

    def _on_select(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(self.messages):
            self.window.open_message_detail(self.messages[index].id)

    def select(self, index: int) -> None:
        """选中一条（供测试/键盘导航使用）。"""
        if 0 <= index < len(self.messages):
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(index)
            self.listbox.activate(index)


class SummaryPage(tk.Frame):
    """今日摘要页面。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self.cards: dict[Category, CategoryCard] = {}
        self._summary: DailySummary | None = None

        outer = tk.Frame(self, bg=theme.color("bg"))
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 6))
        label(header, theme, "今日摘要", kind="h1", bg=theme.color("bg")).pack(side="left")
        self.date_label = hint(header, theme, "", bg=theme.color("bg"))
        self.date_label.pack(side="left", padx=(10, 0))
        self.demo_badge = tk.Label(
            header,
            text="演示模式（仅合成数据）",
            font=theme.font("small"),
            fg=theme.color("demo"),
            bg=theme.color("demo_soft"),
            padx=10,
            pady=3,
        )
        self.real_badge = tk.Label(
            header,
            text="真实数据（本机只读）",
            font=theme.font("small"),
            fg=theme.color("success"),
            bg=theme.color("success_soft"),
            padx=10,
            pady=3,
        )

        self._canvas, content = make_scrollable(outer, theme)
        self.content = content

        self.error_banner = Banner(content, theme, "", wrap=900)
        self.scan_banner = Banner(content, theme, "", wrap=900)
        self.fallback_banner = Banner(content, theme, "", wrap=900, kind="warning")
        # P0：一键整理的详细结果（原因/解决方法/邮箱新增/历史），放在 narrative 上方，
        # 滚动前就能看到关键原因。
        self.organize_banner = Banner(content, theme, "", wrap=900, kind="warning")
        self._last_organize_result: dict | None = None
        self._last_organize_summary: DailySummary | None = None

        ai_card = Card(content, theme, "AI 摘要（实验，语义）")
        ai_card.pack(fill="x", pady=(0, 10))
        hint(
            ai_card.body,
            theme,
            "默认使用本地规则，不联网；外部 AI 需你在设置中单独同意，可能上传选中的消息内容并产生费用。"
            "AI 摘要只读、不会发送任何消息。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x")
        ai_button_row = tk.Frame(ai_card.body, bg=theme.color("card"))
        ai_button_row.pack(fill="x", pady=(8, 0))
        from ..widgets import big_button as _big_button

        self.ai_conversation_summary_button = _big_button(
            ai_button_row,
            theme,
            "AI 会话摘要",
            lambda: self.open_conversation_semantic(),
        )
        self.ai_conversation_summary_button.pack(side="left")
        self.ai_daily_brief_button = _big_button(
            ai_button_row,
            theme,
            "AI 每日简报",
            lambda: self.open_daily_brief(),
            kind="primary",
        )
        self.ai_daily_brief_button.pack(side="left", padx=(8, 0))
        self.ai_status_label = hint(ai_card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.ai_status_label.pack(fill="x", pady=(8, 0))
        self.last_ai_dialog: SemanticSummaryDialog | None = None
        self._last_ai_conversation_id: str | None = None

        narrative_card = Card(content, theme, "今天的情况")
        narrative_card.pack(fill="x", pady=(0, 10))
        self.narrative_card = narrative_card
        self.narrative_label = tk.Label(
            narrative_card.body,
            text="",
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("card"),
            justify="left",
            anchor="w",
            wraplength=900,
        )
        self.narrative_label.pack(fill="x")
        self.todo_hint_label = hint(narrative_card.body, theme, "", bg=theme.color("card"))
        self.todo_hint_label.pack(fill="x", pady=(6, 0))
        self.caveats_frame = tk.Frame(narrative_card.body, bg=theme.color("card"))
        self.caveats_frame.pack(fill="x", pady=(6, 0))

        cards_row = tk.Frame(content, bg=theme.color("bg"))
        cards_row.pack(fill="both", expand=True, pady=(0, 8))
        for column, category in enumerate(CATEGORY_ORDER):
            card = CategoryCard(cards_row, window, category)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            cards_row.grid_columnconfigure(column, weight=1, uniform="category")
            self.cards[category] = card

        hint(
            content,
            theme,
            "点任意一条消息可以查看完整内容与来源；程序只读，不会替你发送任何消息。",
            bg=theme.color("bg"),
        ).pack(fill="x", pady=(4, 14))

        self.show_summary(None)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            summary = self.service.get_cached_summary()
        except Exception as exc:  # noqa: BLE001 - 界面必须容错
            self.error_banner.set_text(f"读取今日摘要失败：{exc} 可以稍后重试，或到「设置与隐私」查看采集状态。")
            self.error_banner.pack(fill="x", pady=(0, 8), before=self.narrative_card)
            if self._last_organize_result is None:
                self.show_summary(None)
            else:
                self._render_organize_result(self._last_organize_result, self._last_organize_summary)
            return
        self.error_banner.pack_forget()
        # P0：刷新后保留最近一次一键整理的详细结果（原因/解决方法/历史）。
        if self._last_organize_result is not None:
            self._render_organize_result(
                self._last_organize_result, self._last_organize_summary or summary
            )
            return
        self.show_summary(summary)

    # ------------------------------------------------------------------
    # P0：一键整理结果直接展示（原因/解决方法/邮箱新增/摘要/历史）
    # ------------------------------------------------------------------
    def organize_feedback_text(self, result: dict | None = None) -> str:
        """返回一键整理结果的详细中文文案（供测试/展示复用）。"""
        result = dict(result or self._last_organize_result or {})
        return self._build_organize_feedback(result)[0]

    def show_organize_result(
        self, result: dict, *, summary: DailySummary | None = None
    ) -> None:
        self._last_organize_result = dict(result or {})
        result_summary = summary if summary is not None else result.get("summary")
        self._last_organize_summary = (
            result_summary if isinstance(result_summary, DailySummary) else None
        )
        self._render_organize_result(self._last_organize_result, self._last_organize_summary)

    def _build_organize_feedback(self, result: dict) -> tuple[str, bool]:
        configured = result.get("configured") or {}
        details = result.get("details") or []
        errors = result.get("errors") or []

        def detail_of(kind: str) -> list[dict]:
            return [
                item
                for item in details
                if isinstance(item, dict) and str(item.get("kind") or "") == kind
            ]

        bad = bool(errors)
        for item in details:
            if isinstance(item, dict) and str(item.get("status") or "").lower() in (
                "partial",
                "failed",
                "blocked",
            ):
                bad = True
        qq_failed = int(result.get("qq_sessions_failed") or result.get("qq_failed") or 0)
        mail_failed = int(result.get("mail_failed") or 0)
        if qq_failed or mail_failed:
            bad = True
        lines: list[str] = []
        if bad:
            lines.append("本次整理未完整完成，无法确认今天是否有新消息。")
        else:
            lines.append("本次整理完成。")
        if configured.get("qq"):
            qq_authorized = int(result.get("qq_authorized") or 0)
            qq_scanned = int(result.get("qq_sessions_scanned") or 0)
            qq_success = max(0, qq_scanned - qq_failed) if qq_scanned >= qq_failed else qq_scanned
            if qq_failed:
                lines.append(
                    f"QQ：授权 {qq_authorized} 个会话，成功 {qq_success} 个，失败 {qq_failed} 个。"
                )
                hint = str(result.get("qq_failure_hint") or "").strip()
                if not hint:
                    for item in detail_of("qq"):
                        hint = str(item.get("error") or "").strip()
                        if hint:
                            break
                if hint:
                    lines.append(f"原因：{hint}")
                lines.append(
                    "解决方法：先打开 QQ 主面板并保持可见（不要最小化到托盘/被遮挡），"
                    "再点「一键整理所有消息」重试。"
                )
            else:
                lines.append(
                    f"QQ：授权 {qq_authorized} 个会话，本次读取 {qq_scanned} 个，未发现失败。"
                )
            lines.append(
                "QQ 读取为实验性：消息身份无法完全确认，可能有重复，不能保证每条都是新消息。"
            )
        else:
            lines.append("QQ：还没有可用的授权会话。")
        if configured.get("mail"):
            mail_new = sum(
                int(item.get("messages_new") or 0) for item in detail_of("mail")
            )
            if mail_failed:
                lines.append("邮箱：同步失败，请检查网络或授权码后重试。")
            elif int(result.get("mail_success") or 0) > 0:
                lines.append(f"邮箱：同步成功，新增 {mail_new} 封。")
            else:
                lines.append("邮箱：本次没有可同步的邮箱账号。")
        else:
            lines.append("邮箱：还没有可用的授权邮箱。")
        if bad:
            lines.append("摘要：已生成，但因 QQ 未读取可能不完整。")
        history_total = int(result.get("history_total") or 0)
        latest_day = result.get("latest_message_day")
        if history_total > 0:
            latest_text = str(latest_day) if latest_day else "日期未知"
            lines.append(f"历史：本地已保存 {history_total} 条，最近一条 {latest_text}。")
        else:
            lines.append("历史：本地还没有消息。")
        return "\n".join(lines), bad

    def _render_organize_result(
        self, result: dict, summary: DailySummary | None
    ) -> None:
        if not result:
            return
        text, bad = self._build_organize_feedback(result)
        self.organize_banner.set_kind("warning" if bad else "info")
        self.organize_banner.set_text(text)
        self.organize_banner.pack(fill="x", pady=(0, 8), before=self.narrative_card)
        self.show_summary(summary)
        # 详细结果已给出原因：不要再显示通用“详见下方/列表可能不完整”提示。
        self.scan_banner.pack_forget()
        self.fallback_banner.pack_forget()
        self.organize_banner.pack(fill="x", pady=(0, 8), before=self.narrative_card)

        history_total = int(result.get("history_total") or 0)
        latest_day = str(result.get("latest_message_day") or "日期未知")
        total = 0
        if isinstance(summary, DailySummary):
            total = (
                len(summary.important)
                + len(summary.normal)
                + len(summary.fyi)
            )
        if bad:
            self.narrative_label.configure(
                text=(
                    "本次整理未完整完成，无法确认今天是否有新消息。"
                    "上方为失败原因和处理方法；历史消息仍然保存在本地。"
                )
            )
            self.todo_hint_label.configure(
                text="请先按上方方法处理后，再点「一键整理所有消息」重试。"
            )
        elif total == 0 and history_total > 0:
            day = summary.day.isoformat() if isinstance(summary, DailySummary) else "今天"
            self.narrative_label.configure(
                text=(
                    f"今天（{day}）没有新消息；本地已保存历史 {history_total} 条，"
                    f"最近一条 {latest_day}。"
                )
            )
            self.todo_hint_label.configure(
                text="这不是采集失败；历史消息仍可在「消息中心」查看。"
            )
        elif total == 0 and history_total == 0:
            self.narrative_label.configure(text="本地还没有消息，请先完成授权并整理。")
            self.todo_hint_label.configure(text="先到「设置与隐私」完成 QQ/邮箱授权。")
        if isinstance(summary, DailySummary):
            self.date_label.configure(text=format_date(summary.day))
        elif bad:
            self.date_label.configure(text="今天 · 整理未完整完成")
        else:
            self.date_label.configure(text="今天 · 整理完成")

    def show_summary(
        self,
        summary: DailySummary | None,
        *,
        fallback_notice: str | None = None,
    ) -> None:
        self._summary = summary
        if summary is None:
            self.date_label.configure(text="今天 · 还没有生成")
            self.demo_badge.pack_forget()
            self.real_badge.pack_forget()
            self.fallback_banner.pack_forget()
            self.scan_banner.pack_forget()
            if self._last_organize_result is None:
                self.organize_banner.pack_forget()
            self.narrative_label.configure(
                text=(
                    "还没有整理过消息。\n"
                    "下一步：点页面顶部的「一键整理所有消息」一次即可——程序会自动读取你已授权的 QQ 会话和邮箱，"
                    "并把今天值得处理的简报、待办和来源显示在这里（只读，不会发送任何消息）。\n"
                    "第一次使用请先到「设置与隐私」完成 QQ/邮箱授权（只缺一个来源也能继续整理另一个）；"
                    "也可以先在设置里加载演示数据体验界面。\n"
                    "QQ 客户端读取为实验性：在 QQ 打开聊天 → 点「读取 QQ 当前聊天（实验）」一次即可"
                    "（只读当前聊天、需 QQ 前台、默认关闭）。\n"
                    "也可以到「聊天导入（实验）」粘贴你主动提供的聊天文本（实验性：不会自动读取 QQ）。"
                )
            )
            self.todo_hint_label.configure(text="")
            clear_children(self.caveats_frame)
            for card in self.cards.values():
                card.set_messages([])
            return

        self.date_label.configure(text=format_date(summary.day))
        if summary.is_demo:
            self.demo_badge.pack(side="right", padx=(8, 0))
            self.real_badge.pack_forget()
        else:
            self.demo_badge.pack_forget()
            self.real_badge.pack(side="right", padx=(8, 0))

        if fallback_notice:
            self.fallback_banner.set_text(fallback_notice)
            self.fallback_banner.pack(fill="x", pady=(0, 8), before=self.narrative_card)
        else:
            self.fallback_banner.pack_forget()

        if self._last_organize_result is not None:
            # 详细整理结果已给出原因/解决方法，不再显示“详见下方/列表可能不完整”的通用提示。
            self.scan_banner.pack_forget()
        elif summary.scan_status == ScanStatus.OK:
            self.scan_banner.pack_forget()
        else:
            self.scan_banner.set_kind("warning" if summary.scan_status != ScanStatus.PAUSED else "info")
            self.scan_banner.set_text(
                f"本次采集状态：{scan_status_label(summary.scan_status)}。列表可能不完整，可以稍后重新生成摘要。"
            )
            self.scan_banner.pack(fill="x", pady=(0, 8), before=self.narrative_card)

        self.narrative_label.configure(
            text=summary.narrative or "今天暂时没有需要特别处理的消息，可以先忙自己的事情。"
        )
        counts = summary.counts or {}
        if summary.todos:
            self.todo_hint_label.configure(text=f"今天有 {len(summary.todos)} 条待办，可在左侧「待办」页处理。")
        elif counts.get("todo_open"):
            self.todo_hint_label.configure(
                text=f"今天有 {counts['todo_open']} 条待办，可在左侧「待办」页处理。"
            )
        else:
            self.todo_hint_label.configure(text="今天没有识别到待办。")

        clear_children(self.caveats_frame)
        for caveat in summary.caveats:
            Banner(self.caveats_frame, self.theme, caveat, wrap=900).pack(fill="x", pady=3)

        self.cards[Category.IMPORTANT].set_messages(summary.important)
        self.cards[Category.NORMAL].set_messages(summary.normal)
        self.cards[Category.FYI].set_messages(summary.fyi)

    def summary(self) -> DailySummary | None:
        return self._summary

    def category_counts(self) -> dict[str, int]:
        return {category.value: len(card.messages) for category, card in self.cards.items()}

    # ------------------------------------------------------------------
    # AI 语义摘要 / 每日简报（B/C）
    # ------------------------------------------------------------------
    def _set_ai_status(self, text: str, *, error: bool = False) -> None:
        self.ai_status_label.configure(text=text, fg=self.theme.color("danger" if error else "muted"))

    def _ai_external_allowed(self) -> bool:
        try:
            return bool(self.service.get_settings().external_ai_consent)
        except Exception:  # noqa: BLE001
            return False

    def _ai_api_ready(self, method_name: str) -> bool:
        if hasattr(self.service, method_name):
            return True
        detail = "AI 摘要功能还没有就绪（等待核心层实现）。"
        self._set_ai_status(detail, error=True)
        notify.show_error(self.window.root, "AI 摘要不可用", detail)
        return False

    def _conversation_for_ai(self):
        sources = self.window.pages.get("sources")
        conversation_id = ""
        if sources is not None:
            conversation_id = str(getattr(sources, "_current_conversation_id", lambda: "")() or "")
        try:
            conversations = list(self.service.list_conversations())
        except Exception as exc:  # noqa: BLE001
            self._set_ai_status(f"读取会话失败：{exc}", error=True)
            notify.show_error(self.window.root, "读取会话失败", f"暂时读不到会话：{exc}")
            return None
        if conversation_id:
            match = next((item for item in conversations if item.id == conversation_id), None)
            if match is not None:
                return match
        return conversations[0] if conversations else None

    def _conversation_semantic_data(self, conversation_id: str, use_external: bool) -> dict:
        try:
            data = self.service.generate_conversation_semantic(
                conversation_id, use_external=use_external
            )
            return dict(data or {})
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": "external_ai" if use_external else "local_rules",
                "error": str(exc),
            }

    def _daily_brief_data(self, use_external: bool) -> dict:
        try:
            data = self.service.generate_daily_brief(use_external=use_external)
            return dict(data or {})
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": "external_ai" if use_external else "local_rules",
                "error": str(exc),
            }

    def open_conversation_semantic(
        self, *, use_external: bool = False, show: bool = True
    ) -> SemanticSummaryDialog | None:
        if not self._ai_api_ready("generate_conversation_semantic"):
            return None
        if use_external and not self._ai_external_allowed():
            detail = "还没有同意外部 AI 上传：请先到「设置与隐私 → AI 回复草稿」明确同意，或继续使用本地规则。"
            self._set_ai_status(detail, error=True)
            notify.show_warning(self.window.root, "外部 AI 未同意", detail)
            return None
        conversation = self._conversation_for_ai()
        if conversation is None:
            detail = "还没有可用的会话。请先在「消息中心」选中一个会话，或先读取一些消息。"
            self._set_ai_status(detail, error=True)
            notify.show_info(self.window.root, "没有可用会话", detail)
            return None
        self._last_ai_conversation_id = conversation.id
        data = self._conversation_semantic_data(conversation.id, use_external)
        if data.get("error"):
            self._set_ai_status(f"AI 会话摘要生成失败：{data['error']}", error=True)
        else:
            self._set_ai_status("已生成 AI 会话摘要（只读，不会发送任何消息）。")
        dialog = self.show_ai_summary(
            data,
            title="AI 会话摘要",
            reload_callback=lambda flag: self._conversation_semantic_data(conversation.id, flag),
        )
        if show:
            dialog.show()
        return dialog

    def open_daily_brief(
        self, *, use_external: bool = False, show: bool = True
    ) -> SemanticSummaryDialog | None:
        if not self._ai_api_ready("generate_daily_brief"):
            return None
        if use_external and not self._ai_external_allowed():
            detail = "还没有同意外部 AI 上传：请先到「设置与隐私 → AI 回复草稿」明确同意，或继续使用本地规则。"
            self._set_ai_status(detail, error=True)
            notify.show_warning(self.window.root, "外部 AI 未同意", detail)
            return None
        data = self._daily_brief_data(use_external)
        if data.get("error"):
            self._set_ai_status(f"AI 每日简报生成失败：{data['error']}", error=True)
        else:
            self._set_ai_status("已生成 AI 每日简报（只读，不会发送任何消息）。")
        dialog = self.show_ai_summary(
            data,
            title="AI 每日简报",
            reload_callback=lambda flag: self._daily_brief_data(flag),
        )
        if show:
            dialog.show()
        return dialog

    def show_ai_summary(
        self,
        data: dict,
        *,
        title: str = "AI 语义摘要",
        reload_callback=None,
        show: bool = False,
    ) -> SemanticSummaryDialog:
        """固定钩子：用给定数据展示 AI 摘要对话框（测试可 show=False）。"""
        dialog = SemanticSummaryDialog(
            self.window,
            dict(data or {}),
            title=title,
            reload_callback=reload_callback,
        )
        self.last_ai_dialog = dialog
        if show:
            dialog.show()
        return dialog



__all__ = ["CategoryCard", "SummaryPage"]
