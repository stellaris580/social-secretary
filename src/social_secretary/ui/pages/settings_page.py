"""设置与隐私页：账号管理、白名单会话、采集、AI 草稿、隐私与本机数据、关于。

设计原则：
- 默认最安全：默认只用本机规则、默认不读取任何会话、默认不联网；
- 危险操作（删除本机数据）必须二次确认；外部 AI 必须显式同意并说明上传范围；
- 凭据（邮箱授权码、AI Key）只在内存里短暂出现，交由服务保存进 Windows 凭据管理器，
  界面不显示明文、不写日志。
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

from ...contracts import (
    APP_VERSION,
    AppSettings,
    ConsentScope,
    ScanStatus,
)
from .. import notify
from ..dialogs import ConfirmDeleteDataDialog, ExternalAIUploadDialog, about_text
from ..formatting import (
    account_kind_label,
    conversation_kind_label,
    format_bytes,
    format_datetime,
    priority_label,
    scan_status_label,
)
from ..widgets import Banner, Card, big_button, clear_children, hint, label, make_scrollable

__all__ = ["SettingsPage"]

_AUTH_CODE_HELP = (
    "授权码不是你的 QQ 密码。它是 QQ 邮箱为「第三方邮件客户端」生成的 16 位专用密码，"
    "只用于邮件程序登录，即使泄露也不等于泄露 QQ 密码。\n\n"
    "获取步骤（在电脑上操作）：\n"
    "1. 用浏览器打开 mail.qq.com，登录你的 QQ 邮箱；\n"
    "2. 点顶部「设置」→「账号」（部分版本叫「账号与安全」）；\n"
    "3. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」；\n"
    "4. 开启「IMAP/SMTP 服务」，按提示用手机短信验证；\n"
    "5. 点「生成授权码」，把 16 位授权码复制到上面的输入框。\n\n"
    "安全说明：授权码只输入到本机程序，保存在 Windows 凭据管理器；程序不会显示明文、"
    "不会写进日志、不会上传。你随时可以点「删除授权码」清除它。"
)

_SCOPE_LABELS = {
    ConsentScope.QQ_READ.value: "读取 QQ 会话",
    ConsentScope.MAIL_READ.value: "读取 QQ 邮箱",
    ConsentScope.EXTERNAL_AI.value: "外部 AI 上传",
}


def _small_entry(parent: tk.Misc, theme, *, width: int = 40, show: str | None = None) -> tk.Entry:
    entry = tk.Entry(
        parent,
        width=width,
        font=theme.font("base"),
        bg="#FFFFFF",
        fg=theme.color("text"),
        relief="flat",
        highlightthickness=1,
        highlightbackground=theme.color("border"),
        highlightcolor=theme.color("primary"),
        insertbackground=theme.color("text"),
    )
    if show:
        entry.configure(show=show)
    return entry


class SettingsPage(tk.Frame):
    """设置与隐私页面。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self._refreshing = False
        self._settings: AppSettings | None = None
        self._conversations = []
        self.account_rows: list[dict] = []
        self.whitelist_rows: list[dict] = []

        header = tk.Frame(self, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 4))
        label(header, theme, "设置与隐私", kind="h1", bg=theme.color("bg")).pack(side="left")
        hint(header, theme, "默认不读取会话、默认不联网；所有开关都可以随时关闭。", bg=theme.color("bg")).pack(
            side="right"
        )

        self.error_banner = Banner(self, theme, "", wrap=900)
        self._canvas, content = make_scrollable(self, theme)
        self.content = content

        self._build_capability_card(content)
        self._build_mail_card(content)
        self._build_accounts_card(content)
        self._build_whitelist_card(content)
        self._build_collection_card(content)
        self._build_ai_card(content)
        self._build_privacy_card(content)
        self._build_about_card(content)

        self.refresh()

    # ==================================================================
    # 能力状态（开箱即用 / 需要额外配置 / 当前不可用）
    # ==================================================================
    def _build_capability_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, f"能力状态（版本 {APP_VERSION}）")
        card.pack(fill="x", pady=(0, 10))
        hint(
            card.body,
            theme,
            "先看清楚哪些功能装好就能用、哪些需要你额外配置。程序默认只在本机运行，不联网。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(0, 8))

        label(
            card.body,
            theme,
            "● 开箱即用（无需任何配置）",
            kind="bold",
            color=theme.color("success"),
        ).pack(fill="x", anchor="w", pady=(4, 2))
        hint(
            card.body,
            theme,
            "今日摘要、重要 / 普通 / 仅供了解 分类、待办提取、本机规则回复草稿、消息来源、"
            "暂停采集 / 删除本机数据等隐私控制 —— 安装后即可使用，全部在本机计算，不联网。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", padx=(14, 0))

        label(
            card.body,
            theme,
            "● 需要额外配置（可选，不配置也能用其他功能）",
            kind="bold",
            color=theme.color("warning"),
        ).pack(fill="x", anchor="w", pady=(8, 2))
        self.mail_capability_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.mail_capability_label.pack(fill="x", padx=(14, 0))
        self.ai_capability_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.ai_capability_label.pack(fill="x", padx=(14, 0), pady=(2, 0))

        label(
            card.body,
            theme,
            "● 实验性（默认关闭）",
            kind="bold",
            color=theme.color("warning"),
        ).pack(fill="x", anchor="w", pady=(8, 2))
        hint(
            card.body,
            theme,
            "QQ 客户端读取（实验）：默认关闭；开启后仅只读你在白名单中授权的会话，扫描时需 QQ 主窗口在前台"
            "（会短暂占用前台，可能打断操作）；仍无任何发送能力。未完成全部真机内容验收，可能失败，"
            "失败会明确提示。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", padx=(14, 0))

        label(
            card.body,
            theme,
            "● 实验性（未验证）",
            kind="bold",
            color=theme.color("warning"),
        ).pack(fill="x", anchor="w", pady=(8, 2))
        hint(
            card.body,
            theme,
            "聊天导入：需要你主动选择/粘贴聊天文本，不会自动读取 QQ、不读 QQ 数据库、不联网；"
            "未验证真实 QQ 导出格式，不保证兼容。请先预览、确认后再导入。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", padx=(14, 0))

    def _refresh_capability_status(self) -> None:
        """刷新「需要额外配置」两行的当前状态（邮箱授权码 / 外部 AI 密钥）。"""
        mail_saved = False
        try:
            for account in self.service.list_accounts():
                if account.kind.value != "email":
                    continue
                identifier = account.identifier or account.display_name
                if identifier and self.service.has_mail_credential(identifier):
                    mail_saved = True
                    break
        except Exception:  # noqa: BLE001
            mail_saved = False
        self.mail_capability_label.configure(
            text="QQ 邮箱读取：需要邮箱授权码（16 位专用密码，不是 QQ 密码；获取步骤见下方「什么是授权码？」）。"
            f"当前状态：{'已保存授权码。' if mail_saved else '还没有配置授权码，邮箱读取暂不可用。'}"
        )

        try:
            has_key = bool(self.service.has_external_ai_key())
        except Exception:  # noqa: BLE001
            has_key = False
        consented = bool(self._settings and self._settings.external_ai_consent)
        if has_key and consented:
            state = "已保存密钥并同意上传（每次生成前仍会再次确认上传范围）。"
        elif has_key:
            state = "已保存 API Key，但还没有同意上传，外部 AI 仍不可用。"
        else:
            state = "还没有配置 API Key，外部 AI 不可用（本机规则草稿不受影响）。"
        self.ai_capability_label.configure(
            text="外部 AI 草稿：需要 API Key 并在设置中明确同意上传（默认关闭、默认不联网）。"
            f"当前状态：{state}"
        )

    # ==================================================================
    # 账号
    # ==================================================================
    def _build_accounts_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "账号")
        card.pack(fill="x", pady=(0, 10))
        hint(card.body, theme, "账号只用于读取消息。程序没有发送消息或邮件的能力，添加账号后也不会主动做任何事。", wrap=900, bg=theme.color("card")).pack(fill="x")

        self.accounts_frame = tk.Frame(card.body, bg=theme.color("card"))
        self.accounts_frame.pack(fill="x", pady=(8, 8))

        # P0-25：最简单路径——QQ 打开聊天 → 点这里 → 确认。
        label(card.body, theme, "一键读取（推荐）", kind="bold").pack(fill="x", anchor="w", pady=(6, 2))
        self.quick_qq_read_button = big_button(
            card.body,
            theme,
            "一键读取当前 QQ 聊天（实验）",
            self._quick_qq_read,
            kind="primary",
        )
        self.quick_qq_read_button.pack(anchor="w")
        hint(
            card.body,
            theme,
            "QQ 打开聊天 → 点这里 → 确认即可（只读当前聊天）。下方「手动设置」是高级选项，普通用户不用管。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(4, 10))

        # 隐私优先：只读取会话标题让用户勾选，默认一个都不读。
        self.qq_session_picker_button = big_button(
            card.body,
            theme,
            "选择要读取的 QQ 会话（实验）",
            self._open_qq_session_picker,
        )
        self.qq_session_picker_button.pack(anchor="w")
        hint(
            card.body,
            theme,
            "只显示 QQ 会话名称供你勾选，不读取聊天内容；默认一个都不读，勾选后才只读你选中的会话。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(4, 10))
        self.qq_authorized_status = hint(
            card.body, theme, "", wrap=900, bg=theme.color("card")
        )
        self.qq_authorized_status.pack(fill="x", pady=(0, 6))

        label(card.body, theme, "QQ 读取（实验）", kind="bold").pack(fill="x", anchor="w", pady=(6, 2))
        self.qq_experimental_var = tk.BooleanVar(value=False)
        self.qq_experimental_check = tk.Checkbutton(
            card.body,
            text="启用实验性 QQ 客户端读取（默认关闭；只读授权会话）",
            variable=self.qq_experimental_var,
            command=self.toggle_qq_experimental,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
            anchor="w",
        )
        self.qq_experimental_check.pack(fill="x", pady=(0, 2))
        hint(
            card.body,
            theme,
            "开启后扫描仍只读取你在白名单里勾选的 QQ 会话，需要 QQ 主窗口在前台，可能短暂打断你的操作；"
            "程序仍没有任何发送能力。未完成全部真机内容验收，可能失败；失败会明确提示。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(0, 6))
        self.qq_experimental_status = hint(
            card.body, theme, "当前：关闭（扫描不会读取 QQ）", wrap=900, bg=theme.color("card")
        )
        self.qq_experimental_status.pack(fill="x", pady=(0, 6))

        label(card.body, theme, "添加 QQ 账号", kind="bold").pack(fill="x", anchor="w", pady=(6, 2))
        self.qq_name_input = _small_entry(card.body, theme)
        self.qq_name_input.pack(fill="x", pady=(0, 6))
        self.qq_allowlist_input = _small_entry(card.body, theme)
        self.qq_allowlist_input.pack(fill="x")
        hint(card.body, theme, "QQ 名称（例如「我的 QQ」）；可选：允许读取的会话名，用逗号分隔，留空则稍后在白名单里勾选。", wrap=900, bg=theme.color("card")).pack(fill="x", pady=(4, 6))
        self.add_qq_button = big_button(card.body, theme, "添加 QQ 账号", self.add_qq_account)
        self.add_qq_button.pack(anchor="w")
        hint(
            card.body,
            theme,
            "只读取你勾选的会话；读取 QQ 时可能短暂切换到 QQ 前台窗口，会打断你正在进行的操作。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(6, 10))

        label(card.body, theme, "高级操作（普通用户不用管）", kind="bold").pack(
            fill="x", anchor="w", pady=(6, 2)
        )
        advanced_row = tk.Frame(card.body, bg=theme.color("card"))
        advanced_row.pack(fill="x")
        self.advanced_scan_button = big_button(
            advanced_row, theme, "立即读取一次（高级）", lambda: self.window.run_scan()
        )
        self.advanced_scan_button.pack(side="left")
        self.advanced_generate_button = big_button(
            advanced_row,
            theme,
            "生成今日摘要（高级）",
            lambda: self.window.generate_summary(),
        )
        self.advanced_generate_button.pack(side="left", padx=(8, 0))
        hint(
            card.body,
            theme,
            "日常只需要用主界面的「一键整理所有消息」；这里的单独操作供排查/高级用户使用。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(4, 0))

    # ==================================================================
    # 邮箱
    # ==================================================================
    def _build_mail_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "邮箱账号（QQ 邮箱，只读）")
        card.pack(fill="x", pady=(0, 10))
        label(card.body, theme, "添加 QQ 邮箱账号", kind="bold").pack(fill="x", anchor="w")

        form = tk.Frame(card.body, bg=theme.color("card"))
        form.pack(fill="x", pady=(6, 0))
        hint(form, theme, "邮箱地址：", bg=theme.color("card"), wrap=90).pack(side="left")
        self.mail_address_input = _small_entry(form, theme, width=30)
        self.mail_address_input.pack(side="left", padx=(4, 12))
        hint(form, theme, "显示名（可留空）：", bg=theme.color("card"), wrap=130).pack(side="left")
        self.mail_name_input = _small_entry(form, theme, width=20)
        self.mail_name_input.pack(side="left", padx=(4, 0))

        auth_row = tk.Frame(card.body, bg=theme.color("card"))
        auth_row.pack(fill="x", pady=(8, 0))
        hint(auth_row, theme, "授权码（密码回显）：", bg=theme.color("card"), wrap=140).pack(side="left")
        self.mail_auth_code_input = _small_entry(auth_row, theme, width=34, show="*")
        self.mail_auth_code_input.pack(side="left", padx=(4, 0))

        self.auth_help_button = big_button(
            card.body, theme, "什么是授权码？点这里查看获取步骤", self.toggle_auth_help, kind="link"
        )
        self.auth_help_button.pack(fill="x", anchor="w", pady=(6, 0))
        self.auth_help_panel = tk.Label(
            card.body,
            text=_AUTH_CODE_HELP,
            font=theme.font("small"),
            fg=theme.color("text"),
            bg=theme.color("primary_soft"),
            justify="left",
            anchor="w",
            padx=12,
            pady=10,
            wraplength=880,
        )

        button_row = tk.Frame(card.body, bg=theme.color("card"))
        button_row.pack(fill="x", pady=(8, 0))
        self.add_mail_button = big_button(button_row, theme, "添加邮箱账号", self.add_mail_account)
        self.add_mail_button.pack(side="left")
        self.save_mail_credential_button = big_button(
            button_row, theme, "保存授权码", self.save_mail_credential
        )
        self.save_mail_credential_button.pack(side="left", padx=(8, 0))
        self.test_mail_credential_button = big_button(button_row, theme, "测试连接", self.test_mail_credential)
        self.test_mail_credential_button.pack(side="left", padx=(8, 0))
        self.delete_mail_credential_button = big_button(
            button_row, theme, "删除授权码", self.delete_mail_credential
        )
        self.delete_mail_credential_button.pack(side="left", padx=(8, 0))

        self.mail_status_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.mail_status_label.pack(fill="x", pady=(8, 0))
        hint(
            card.body,
            theme,
            "授权码只保存在 Windows 凭据管理器，不会写进本程序的设置文件，也不会在界面上显示明文。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(4, 0))

    # ==================================================================
    # 白名单
    # ==================================================================
    def _build_whitelist_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "允许读取的会话（白名单）")
        card.pack(fill="x", pady=(0, 10))
        hint(card.body, theme, "只有勾选的会话才会被读取，默认全部不读取。取消勾选后，程序不会再读这个会话。", wrap=900, bg=theme.color("card")).pack(fill="x")

        row = tk.Frame(card.body, bg=theme.color("card"))
        row.pack(fill="x", pady=(8, 4))
        hint(row, theme, "账号：", bg=theme.color("card"), wrap=50).pack(side="left")
        self.whitelist_account_combo = ttk.Combobox(row, state="readonly", width=26, font=theme.font("base"))
        self.whitelist_account_combo.pack(side="left", padx=(4, 8))
        self.whitelist_account_combo.bind("<<ComboboxSelected>>", lambda _event: self._reload_whitelist())
        self.refresh_conversations_button = big_button(row, theme, "刷新会话列表", self.refresh_conversations)
        self.refresh_conversations_button.pack(side="left")

        self.whitelist_status_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.whitelist_status_label.pack(fill="x", pady=(4, 4))
        self.whitelist_frame = tk.Frame(card.body, bg=theme.color("card"))
        self.whitelist_frame.pack(fill="x")

    # ==================================================================
    # 采集
    # ==================================================================
    def _build_collection_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "采集")
        card.pack(fill="x", pady=(0, 10))

        self.pause_var = tk.IntVar(value=0)
        self.pause_check = tk.Checkbutton(
            card.body,
            text="暂停全部采集（仍可手动生成今日摘要）",
            variable=self.pause_var,
            command=self._on_pause_toggled,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
            anchor="w",
            cursor="hand2",
        )
        self.pause_check.pack(fill="x")
        self.collection_status_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.collection_status_label.pack(fill="x", pady=(4, 6))

        interval_row = tk.Frame(card.body, bg=theme.color("card"))
        interval_row.pack(fill="x")
        hint(interval_row, theme, "自动采集间隔（分钟，0 = 仅手动）：", bg=theme.color("card")).pack(side="left")
        self.interval_spin = tk.Spinbox(
            interval_row,
            from_=0,
            to=240,
            increment=5,
            width=8,
            font=theme.font("base"),
            bg="#FFFFFF",
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
        )
        self.interval_spin.pack(side="left", padx=(4, 8))
        hint(interval_row, theme, "建议保持「仅手动」，需要时在主窗口点「立即读取一次」。", bg=theme.color("card")).pack(side="left")

        self.foreground_var = tk.IntVar(value=1)
        self.foreground_check = tk.Checkbutton(
            card.body,
            text="读取 QQ 前先显示 5 秒前台警告（可能打断电脑操作）",
            variable=self.foreground_var,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
            anchor="w",
            cursor="hand2",
        )
        self.foreground_check.pack(fill="x", pady=(6, 4))
        self.save_collection_button = big_button(card.body, theme, "保存采集设置", self.save_collection_settings, kind="primary")
        self.save_collection_button.pack(anchor="w", pady=(4, 0))

    # ==================================================================
    # AI
    # ==================================================================
    def _build_ai_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "AI 回复草稿")
        card.pack(fill="x", pady=(0, 10))
        hint(card.body, theme, "默认只用本机规则生成草稿，不联网。外部 AI 是可选项，必须由你明确同意后才能使用。", wrap=900, bg=theme.color("card")).pack(fill="x")
        hint(
            card.body,
            theme,
            "费用与数据范围：外部 AI 可能按服务商价格产生费用；若服务返回用量，界面会显示调用次数/token。"
            "只发送你在界面中选中的消息/摘要所需内容；默认本地规则不联网、不产生费用。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(4, 0))

        self.ai_mode_var = tk.IntVar(value=0)
        self.local_radio = tk.Radiobutton(
            card.body,
            text="仅使用本机规则生成草稿（推荐，不联网）",
            variable=self.ai_mode_var,
            value=0,
            command=self._on_ai_mode_changed,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
            anchor="w",
            cursor="hand2",
        )
        self.local_radio.pack(fill="x", pady=(6, 0))
        self.external_radio = tk.Radiobutton(
            card.body,
            text="使用外部 AI 生成草稿（会把消息内容发送到外部服务）",
            variable=self.ai_mode_var,
            value=1,
            command=self._on_ai_mode_changed,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
            anchor="w",
            cursor="hand2",
        )
        self.external_radio.pack(fill="x")

        Banner(
            card.body,
            theme,
            "使用外部 AI 时会上传：你选中的消息正文、发送者昵称、会话标题和最近几条上下文。"
            "不会上传：邮箱授权码、AI 密钥、账号密码、其他会话内容。每次生成草稿前还会再弹窗确认一次。",
            kind="info",
            wrap=900,
        ).pack(fill="x", pady=(8, 8))

        form = tk.Frame(card.body, bg=theme.color("card"))
        form.pack(fill="x")
        hint(form, theme, "服务地址：", bg=theme.color("card"), wrap=70).grid(row=0, column=0, sticky="w", pady=2)
        self.external_url_input = _small_entry(form, theme, width=46)
        self.external_url_input.grid(row=0, column=1, sticky="w", padx=(4, 0), pady=2)
        hint(form, theme, "模型名称：", bg=theme.color("card"), wrap=70).grid(row=1, column=0, sticky="w", pady=2)
        self.external_model_input = _small_entry(form, theme, width=30)
        self.external_model_input.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=2)
        hint(form, theme, "API 密钥：", bg=theme.color("card"), wrap=70).grid(row=2, column=0, sticky="w", pady=2)
        self.external_key_input = _small_entry(form, theme, width=46, show="*")
        self.external_key_input.grid(row=2, column=1, sticky="w", padx=(4, 0), pady=2)

        key_row = tk.Frame(card.body, bg=theme.color("card"))
        key_row.pack(fill="x", pady=(6, 4))
        self.save_external_key_button = big_button(key_row, theme, "保存密钥", self.save_external_key)
        self.save_external_key_button.pack(side="left")
        self.test_external_ai_button = big_button(key_row, theme, "测试连接", self.test_external_ai)
        self.test_external_ai_button.pack(side="left", padx=(8, 0))

        self.external_status_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.external_status_label.pack(fill="x", pady=(2, 4))

        consent_row = tk.Frame(card.body, bg=theme.color("card"))
        consent_row.pack(fill="x", pady=(4, 0))
        self.external_consent_label = label(
            consent_row, theme, "", kind="base", bg=theme.color("card"), wrap=520
        )
        self.external_consent_label.pack(side="left", fill="x", expand=True)
        self.grant_external_button = big_button(
            consent_row, theme, "同意外部 AI 上传…", self.grant_external_consent
        )
        self.grant_external_button.pack(side="left", padx=(8, 0))
        self.revoke_external_button = big_button(
            consent_row, theme, "撤回同意", self.revoke_external_consent
        )
        self.revoke_external_button.pack(side="left", padx=(8, 0))

        self.save_ai_settings_button = big_button(card.body, theme, "保存 AI 设置", self.save_ai_settings, kind="primary")
        self.save_ai_settings_button.pack(anchor="w", pady=(8, 0))
        hint(
            card.body,
            theme,
            "只有点了「同意外部 AI 上传…」并确认后，外部 AI 才会启用；否则保存会被阻止。",
            wrap=900,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(6, 0))

    # ==================================================================
    # 隐私
    # ==================================================================
    def _build_privacy_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "隐私与本机数据")
        card.pack(fill="x", pady=(0, 10))

        self.storage_stats_label = tk.Label(
            card.body,
            text="",
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("card"),
            justify="left",
            anchor="w",
            wraplength=900,
        )
        self.storage_stats_label.pack(fill="x")

        stats_row = tk.Frame(card.body, bg=theme.color("card"))
        stats_row.pack(fill="x", pady=(6, 4))
        self.refresh_stats_button = big_button(stats_row, theme, "刷新统计", self.refresh)
        self.refresh_stats_button.pack(side="left")
        self.export_diagnostics_button = big_button(
            stats_row, theme, "导出诊断信息…", self.export_diagnostics
        )
        self.export_diagnostics_button.pack(side="left", padx=(8, 0))

        Banner(
            card.body,
            theme,
            "本地数据只存在这台电脑上，默认不联网、不上传。日志只记录计数、耗时与错误码，"
            "不记录消息正文、邮箱授权码或 AI 密钥。",
            kind="info",
            wrap=900,
        ).pack(fill="x", pady=(6, 8))

        label(card.body, theme, "同意记录（本地）", kind="bold").pack(fill="x", anchor="w")
        self.consent_history_list = tk.Listbox(
            card.body,
            height=4,
            font=theme.font("small"),
            bg="#FFFFFF",
            fg=theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
            activestyle="none",
            exportselection=False,
        )
        self.consent_history_list.pack(fill="x", pady=(4, 8))

        label(card.body, theme, "演示数据", kind="bold").pack(fill="x", anchor="w")
        self.demo_status_label = hint(card.body, theme, "", wrap=900, bg=theme.color("card"))
        self.demo_status_label.pack(fill="x", pady=(2, 4))
        demo_row = tk.Frame(card.body, bg=theme.color("card"))
        demo_row.pack(fill="x")
        self.load_demo_button = big_button(demo_row, theme, "加载演示数据（合成示例）", self.load_demo_data)
        self.load_demo_button.pack(side="left")
        self.clear_demo_button = big_button(demo_row, theme, "清除演示数据", self.clear_demo_data)
        self.clear_demo_button.pack(side="left", padx=(8, 0))
        hint(card.body, theme, "演示数据是程序生成的示例对话，不含任何真实聊天内容，适合先体验界面。", wrap=900, bg=theme.color("card")).pack(fill="x", pady=(4, 8))

        danger_row = tk.Frame(card.body, bg=theme.color("card"))
        danger_row.pack(fill="x")
        self.delete_all_data_button = big_button(danger_row, theme, "删除全部本机数据…", self.delete_all_data, kind="danger")
        self.delete_all_data_button.pack(side="left")
        hint(danger_row, theme, "只删除本机整理结果，不会删除 QQ 或邮箱里的原始消息。", bg=theme.color("card")).pack(
            side="left", padx=(10, 0)
        )

    # ==================================================================
    # 关于
    # ==================================================================
    def _build_about_card(self, parent: tk.Misc) -> None:
        theme = self.theme
        card = Card(parent, theme, "关于")
        card.pack(fill="x", pady=(0, 16))
        self.about_label = tk.Label(
            card.body,
            text=about_text(),
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("card"),
            justify="left",
            anchor="w",
            wraplength=900,
        )
        self.about_label.pack(fill="x")
        Banner(
            card.body,
            theme,
            "本程序没有任何发送能力：没有发送 QQ 消息、发送邮件、删除远端消息的按钮或代码路径。",
            kind="success",
            wrap=900,
        ).pack(fill="x", pady=(8, 0))

    # ==================================================================
    # 刷新
    # ==================================================================
    def _set_mail_status(self, text: str, *, error: bool = False, success: bool = False) -> None:
        color = "danger" if error else ("success" if success else "muted")
        self.mail_status_label.configure(text=text, fg=self.theme.color(color))

    def _set_external_status(self, text: str, *, error: bool = False, success: bool = False) -> None:
        color = "danger" if error else ("success" if success else "muted")
        self.external_status_label.configure(text=text, fg=self.theme.color(color))

    def refresh(self) -> None:
        self._refreshing = True
        try:
            settings = self.service.get_settings()
            accounts = self.service.list_accounts()
            self._conversations = list(self.service.list_conversations())
            self._settings = settings
            self._rebuild_accounts(accounts)
            self._sync_whitelist_accounts(accounts)
            self._reload_whitelist()
            self._sync_collection_controls(settings)
            self._sync_qq_experimental(settings)
            self._sync_ai_controls(settings)
            self._refresh_capability_status()
            self._refresh_qq_authorized_status()
            self._refresh_stats()
            self._refresh_consent_history()
            self._refresh_demo_state()
            self.error_banner.pack_forget()
        except Exception as exc:  # noqa: BLE001 - 初始化失败不应崩溃整个窗口
            self.error_banner.set_text(
                f"读取设置失败：{exc}。可以点「刷新统计」重试；如果一直失败，请重启程序。"
            )
            self.error_banner.pack(fill="x", padx=18, pady=(0, 8))
        finally:
            self._refreshing = False

    # ------------------------------ 账号 ------------------------------
    def _rebuild_accounts(self, accounts) -> None:
        clear_children(self.accounts_frame)
        self.account_rows = []
        if not accounts:
            hint(self.accounts_frame, self.theme, "还没有添加账号。可以在下面添加 QQ 或 QQ 邮箱账号。", bg=self.theme.color("card")).pack(fill="x")
            return
        for account in accounts:
            row = tk.Frame(self.accounts_frame, bg=self.theme.color("card"))
            row.pack(fill="x", pady=2)
            enabled_var = tk.IntVar(value=1 if account.enabled else 0)
            check = tk.Checkbutton(
                row,
                text="启用",
                variable=enabled_var,
                command=lambda account_id=account.id, var=enabled_var: self._set_account_enabled(account_id, var),
                font=self.theme.font("base"),
                bg=self.theme.color("card"),
                activebackground=self.theme.color("card"),
                anchor="w",
                cursor="hand2",
            )
            check.pack(side="left", padx=(0, 8))
            title = account.display_name
            if account.identifier:
                title = f"{account.display_name}（{account.identifier}）"
            label(
                row,
                self.theme,
                f"{title}　·　{account_kind_label(account.kind)}",
                kind="base",
                bg=self.theme.color("card"),
            ).pack(side="left")
            remove_button = big_button(
                row, self.theme, "移除", lambda account_id=account.id, name=account.display_name: self._remove_account(account_id, name)
            )
            remove_button.pack(side="right")
            self.account_rows.append(
                {"account": account, "frame": row, "enabled_var": enabled_var, "remove_button": remove_button}
            )

    def _set_account_enabled(self, account_id: str, enabled_var: tk.IntVar) -> None:
        if self._refreshing:
            return
        try:
            self.service.set_account_enabled(account_id, enabled_var.get() == 1)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "修改账号状态失败", f"这次修改保存失败：{exc}\n\n可以再试一次。")
        self.window.root.after_idle(self.refresh)

    def _remove_account(self, account_id: str, display_name: str) -> None:
        confirmed = notify.ask_confirm(
            self.window.root,
            "移除账号",
            f"确定要移除账号「{display_name}」吗？\n\n"
            "本程序会同时删除这个账号在本机保存的消息与整理结果；QQ 或邮箱服务器里的原始消息不会受影响"
            "（程序只读）。",
            confirm_text="移除账号",
            danger=True,
        )
        if not confirmed:
            return
        try:
            self.service.remove_account(account_id, delete_messages=True)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "移除账号失败", f"账号移除失败：{exc}\n\n可以再试一次。")
            return
        self.refresh()
        self.window.refresh_status()

    def _refresh_qq_authorized_status(self) -> None:
        """设置页明确显示当前已保存授权/会读取的会话，避免误解。"""
        if not hasattr(self, "qq_authorized_status"):
            return
        try:
            accounts = list(self.service.list_accounts())
            conversations = list(self.service.list_conversations())
        except Exception:  # noqa: BLE001
            self.qq_authorized_status.configure(text="当前授权范围暂时读取不到。")
            return
        qq_ids = {
            str(getattr(account, "id", ""))
            for account in accounts
            if str(getattr(getattr(account, "kind", None), "value", getattr(account, "kind", ""))) == "qq"
        }
        enabled = [
            str(getattr(conversation, "title", "") or "（无标题会话）")
            for conversation in conversations
            if str(getattr(conversation, "account_id", "")) in qq_ids
            and bool(getattr(conversation, "enabled", False))
        ]
        disabled = [
            str(getattr(conversation, "title", "") or "（无标题会话）")
            for conversation in conversations
            if str(getattr(conversation, "account_id", "")) in qq_ids
            and not bool(getattr(conversation, "enabled", False))
        ]
        lines = []
        if enabled:
            lines.append(f"✅ 已授权会读取：{'、'.join(enabled[:8])}" + ("…" if len(enabled) > 8 else ""))
        else:
            lines.append("还没有已授权的 QQ 会话。")
        if disabled:
            lines.append(f"⛔ 未授权/未勾选，不会读取：{len(disabled)} 个")
        self.qq_authorized_status.configure(text="\n".join(lines))

    def _sync_qq_experimental(self, settings) -> None:
        enabled = bool(getattr(settings, "qq_read_experimental", False))
        try:
            self.qq_experimental_var.set(enabled)
        except Exception:  # noqa: BLE001
            pass
        self.qq_experimental_status.configure(
            text=(
                "当前：已开启（仅只读白名单授权会话；扫描需 QQ 主窗口在前台；仍无任何发送能力）"
                if enabled
                else "当前：关闭（扫描不会读取 QQ，仍可正常读取邮箱）"
            ),
            fg=self.theme.color("success" if enabled else "muted"),
        )

    def toggle_qq_experimental(self) -> None:
        """切换实验性 QQ 客户端读取（P0-23）。失败时回滚勾选并明确提示。"""
        if getattr(self, "_refreshing", False):
            return
        enabled = bool(self.qq_experimental_var.get())
        try:
            settings = self.service.update_settings(qq_read_experimental=enabled)
        except Exception as exc:  # noqa: BLE001
            self._refreshing = True
            try:
                self.qq_experimental_var.set(not enabled)
            finally:
                self._refreshing = False
            self.qq_experimental_status.configure(
                text=f"保存失败（已回滚）：{exc}", fg=self.theme.color("danger")
            )
            notify.show_error(
                self.window.root,
                "切换 QQ 实验读取失败",
                f"这次设置保存失败：{exc}\n\n勾选状态已回滚；可以稍后重试，"
                "或检查是否启用了采集/同意读取 QQ。",
            )
            return
        self._sync_qq_experimental(settings)
        self.window.refresh_status()

    def _quick_qq_read(self) -> dict:
        """设置页的同一个一键读取入口：调用主窗口同一逻辑（含确认框）。"""
        result = self.window.quick_qq_read()
        if result.get("ok"):
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
        return result

    def _open_qq_session_picker(self) -> dict:
        """设置页的同一个 QQ 会话选择入口：调用主窗口同一逻辑。"""
        result = self.window.open_qq_session_picker()
        if result.get("applied"):
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
        return result

    def add_qq_account(self) -> None:
        name = self.qq_name_input.get().strip()
        if not name:
            notify.show_warning(self.window.root, "请先填写账号名称", "给这个 QQ 账号起个名字，例如「我的 QQ」，方便区分。")
            return
        allowlist = [part for part in re.split(r"[，,、;；\s]+", self.qq_allowlist_input.get()) if part.strip()]
        try:
            self.service.add_qq_account(name, allowlist)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "添加 QQ 账号失败", f"这次添加失败：{exc}\n\n可以检查名称后重试。")
            return
        self.qq_name_input.delete(0, "end")
        self.qq_allowlist_input.delete(0, "end")
        self.refresh()
        notify.show_info(
            self.window.root,
            "QQ 账号已添加",
            "账号已添加。接下来请：\n1. 在「允许读取的会话」里勾选要读取的会话；\n"
            "2. 确认已同意读取 QQ（首次使用时已确认）。\n\n程序只读，不会发送任何消息。",
        )

    # ------------------------------ 邮箱 ------------------------------
    def add_mail_account(self) -> None:
        email = self.mail_address_input.get().strip()
        if "@" not in email or email.endswith("@"):
            notify.show_warning(self.window.root, "邮箱地址不完整", "请输入完整的 QQ 邮箱地址，例如 12345678@qq.com。")
            return
        display_name = self.mail_name_input.get().strip() or email
        auth_code = self.mail_auth_code_input.get().strip()
        try:
            self.service.add_mail_account(email, display_name)
            if auth_code:
                self.service.set_mail_credential(email, auth_code)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "添加邮箱账号失败", f"这次添加失败：{exc}\n\n可以检查邮箱地址与授权码后重试。")
            return
        self.mail_auth_code_input.delete(0, "end")
        self.refresh()
        self._set_mail_status(
            text=f"已添加邮箱账号 {email}。" + ("授权码已保存到 Windows 凭据管理器。" if auth_code else "还没有保存授权码。"),
            success=bool(auth_code),
        )

    def save_mail_credential(self) -> None:
        email = self.mail_address_input.get().strip()
        code = self.mail_auth_code_input.get().strip()
        if "@" not in email:
            notify.show_warning(self.window.root, "请先填写邮箱地址", "在「添加 QQ 邮箱账号」里填写邮箱地址后再保存授权码。")
            return
        if not code:
            notify.show_warning(
                self.window.root,
                "还没有输入授权码",
                "请输入 16 位邮箱授权码（不是 QQ 密码）。如果不知道在哪里获取，点「什么是授权码？」查看步骤。",
            )
            return
        try:
            self.service.set_mail_credential(email, code)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "保存授权码失败", f"授权码保存失败：{exc}\n\n可以稍后重试；如果一直失败，请检查 Windows 凭据管理器是否可用。")
            return
        self.mail_auth_code_input.delete(0, "end")
        self._set_mail_status(
            text=f"授权码已保存到 Windows 凭据管理器（{email}），界面不会显示明文。", success=True
        )
        self._refresh_capability_status()

    def test_mail_credential(self) -> None:
        email = self.mail_address_input.get().strip()
        if "@" not in email:
            try:
                accounts = [a for a in self.service.list_accounts() if a.kind.value == "email"]
            except Exception:  # noqa: BLE001
                accounts = []
            if not accounts:
                notify.show_warning(self.window.root, "还没有邮箱账号", "请先填写邮箱地址并添加账号，然后才能测试连接。")
                return
            email = accounts[0].identifier or accounts[0].display_name
        try:
            check = self.service.test_mail_credential(email)
        except Exception as exc:  # noqa: BLE001
            self._set_mail_status(text=f"测试连接失败：{exc}", error=True)
            return
        if check.ok:
            latency = f"，耗时约 {check.latency_ms} 毫秒。" if check.latency_ms else "。"
            self._set_mail_status(text=f"连接成功（{email}）{latency}", success=True)
        else:
            self._set_mail_status(
                text=f"连接失败：{check.detail}。请检查授权码是否正确、邮箱是否已开启 IMAP 服务。",
                error=True,
            )

    def delete_mail_credential(self) -> None:
        email = self.mail_address_input.get().strip()
        if "@" not in email:
            notify.show_warning(self.window.root, "请先填写邮箱地址", "填写要清除授权码的邮箱地址。")
            return
        try:
            existed = self.service.has_mail_credential(email)
            self.service.delete_mail_credential(email)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "删除授权码失败", f"删除失败：{exc}")
            return
        self._set_mail_status(
            text=f"已删除 {email} 的授权码。" if existed else f"本机没有保存过 {email} 的授权码。"
        )
        self._refresh_capability_status()

    def toggle_auth_help(self) -> None:
        if self.auth_help_panel.winfo_ismapped():
            self.auth_help_panel.pack_forget()
            self.auth_help_button.configure(text="什么是授权码？点这里查看获取步骤")
        else:
            self.auth_help_panel.pack(fill="x", pady=(6, 0))
            self.auth_help_button.configure(text="收起授权码说明")

    # ------------------------------ 白名单 ------------------------------
    def _sync_whitelist_accounts(self, accounts) -> None:
        labels = [f"{account.display_name}（{account_kind_label(account.kind)}）" for account in accounts]
        self._whitelist_account_ids = [account.id for account in accounts]
        current = self.whitelist_account_combo.current()
        self.whitelist_account_combo.configure(values=labels)
        if labels:
            self.whitelist_account_combo.current(min(max(current, 0), len(labels) - 1))
        else:
            self.whitelist_account_combo.set("")

    def _current_whitelist_account(self) -> str:
        index = self.whitelist_account_combo.current()
        ids = getattr(self, "_whitelist_account_ids", [])
        if 0 <= index < len(ids):
            return ids[index]
        return ""

    def _reload_whitelist(self) -> None:
        clear_children(self.whitelist_frame)
        self.whitelist_rows = []
        account_id = self._current_whitelist_account()
        conversations = [c for c in self._conversations if not account_id or c.account_id == account_id]
        for conversation in conversations:
            row = tk.Frame(self.whitelist_frame, bg=self.theme.color("card"))
            row.pack(fill="x", pady=1)
            enabled_var = tk.IntVar(value=1 if conversation.enabled else 0)
            check = tk.Checkbutton(
                row,
                text="允许读取",
                variable=enabled_var,
                command=lambda conversation_id=conversation.id, var=enabled_var: self._on_whitelist_toggle(
                    conversation_id, var
                ),
                font=self.theme.font("base"),
                bg=self.theme.color("card"),
                activebackground=self.theme.color("card"),
                anchor="w",
                cursor="hand2",
            )
            check.pack(side="left")
            text = (
                f"{conversation.title}　·　{conversation_kind_label(conversation.kind)}　·　"
                f"优先级：{priority_label(conversation.priority)}"
            )
            if conversation.last_message_at is not None:
                text += f"　·　最近消息：{format_datetime(conversation.last_message_at)}"
            label(row, self.theme, text, kind="small", bg=self.theme.color("card")).pack(side="left", padx=(8, 0))
            self.whitelist_rows.append(
                {"conversation": conversation, "frame": row, "enabled_var": enabled_var}
            )
        if not conversations:
            self.whitelist_status_label.configure(
                text="还没有发现会话。可以点「刷新会话列表」；还没有添加账号时请先添加账号。"
            )
        else:
            enabled = sum(1 for c in conversations if c.enabled)
            self.whitelist_status_label.configure(
                text=f"共 {len(conversations)} 个会话，其中 {enabled} 个允许读取。"
            )

    def _on_whitelist_toggle(self, conversation_id: str, enabled_var: tk.IntVar) -> None:
        if self._refreshing:
            return
        enabled = enabled_var.get() == 1
        try:
            self.service.set_conversation_enabled(conversation_id, enabled)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "保存会话设置失败", f"这次修改保存失败：{exc}\n\n可以再试一次。")
            return
        self.whitelist_status_label.configure(text="已保存：这个会话" + ("允许读取。" if enabled else "不再读取。"))

    def refresh_conversations(self) -> None:
        account_id = self._current_whitelist_account()
        if not account_id:
            notify.show_warning(self.window.root, "还没有账号", "请先在上面的「账号」里添加 QQ 或邮箱账号。")
            return
        try:
            check = self.service.refresh_conversations(account_id)
            self._conversations = list(self.service.list_conversations())
        except Exception as exc:  # noqa: BLE001
            notify.show_error(
                self.window.root,
                "刷新会话列表失败",
                f"刷新失败：{exc}\n\n如果读取 QQ 会话，请确认 QQ 已登录并允许短暂切换前台。",
            )
            return
        self._reload_whitelist()
        if check.ok:
            discovered = check.discovered or {}
            count = discovered.get("conversations", len(self._conversations))
            self.whitelist_status_label.configure(text=f"已刷新会话列表，共发现 {count} 个会话。{check.detail}")
        else:
            self.whitelist_status_label.configure(text=f"刷新未完成：{check.detail}")

    # ------------------------------ 采集 ------------------------------
    def _sync_collection_controls(self, settings: AppSettings) -> None:
        paused = bool(settings.collection_paused)
        try:
            status = self.service.collection_status() or {}
        except Exception:  # noqa: BLE001
            status = {}
        paused = bool(status.get("paused", paused))
        self.pause_var.set(1 if paused else 0)
        self.interval_spin.delete(0, "end")
        self.interval_spin.insert(0, str(int(settings.auto_scan_interval_minutes or 0)))
        self.foreground_var.set(1 if settings.show_foreground_warning else 0)

        status_text = "已暂停（仍可手动生成摘要）" if paused else "运行中"
        last = status.get("last_scan_at")
        last_text = format_datetime(last, empty="从未读取") if last else "从未读取"
        last_status = status.get("last_scan_status")
        if last_status:
            try:
                last_text += f"（{scan_status_label(ScanStatus(str(last_status)))}）"
            except ValueError:
                last_text += f"（{last_status}）"
        self.collection_status_label.configure(text=f"采集状态：{status_text}　·　最近一次读取：{last_text}")

    def _on_pause_toggled(self) -> None:
        if self._refreshing:
            return
        paused = self.pause_var.get() == 1
        try:
            if paused:
                self.service.pause_collection()
            else:
                self.service.resume_collection()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "切换采集状态失败", f"这次切换失败：{exc}\n\n可以再试一次。")
            return
        try:
            self._settings = self.service.get_settings()
            self._sync_collection_controls(self._settings)
        except Exception:  # noqa: BLE001
            pass
        self.window.refresh_status()

    def save_collection_settings(self) -> None:
        try:
            interval = int(self.interval_spin.get() or 0)
            self.service.update_settings(
                auto_scan_interval_minutes=interval,
                show_foreground_warning=self.foreground_var.get() == 1,
            )
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "保存采集设置失败", f"设置保存失败：{exc}\n\n可以再试一次。")
            return
        self.window.set_status("已保存采集设置。")

    # ------------------------------ AI ------------------------------
    def _sync_ai_controls(self, settings: AppSettings) -> None:
        consented = bool(settings.external_ai_consent)
        self.ai_mode_var.set(1 if consented else 0)
        self.external_url_input.delete(0, "end")
        self.external_url_input.insert(0, settings.external_ai_base_url or "")
        self.external_model_input.delete(0, "end")
        self.external_model_input.insert(0, settings.external_ai_model or "")
        self._sync_ai_consent_widgets(consented)
        try:
            has_key = bool(self.service.has_external_ai_key())
        except Exception:  # noqa: BLE001
            has_key = False
        self._set_external_status(
            text="密钥状态：已保存到 Windows 凭据管理器。" if has_key else "密钥状态：还没有保存 API 密钥。"
        )

    def _sync_ai_consent_widgets(self, consented: bool) -> None:
        if consented:
            self.external_consent_label.configure(text="外部 AI 同意状态：已同意（可随时撤回）")
            self.grant_external_button.configure(state="disabled")
            self.revoke_external_button.configure(state="normal")
        else:
            self.external_consent_label.configure(text="外部 AI 同意状态：未同意（外部 AI 不可用）")
            self.grant_external_button.configure(state="normal")
            self.revoke_external_button.configure(state="disabled")

    def _on_ai_mode_changed(self) -> None:
        if self._refreshing:
            return
        if self.ai_mode_var.get() == 1:
            self._set_external_status(
                text="已选择：使用外部 AI。保存前请先点「同意外部 AI 上传…」。"
            )
        else:
            self._set_external_status(text="已选择：仅使用本机规则（不联网）。")

    def save_external_key(self) -> None:
        key = self.external_key_input.get().strip()
        if not key:
            notify.show_warning(self.window.root, "还没有输入 API 密钥", "请输入外部 AI 的 API Key。密钥只保存在 Windows 凭据管理器。")
            return
        try:
            self.service.set_external_ai_key(key)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "保存密钥失败", f"密钥保存失败：{exc}\n\n可以稍后重试；如果一直失败，请检查 Windows 凭据管理器是否可用。")
            return
        self.external_key_input.delete(0, "end")
        self._set_external_status(text="密钥已保存到 Windows 凭据管理器，界面不会显示明文。", success=True)
        self._refresh_capability_status()

    def test_external_ai(self) -> None:
        try:
            check = self.service.test_external_ai()
        except Exception as exc:  # noqa: BLE001
            self._set_external_status(text=f"测试连接失败：{exc}", error=True)
            return
        if check.ok:
            self._set_external_status(text="外部 AI 连接成功。" + check.detail, success=True)
        else:
            self._set_external_status(
                text=f"外部 AI 连接失败：{check.detail}。请检查服务地址、模型名称与密钥。",
                error=True,
            )

    def grant_external_consent(self) -> None:
        settings = self._settings or self.service.get_settings()
        dialog = ExternalAIUploadDialog(
            self.window,
            model_label=settings.external_ai_model,
            endpoint=settings.external_ai_base_url,
            message_count=1,
            include_context=True,
        )
        if not dialog.show():
            self._set_external_status(text="已取消，外部 AI 仍未启用。")
            return
        try:
            self.service.grant_consent(
                ConsentScope.EXTERNAL_AI,
                confirmation_text="设置页确认同意外部 AI 上传（每次生成前仍会再次确认）",
            )
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "同意外部 AI 失败", f"操作失败：{exc}\n\n可以再试一次。")
            return
        self.refresh()
        self._set_external_status(text="已同意外部 AI 上传。每次生成草稿前仍会再次弹窗确认。", success=True)

    def revoke_external_consent(self) -> None:
        confirmed = notify.ask_confirm(
            self.window.root,
            "撤回外部 AI 同意",
            "撤回后，外部 AI 将无法再生成草稿，已生成的本机草稿不受影响。\n\n确定要撤回吗？",
            confirm_text="撤回同意",
        )
        if not confirmed:
            return
        try:
            self.service.revoke_consent(ConsentScope.EXTERNAL_AI)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "撤回同意失败", f"操作失败：{exc}\n\n可以再试一次。")
            return
        self.refresh()
        self._set_external_status(text="已撤回外部 AI 同意。现在只能使用本机规则生成草稿。")

    def save_ai_settings(self) -> None:
        settings = self._settings or self.service.get_settings()
        if self.ai_mode_var.get() == 1:
            if not settings.external_ai_consent:
                notify.show_warning(
                    self.window.root,
                    "外部 AI 还没有获得你的同意",
                    "你选择了使用外部 AI，但还没有同意上传。为保护隐私，这次保存已被阻止。\n\n"
                    "请先点「同意外部 AI 上传…」，阅读上传范围并确认；或者改回「仅使用本机规则」。",
                )
                self._set_external_status(text="保存被阻止：外部 AI 尚未同意。", error=True)
                return
            try:
                self.service.update_settings(
                    external_ai_base_url=self.external_url_input.get().strip(),
                    external_ai_model=self.external_model_input.get().strip(),
                )
            except Exception as exc:  # noqa: BLE001
                notify.show_error(self.window.root, "保存 AI 设置失败", f"设置保存失败：{exc}\n\n可以再试一次。")
                return
            self.window.set_status("已保存 AI 设置（外部 AI）。")
            self._set_external_status(text="已保存：外部 AI 已启用，每次生成前仍会确认上传范围。", success=True)
            self._refresh_capability_status()
            return
        try:
            self.service.update_settings(
                external_ai_base_url=self.external_url_input.get().strip(),
                external_ai_model=self.external_model_input.get().strip(),
            )
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "保存 AI 设置失败", f"设置保存失败：{exc}\n\n可以再试一次。")
            return
        self.window.set_status("已保存 AI 设置（本机规则）。")
        self._set_external_status(text="已保存：仅使用本机规则，不联网。", success=True)
        self._refresh_capability_status()

    # ------------------------------ 隐私 ------------------------------
    def _refresh_stats(self) -> None:
        try:
            stats = self.service.get_storage_stats() or {}
        except Exception as exc:  # noqa: BLE001
            self.storage_stats_label.configure(text=f"读取本机数据统计失败：{exc}")
            return
        lines = [
            f"本机数据：消息 {stats.get('message_count', 0)} 条　·　会话 {stats.get('conversation_count', 0)} 个　·　"
            f"待办 {stats.get('todo_count', 0)} 条　·　草稿 {stats.get('draft_count', 0)} 条",
            f"数据库大小：{format_bytes(stats.get('db_bytes', 0))}",
        ]
        if stats.get("data_dir"):
            lines.append(f"数据目录：{stats['data_dir']}")
        self.storage_stats_label.configure(text="\n".join(lines))

    def _refresh_consent_history(self) -> None:
        try:
            history = self.service.list_consent_history() or []
        except Exception:  # noqa: BLE001
            history = []
        self.consent_history_list.delete(0, "end")
        if not history:
            self.consent_history_list.insert("end", "还没有同意记录。")
            return
        for entry in history:
            self.consent_history_list.insert("end", self._consent_entry_text(entry))

    @staticmethod
    def _consent_entry_text(entry: dict) -> str:
        scope = str(entry.get("scope") or "")
        scope_label = _SCOPE_LABELS.get(scope, scope or "未知范围")
        when = entry.get("granted_at") or entry.get("at") or entry.get("timestamp")
        when_text = format_datetime(when, empty="时间未知") if when else "时间未知"
        action = str(entry.get("action") or "已记录")
        if action in {"granted", "grant"}:
            action = "已同意"
        elif action in {"revoked", "revoke"}:
            action = "已撤回"
        text = f"{when_text}　·　{scope_label}　·　{action}"
        note = entry.get("confirmation_text")
        if note:
            text += f"　·　{note}"
        return text

    def _refresh_demo_state(self) -> None:
        try:
            active = bool(self.service.is_demo_active())
        except Exception:  # noqa: BLE001
            active = False
        self.demo_status_label.configure(
            text="当前已加载演示数据：界面里是合成示例，不含任何真实聊天内容。"
            if active
            else "当前没有加载演示数据。"
        )

    def load_demo_data(self) -> None:
        try:
            self.service.load_demo_data()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "加载演示数据失败", f"加载失败：{exc}\n\n可以再试一次。")
            return
        self.refresh()
        self.window.refresh_all()
        notify.show_info(
            self.window.root,
            "已加载演示数据",
            "界面里现在显示的是程序生成的合成示例（重要 / 普通 / 仅供了解、待办、草稿都有示例），"
            "不含任何真实聊天内容。可以在「今日摘要」页点「生成今日摘要」体验。",
        )

    def clear_demo_data(self) -> None:
        confirmed = notify.ask_confirm(
            self.window.root,
            "清除演示数据",
            "将删除本机上的演示消息、待办与草稿。确定要清除吗？",
            confirm_text="清除演示数据",
        )
        if not confirmed:
            return
        try:
            self.service.clear_demo_data()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "清除演示数据失败", f"清除失败：{exc}\n\n可以再试一次。")
            return
        self.refresh()
        self.window.refresh_all()
        self.demo_status_label.configure(text="演示数据已清除。")

    def export_diagnostics(self) -> str | None:
        try:
            path = self.service.export_diagnostics()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "导出诊断信息失败", f"导出失败：{exc}\n\n可以稍后重试；如果一直失败，请重启程序再来。")
            return None
        notify.show_info(
            self.window.root,
            "诊断信息已导出",
            f"已导出到：\n{path}\n\n诊断文件只包含运行计数、版本与错误信息，不包含消息正文、授权码或 AI 密钥。"
            "可以把文件发给帮你排查的人。",
        )
        return str(path)

    def delete_all_data(self) -> None:
        dialog = ConfirmDeleteDataDialog(self.window)
        dialog.show()
        if not dialog.confirmed:
            return
        try:
            self.service.delete_all_local_data()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.window.root, "删除本机数据失败", f"删除失败：{exc}\n\n可以重试；如果一直失败，请重启程序后再试。")
            return
        self.refresh()
        self.window.refresh_all()
        notify.show_info(
            self.window.root,
            "本机数据已删除",
            "本机保存的消息、待办、草稿与采集记录已删除。\n\n"
            "QQ 与邮箱服务器里的原始消息不受影响（本程序只读，无法改动它们）。",
        )


__all__ = ["SettingsPage"]
