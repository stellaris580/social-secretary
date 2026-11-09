"""回复草稿页：选择消息 → 生成候选草稿 → 修改 → 复制。

安全边界：本页只有「复制到剪贴板」，**没有任何发送按钮**；程序不能发送 QQ 消息或邮件。
默认使用本机规则生成草稿（不联网）；外部 AI 需要用户先同意，并且每次生成前都会确认上传范围。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...contracts import ReplyDraft
from .. import notify
from ..dialogs import ExternalAIUploadDialog
from ..formatting import category_label, draft_kind_label, shorten
from ..widgets import Banner, Card, big_button, hint, label

__all__ = ["DraftsPage"]

_DRAFT_STYLES = ["简洁", "礼貌", "详细"]


class DraftsPage(tk.Frame):
    """回复草稿页面。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self.messages = []
        self._drafts: list[ReplyDraft] = []
        self._current_draft: ReplyDraft | None = None

        header = tk.Frame(self, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 6))
        label(header, theme, "回复草稿", kind="h1", bg=theme.color("bg")).pack(side="left")
        hint(
            header,
            theme,
            "草稿只是帮你打草稿，程序不会发送；复制后请自己到 QQ 或邮箱里粘贴发送。",
            bg=theme.color("bg"),
        ).pack(side="right")

        columns = tk.Frame(self, bg=theme.color("bg"))
        columns.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        # ---------------- 左：选择消息与生成 ----------------
        left = Card(columns, theme, "1. 选择要回复的消息")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.message_list = tk.Listbox(
            left.body,
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
        self.message_list.pack(fill="both", expand=True)
        self.message_list.bind("<<ListboxSelect>>", self._on_message_selected)

        self.message_preview = tk.Label(
            left.body,
            text="",
            font=theme.font("small"),
            fg=theme.color("muted"),
            bg=theme.color("card"),
            justify="left",
            anchor="w",
            wraplength=360,
        )
        self.message_preview.pack(fill="x", pady=(8, 6))

        style_row = tk.Frame(left.body, bg=theme.color("card"))
        style_row.pack(fill="x", pady=(0, 8))
        hint(style_row, theme, "草稿风格：", bg=theme.color("card")).pack(side="left")
        self.style_combo = ttk.Combobox(
            style_row, values=_DRAFT_STYLES, state="readonly", width=10, font=theme.font("base")
        )
        self.style_combo.current(0)
        self.style_combo.pack(side="left", padx=(4, 0))

        button_row = tk.Frame(left.body, bg=theme.color("card"))
        button_row.pack(fill="x")
        self.generate_local_button = big_button(
            button_row, theme, "生成草稿（本机）", self.generate_local_drafts, kind="primary"
        )
        self.generate_local_button.pack(side="left")
        self.generate_external_button = big_button(
            button_row, theme, "使用外部 AI 生成…", self.generate_external_drafts
        )
        self.generate_external_button.pack(side="left", padx=(8, 0))

        hint(
            left.body,
            theme,
            "「生成草稿（本机）」使用本机规则，不联网、不读取你的任何账号信息。"
            "「使用外部 AI」需要你先在设置里同意上传，并且每次生成前都会再次确认。",
            wrap=380,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(10, 0))

        # ---------------- 右：候选与编辑 ----------------
        right = Card(columns, theme, "2. 查看、修改、复制")
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.draft_list = tk.Listbox(
            right.body,
            height=3,
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
        self.draft_list.pack(fill="x")
        self.draft_list.bind("<<ListboxSelect>>", self._on_draft_selected)

        self.editor = tk.Text(
            right.body,
            height=11,
            font=theme.font("base"),
            wrap="word",
            bg="#FFFFFF",
            fg=theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
            padx=10,
            pady=8,
            undo=True,
        )
        self.editor.pack(fill="both", expand=True, pady=(8, 8))

        action_row = tk.Frame(right.body, bg=theme.color("card"))
        action_row.pack(fill="x")
        self.save_button = big_button(action_row, theme, "保存修改", self.save_edit)
        self.save_button.pack(side="left")
        self.copy_button = big_button(action_row, theme, "复制到剪贴板", self.copy_draft, kind="primary")
        self.copy_button.pack(side="left", padx=(8, 0))
        self._set_actions_enabled(False)

        self.status_label = hint(right.body, theme, "", wrap=380, bg=theme.color("card"))
        self.status_label.pack(fill="x", pady=(8, 0))
        Banner(
            right.body,
            theme,
            "这里没有「发送」按钮。程序只能帮你打草稿：请点「复制到剪贴板」，"
            "然后自己打开 QQ 或邮箱，粘贴到输入框并确认发送。",
            kind="info",
            wrap=380,
        ).pack(fill="x", pady=(8, 0))

        self.refresh()

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            messages = self.service.list_messages(limit=100)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"读取消息失败：{exc}", error=True)
            return
        self.messages = [message for message in messages if not message.is_self]
        current_id = self.current_message_id()

        self.message_list.configure(state="normal")
        self.message_list.delete(0, "end")
        if not self.messages:
            self.message_list.insert("end", "（还没有可回复的消息，请先生成今日摘要）")
            self.message_list.configure(state="disabled")
            self.message_preview.configure(text="")
            self._set_drafts([])
            return
        self.message_list.configure(state="normal")
        for message in self.messages:
            text = shorten(message.text, 26)
            self.message_list.insert(
                "end",
                f"[{category_label(message.category)}] {message.conversation_title} · {message.sender}：{text}",
            )
        index = 0
        if current_id:
            for position, message in enumerate(self.messages):
                if message.id == current_id:
                    index = position
                    break
        self.message_list.selection_clear(0, "end")
        self.message_list.selection_set(index)
        self.message_list.activate(index)
        self._on_message_selected()

    def current_message(self):
        selection = self.message_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if 0 <= index < len(self.messages):
            return self.messages[index]
        return None

    def current_message_id(self) -> str:
        message = self.current_message()
        return message.id if message is not None else ""

    def drafts(self) -> list[ReplyDraft]:
        return list(self._drafts)

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def generate_local_drafts(self) -> None:
        message = self.current_message()
        if message is None:
            notify.show_info(self.window.root, "还没有可回复的消息", "请先到「设置与隐私」读取消息并生成今日摘要。")
            return
        style = self.style_combo.get() or "简洁"
        try:
            drafts = self.service.generate_drafts(message.id, [style])
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "生成草稿失败",
                f"这次生成失败：{exc}\n\n本机规则草稿不需要联网，可以稍后重试；"
                "如果一直失败，请到「设置与隐私」导出诊断信息。",
            )
            self._set_status(f"生成失败：{exc}", error=True)
            return
        self._set_drafts(drafts)
        if drafts:
            self._set_status(f"已生成 {len(drafts)} 条本机规则草稿（未联网）。修改后点「复制到剪贴板」。")
        else:
            self._set_status("这次没有生成草稿，可以换一种风格再试。", error=True)

    def generate_external_drafts(self) -> None:
        message = self.current_message()
        if message is None:
            notify.show_info(self.window.root, "还没有可回复的消息", "请先到「设置与隐私」读取消息并生成今日摘要。")
            return
        try:
            settings = self.service.get_settings()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "读取设置失败", f"暂时读不到设置：{exc}")
            return
        if not settings.external_ai_consent:
            notify.show_warning(
                self.window.root,
                "外部 AI 还没有获得你的同意",
                "使用外部 AI 会把这条消息的正文发送到外部服务，必须先同意。\n\n"
                "请到「设置与隐私 → AI 回复草稿」中勾选并确认同意外部 AI 上传；"
                "在那之前可以继续使用「生成草稿（本机）」，完全不需要联网。",
            )
            self._set_status("未同意外部 AI 上传，已阻止本次操作；可以改用本机规则生成。", error=True)
            return
        dialog = ExternalAIUploadDialog(
            self.window,
            model_label=settings.external_ai_model,
            endpoint=settings.external_ai_base_url,
            message_count=1,
            include_context=True,
        )
        if not dialog.show():
            self._set_status("已取消，没有上传任何内容。")
            return
        style = self.style_combo.get() or "简洁"
        try:
            drafts = self.service.generate_drafts(message.id, [style])
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "外部 AI 生成失败",
                f"外部 AI 没有返回可用草稿：{exc}\n\n可以检查网络、服务地址与密钥后重试；"
                "或者改用「生成草稿（本机）」，它不需要联网。",
            )
            self._set_status(f"外部 AI 生成失败：{exc}", error=True)
            return
        self._set_drafts(drafts)
        if drafts:
            self._set_status(f"已通过外部 AI 生成 {len(drafts)} 条草稿（内容已按确认范围上传）。")
        else:
            self._set_status("外部 AI 没有返回草稿，可以改用本机规则生成。", error=True)

    # ------------------------------------------------------------------
    # 编辑与复制
    # ------------------------------------------------------------------
    def save_edit(self) -> None:
        if self._current_draft is None:
            notify.show_info(self.window.root, "还没有选中的草稿", "请先生成或选择一条草稿。")
            return
        try:
            self.service.save_draft_edit(self._current_draft.id, self.editor_text())
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "保存修改失败", f"这次修改保存失败：{exc}\n\n可以重试一次。")
            return
        self._set_status("修改已保存（只保存在本机，不会发送）。")

    def copy_draft(self) -> None:
        text = self.editor_text().strip()
        if not text:
            notify.show_info(self.window.root, "草稿还是空的", "先写点内容再复制。")
            return
        try:
            self.window.root.clipboard_clear()
            self.window.root.clipboard_append(text)
            self.window.root.update_idletasks()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "复制失败", f"复制失败：{exc}\n\n可以手动选中文字后按 Ctrl+C。")
            return
        self._set_status(
            f"已复制 {len(text)} 个字到剪贴板。请到 QQ 或邮箱里自行粘贴发送——程序不会替你发送。"
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def editor_text(self) -> str:
        return self.editor.get("1.0", "end-1c")

    def set_editor_text(self, text: str) -> None:
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)

    def _clear_editor(self) -> None:
        self.editor.delete("1.0", "end")

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.save_button.configure(state=state)
        self.copy_button.configure(state=state)

    def _on_message_selected(self, _event=None) -> None:
        message = self.current_message()
        if message is None:
            self.message_preview.configure(text="")
            self._set_drafts([])
            return
        self.message_preview.configure(
            text=f"会话：{message.conversation_title}\n发送者：{message.sender}\n正文：{shorten(message.text, 220)}"
        )
        try:
            drafts = self.service.list_drafts(message.id)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"读取已有草稿失败：{exc}", error=True)
            drafts = []
        self._set_drafts(drafts)

    def _set_drafts(self, drafts: list[ReplyDraft]) -> None:
        self._drafts = list(drafts)
        self._current_draft = None
        self.draft_list.configure(state="normal")
        self.draft_list.delete(0, "end")
        if not self._drafts:
            self.draft_list.insert("end", "还没有草稿")
            self.draft_list.configure(state="disabled")
            self._clear_editor()
            self._set_actions_enabled(False)
            return
        self.draft_list.configure(state="normal")
        for draft in self._drafts:
            text = f"{draft_kind_label(draft.kind)} · 风格：{draft.style or '默认'}"
            if draft.model_label:
                text += f" · {draft.model_label}"
            self.draft_list.insert("end", text)
        self.draft_list.selection_clear(0, "end")
        self.draft_list.selection_set(0)
        self.draft_list.activate(0)
        self._on_draft_selected()

    def _on_draft_selected(self, _event=None) -> None:
        selection = self.draft_list.curselection()
        if not selection:
            self._current_draft = None
            self._set_actions_enabled(False)
            return
        index = int(selection[0])
        if index >= len(self._drafts):
            return
        draft = self._drafts[index]
        self._current_draft = draft
        self.set_editor_text(draft.edited_text if draft.edited_text is not None else draft.text)
        self._set_actions_enabled(True)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(text=text, fg=self.theme.color("danger" if error else "muted"))


__all__ = ["DraftsPage"]
