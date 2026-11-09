"""收件箱页：连接 QQ 邮箱、同步真实邮件、列表查看正文（只读）。

P0-15 背景：正式程序必须有用户能直接看到的「收件箱」与「同步邮件」按钮，否则用户
添加邮箱后不知道下一步。本页把「账号状态 → 连接 → 同步 → 列表 → 正文」放在同一页。

测试/正式 E2E 依赖的固定钩子（Lead 冻结）：
- ``rows()`` / ``select_row(index)`` / ``open_selected()`` / ``detail_content()`` /
  ``status_text()`` / ``sync_now()`` / ``refresh()``；
- 公开属性 ``tree``（ttk.Treeview）与 ``detail_text``（tk.Text）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from datetime import datetime

from ...contracts import Message
from .. import notify
from ..formatting import format_datetime, sender_label, shorten
from ..widgets import Card, big_button, hint, label

__all__ = ["InboxPage"]


class InboxPage(tk.Frame):
    """收件箱页面（真实邮件）。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self._messages: list[Message] = []
        self._api_ready = True
        self._status_text = ""
        self._last_status = ""

        header = tk.Frame(self, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 6))
        label(header, theme, "收件箱", kind="h1", bg=theme.color("bg")).pack(side="left")
        hint(
            header,
            theme,
            "真实 QQ 邮件（只读）。程序不会发送邮件、不会删除远端邮件。",
            bg=theme.color("bg"),
        ).pack(side="right")

        status_card = Card(self, theme, "邮箱状态")
        status_card.pack(fill="x", padx=18, pady=(0, 8))
        self.account_label = label(status_card.body, theme, "邮箱账号：未配置", kind="base")
        self.account_label.pack(fill="x")
        self.credential_label = hint(status_card.body, theme, "授权码：未保存")
        self.credential_label.pack(fill="x", pady=(2, 0))
        self.consent_label = hint(status_card.body, theme, "读取同意：未同意")
        self.consent_label.pack(fill="x", pady=(2, 0))

        button_row = tk.Frame(status_card.body, bg=theme.color("card"))
        button_row.pack(fill="x", pady=(10, 0))
        self.connect_button = big_button(
            button_row, theme, "连接 QQ 邮箱", self.open_connect_wizard, kind="primary"
        )
        self.connect_button.pack(side="left")
        self.test_button = big_button(button_row, theme, "测试连接", self.test_connection)
        self.test_button.pack(side="left", padx=(8, 0))
        self.sync_button = big_button(button_row, theme, "同步邮件", self.sync_now, kind="primary")
        self.sync_button.pack(side="left", padx=(8, 0))
        self.refresh_button = big_button(button_row, theme, "刷新", self.refresh)
        self.refresh_button.pack(side="left", padx=(8, 0))
        self.clear_demo_button = big_button(button_row, theme, "清除演示数据", self.clear_demo_data)
        self.clear_demo_button.pack(side="right")

        self.status_label = hint(status_card.body, theme, "", wrap=980)
        self.status_label.pack(fill="x", pady=(10, 0))

        list_card = Card(self, theme, "邮件列表")
        list_card.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        columns = ("flag", "sender", "subject", "date", "snippet")
        self.tree = ttk.Treeview(
            list_card.body,
            columns=columns,
            show="headings",
            height=11,
            selectmode="browse",
            displaycolumns=("flag", "sender", "subject", "date"),
        )
        headings = {
            "flag": ("标记", 60),
            "sender": ("发件人", 170),
            "subject": ("主题", 420),
            "date": ("日期", 150),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "subject"))
        self.tree.tag_configure("real", foreground=self.theme.color("text"))
        self.tree.tag_configure("demo", foreground=self.theme.color("demo"))
        # 轻量标记：有附件的邮件只用加粗字体提示，不改 rows() 的 values 顺序/内容。
        self.tree.tag_configure("has_attachment", font=self.theme.font("button_bold"))
        scrollbar = ttk.Scrollbar(list_card.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected_detail())
        self.empty_label = hint(
            list_card.body,
            theme,
            "",
            wrap=960,
            bg=theme.color("card"),
        )
        self.empty_label.pack(fill="x", pady=(8, 0))

        detail_card = Card(self, theme, "邮件正文（只读）")
        detail_card.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self.detail_title = label(detail_card.body, theme, "选中上方一封邮件查看正文", kind="card_title")
        self.detail_title.pack(fill="x")
        text_wrap = tk.Frame(detail_card.body, bg=theme.color("card"))
        text_wrap.pack(fill="both", expand=True, pady=(6, 0))
        detail_scroll = ttk.Scrollbar(text_wrap, orient="vertical")
        detail_scroll.pack(side="right", fill="y")
        self.detail_text = tk.Text(
            text_wrap,
            height=9,
            wrap="word",
            font=self.theme.font("base"),
            bg="#FFFFFF",
            fg=self.theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.theme.color("border"),
            padx=10,
            pady=8,
            state="disabled",
            yscrollcommand=detail_scroll.set,
        )
        self.detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll.configure(command=self.detail_text.yview)

        self.refresh()

    # ------------------------------------------------------------------
    # 状态与刷新
    # ------------------------------------------------------------------
    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status_text = text
        color = self.theme.color("danger") if error else self.theme.color("muted")
        self.status_label.configure(text=text, fg=color)

    def status_text(self) -> str:
        return self.status_label.cget("text")

    def refresh(self) -> None:
        self._api_ready = hasattr(self.service, "list_inbox_messages")
        if not self._api_ready:
            self._set_status(
                "邮箱收件箱功能还没有就绪（等待核心层实现）。现在仍可查看演示数据；请稍后重启程序重试。",
                error=True,
            )
            self._render_empty("邮箱功能尚未就绪，暂时无法列出真实邮件。")
            return
        try:
            accounts = list(self.service.list_mail_accounts())
            status = self.service.mail_inbox_status() or {}
            messages = list(self.service.list_inbox_messages(limit=200))
        except AttributeError as exc:
            self._api_ready = False
            self._set_status(f"邮箱收件箱接口不可用：{exc}", error=True)
            self._render_empty("邮箱功能尚未就绪，暂时无法列出真实邮件。")
            return
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                f"读取收件箱失败：{exc}。可以点「刷新」重试；如果一直失败，请到「设置与隐私」导出诊断信息。",
                error=True,
            )
            self._render_empty("读取收件箱失败，请点「刷新」重试。")
            return

        self._accounts = accounts
        self._inbox_status = status
        self._messages = messages
        email = str(status.get("email") or (accounts[0].identifier if accounts else "") or "")
        # 兼容 core 实现（has_credential/consent）与早期接口（credential_saved/mail_read_consent）。
        credential_saved = bool(
            status.get("credential_saved", status.get("has_credential", False))
        )
        consent = bool(status.get("mail_read_consent", status.get("consent", False)))
        paused = bool(status.get("paused", self.window.is_collection_paused()))
        self.account_label.configure(
            text=f"邮箱账号：{email}" if email else "邮箱账号：未配置（点「连接 QQ 邮箱」开始）"
        )
        self.credential_label.configure(
            text=f"授权码：{'已保存到 Windows 凭据管理器' if credential_saved else '未保存'}"
        )
        self.consent_label.configure(
            text=f"读取同意：{'已同意（只读）' if consent else '未同意'}　·　采集："
            f"{'已暂停' if paused else '待命'}"
        )
        ready = self._api_ready and bool(accounts)
        self.test_button.configure(state="normal" if ready else "disabled")
        self.sync_button.configure(state="normal" if ready else "disabled")

        self._fill_tree(messages)
        self._render_empty_state(status, accounts, messages)
        self.refresh_button.configure(state="normal")
        self.clear_demo_button.configure(
            state="normal" if self.window.is_demo_active() else "disabled"
        )

    def _render_empty_state(self, status: dict, accounts: list, messages: list) -> None:
        if messages:
            real = sum(1 for message in messages if not message.is_demo)
            demo = len(messages) - real
            suffix = f"（真实 {real} · 演示 {demo}）" if demo else f"（真实 {real}）"
            self._set_status(f"共 {len(messages)} 封邮件{suffix}。点一行在下方查看完整正文。")
            self.empty_label.pack_forget()
            return
        if not accounts:
            reason = "还没有连接 QQ 邮箱。点「连接 QQ 邮箱」，按向导填写邮箱地址和授权码。"
        elif status.get("last_error"):
            reason = (
                f"上次同步邮件失败：{status.get('last_error')}。可以点「测试连接」检查授权码，"
                "再点「同步邮件」重试。"
            )
        elif not status.get("last_sync_at"):
            reason = "邮箱已连接，但还没有同步过。点「同步邮件」读取真实邮件。"
        else:
            reason = "邮箱暂无可显示邮件。可以稍后再点「同步邮件」；确认邮箱里确实有邮件。"
        self._render_empty(reason)
        self._set_status(reason, error=bool(status.get("last_error")) and bool(accounts))

    def _render_empty(self, text: str) -> None:
        self.empty_label.configure(text=text)
        self.empty_label.pack(fill="x", pady=(8, 0))

    def _fill_tree(self, messages: list[Message]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, message in enumerate(messages):
            flag = "演示" if message.is_demo else "真实"
            subject = message.subject or message.conversation_title
            tags = ["demo" if message.is_demo else "real"]
            has_attachments = bool(getattr(message, "attachment_names", ()) or ())
            if has_attachments:
                tags.append("has_attachment")
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                tags=tuple(tags),
                values=(
                    flag,
                    sender_label(message.sender, message.is_self),
                    subject,
                    format_datetime(message.timestamp),
                    message.text or "",
                ),
            )

    # ------------------------------------------------------------------
    # 固定测试钩子
    # ------------------------------------------------------------------
    def rows(self) -> list[dict]:
        rows: list[dict] = []
        for iid in self.tree.get_children():
            values = tuple(self.tree.item(iid, "values"))
            try:
                index = int(iid)
                has_attachments = bool(
                    getattr(self._messages[index], "attachment_names", ()) or ()
                )
            except (TypeError, ValueError, IndexError):
                has_attachments = False
            # values 顺序固定为 (标记, 发件人, 主题, 日期, 正文片段)，附件只作为额外属性。
            rows.append(
                {"iid": iid, "values": values, "has_attachment": has_attachments}
            )
        return rows

    def select_row(self, index: int) -> bool:
        children = self.tree.get_children()
        if not (0 <= index < len(children)):
            return False
        iid = children[index]
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._show_detail(index)
        return True

    def open_selected(self) -> bool:
        selection = self.tree.selection()
        if not selection:
            return False
        try:
            index = int(selection[0])
        except (TypeError, ValueError):
            index = self.tree.index(selection[0])
        if not (0 <= index < len(self._messages)):
            return False
        self._show_detail(index)
        return True

    def detail_content(self) -> str:
        return self.detail_text.get("1.0", "end-1c")

    def _show_selected_detail(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        try:
            index = int(selection[0])
        except (TypeError, ValueError):
            index = self.tree.index(selection[0])
        self._show_detail(index)

    def _show_detail(self, index: int) -> None:
        if not (0 <= index < len(self._messages)):
            return
        message = self._messages[index]
        subject = message.subject or message.conversation_title or "（无主题）"
        marker = "演示" if message.is_demo else "真实"
        when = format_datetime(message.timestamp or message.collected_at)
        source = message.source_locator or "来源位置未知"
        body = message.text or "（这封邮件没有可显示的正文）"
        attachment_names = tuple(getattr(message, "attachment_names", ()) or ())
        attachment_line = ""
        if attachment_names:
            shown = "、".join(shorten(name, 40) for name in attachment_names[:10])
            attachment_line = (
                f"附件：{shown}（共 {len(attachment_names)} 个；"
                "程序未下载/不保存附件内容）\n"
            )
        self.detail_title.configure(text=f"[{marker}] {subject}")
        content = (
            f"主题：{subject}\n"
            f"发件人：{sender_label(message.sender, message.is_self)}\n"
            f"时间：{when}\n"
            f"来源：{source}\n"
            f"{attachment_line}"
            f"标记：{marker}（{'仅合成演示数据' if message.is_demo else '来自真实邮箱'}）\n\n"
            f"正文：\n{body}"
        )
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", content)
        self.detail_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def open_connect_wizard(self):
        """打开连接向导；成功后跳到收件箱并刷新。"""
        from ..dialogs import MailConnectWizard

        wizard = MailConnectWizard(self.window)
        accepted = wizard.show()
        if accepted:
            self.refresh()
            self.window.show_page("inbox")
            self.window.refresh_status()
        return wizard

    def test_connection(self):
        if not self._api_ready:
            self._set_status("邮箱功能还没有就绪，暂时无法测试连接。", error=True)
            return None
        accounts = getattr(self, "_accounts", [])
        if not accounts:
            self._set_status("还没有连接邮箱，请先点「连接 QQ 邮箱」。", error=True)
            notify.show_warning(
                self.window.root,
                "还没有连接邮箱",
                "请先点「连接 QQ 邮箱」，填写邮箱地址和授权码后再测试连接。",
            )
            return None
        email = accounts[0].identifier or accounts[0].display_name
        try:
            check = self.service.test_mail_credential(email)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"测试连接失败：{exc}。请检查授权码是否正确、邮箱是否已开启 IMAP 服务。", error=True)
            return None
        if check.ok:
            latency = f"，耗时约 {check.latency_ms} 毫秒" if check.latency_ms else ""
            self._set_status(f"连接成功（{email}）{latency}。接下来点「同步邮件」读取真实邮件。")
        else:
            self._set_status(
                f"连接失败：{check.detail}。请检查授权码是否正确、邮箱是否已开启 IMAP 服务。",
                error=True,
            )
        return check

    def sync_now(self):
        if not self._api_ready:
            self._set_status("邮箱功能还没有就绪，暂时无法同步邮件。", error=True)
            return None
        accounts = getattr(self, "_accounts", [])
        if not accounts:
            self._set_status("还没有连接邮箱，请先点「连接 QQ 邮箱」。", error=True)
            notify.show_warning(
                self.window.root,
                "还没有连接邮箱",
                "请先点「连接 QQ 邮箱」，按向导完成授权码、同意读取和同步。",
            )
            return None
        self._set_status("正在同步邮件（只读，不会发送任何邮件）…")
        self.sync_button.configure(state="disabled")
        try:
            results = self.service.sync_mail(user_confirmed=True)
        except Exception as exc:  # noqa: BLE001
            self.sync_button.configure(state="normal")
            self._set_status(
                f"同步邮件失败：{exc}。可以点「测试连接」检查授权码，或稍后再点「同步邮件」重试。",
                error=True,
            )
            notify.show_error(
                self.window.root,
                "同步邮件失败",
                f"这次同步失败：{exc}\n\n可以点「测试连接」检查授权码，或稍后再点「同步邮件」重试。",
            )
            return None
        self.sync_button.configure(state="normal")
        failed = [result for result in results if result.status.value != "ok"]
        self.refresh()
        self.window.refresh_status()
        if failed:
            details = "；".join(
                f"{result.account_id}：{result.error or result.status.value}" for result in failed
            )
            self._set_status(f"同步邮件失败：{details}。请检查网络/授权码后再试。", error=True)
            notify.show_error(self.window.root, "同步邮件失败", f"部分邮箱同步失败：{details}")
            return results
        new_count = sum(int(result.messages_new or 0) for result in results)
        if not results:
            self._set_status("没有可同步的邮箱账号。请点「连接 QQ 邮箱」完成连接。", error=True)
        else:
            self._set_status(f"同步完成：读取到 {new_count} 封邮件（只读）。点一行查看完整正文。")
        return results

    def clear_demo_data(self) -> None:
        try:
            self.service.clear_demo_data()
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"清除演示数据失败：{exc}。可以稍后重试。", error=True)
            return
        self.window.refresh_all()
        self._set_status("演示数据已清除。收件箱只显示真实邮件；可随时重新加载演示数据。")
