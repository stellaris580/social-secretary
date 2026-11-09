"""Tkinter 模态对话框：首次运行同意向导、前台读取警告、删除数据确认、
外部 AI 上传确认、来源详情、消息详情。

安全相关约定（不可放宽）：
- 本项目没有任何「发送 QQ 消息 / 发送邮件」的界面或代码路径；
- 外部 AI 上传、删除本机数据都必须经过这里明确的二次确认；
- 前台读取 QQ 会打断用户操作，必须提前警告并可取消。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..contracts import (
    APP_NAME,
    APP_VERSION,
    ConsentScope,
    Message,
    SourceDetail,
)
from . import notify
from .formatting import (
    account_kind_label,
    category_hint,
    category_label,
    format_datetime,
    sender_label,
    timestamp_source_label,
    timestamp_source_note,
)
from .widgets import Banner, Card, big_button, hint, label, make_scrollable
from .theme import Theme

__all__ = [
    "ConfirmDeleteDataDialog",
    "ConsentWizard",
    "ExternalAIUploadDialog",
    "ForegroundWarningDialog",
    "MailConnectWizard",
    "MessageDetailDialog",
    "QQSessionPickerDialog",
    "QuickQQReadDialog",
    "SemanticSummaryDialog",
    "SourceDetailDialog",
    "about_text",
]

_CONSENT_ITEMS: tuple[tuple[str, str, ConsentScope | None], ...] = (
    (
        "只读声明：我只查看，不会替你发送任何消息",
        "本程序不能发送 QQ 消息、不能发送邮件、不能删除或修改任何聊天记录。"
        "所有草稿都要你自己复制后手动发送。",
        None,
    ),
    (
        "读取 QQ：只读你勾选的会话",
        "允许读取你稍后在「设置与隐私」中勾选的 QQ 会话。读取时可能短暂切换到 QQ 前台窗口，"
        "会打断你正在进行的电脑操作。",
        ConsentScope.QQ_READ,
    ),
    (
        "读取邮箱：只读你配置的 QQ 邮箱",
        "允许通过 IMAP 只读方式读取你配置的 QQ 邮箱。登录用的「授权码」不是 QQ 密码，"
        "是邮箱为第三方客户端生成的 16 位专用密码（获取步骤见设置页说明）。"
        "授权码由你本人在本机输入，只保存在 Windows 凭据管理器。",
        ConsentScope.MAIL_READ,
    ),
    (
        "数据只存在本机，默认不联网",
        "整理结果保存在这台电脑上，默认不联网、不上传到任何外部服务。"
        "程序日志不会记录消息正文、授权码或 AI 密钥。",
        None,
    ),
    (
        "外部 AI 需要你再次单独同意",
        "以后如果你想让外部 AI 帮忙写回复草稿，必须在设置里单独同意；"
        "每次上传前还会再次弹出确认框，显示将上传的内容范围。",
        None,
    ),
)


class ModalDialog(tk.Toplevel):
    """模态对话框基类：居中、置顶、Esc 取消、show() 阻塞直到关闭。"""

    def __init__(self, window, title: str, *, width: int = 640, height: int | None = None) -> None:
        super().__init__(window.root)
        self.window = window
        self.theme: Theme = window.theme
        self.confirmed = False
        self.title(title)
        self.configure(bg=self.theme.color("bg"))
        self.resizable(False, False)
        self.transient(window.root)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self._width = width
        if height is not None:
            self.geometry(f"{width}x{height}")
        self.body = tk.Frame(self, bg=self.theme.color("bg"))
        self.body.pack(fill="both", expand=True, padx=20, pady=18)
        self._built = False

    # -- 生命周期 -----------------------------------------------------
    def _place_center(self) -> None:
        self.update_idletasks()
        parent = self.window.root
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 3)
        self.geometry(f"+{x}+{y}")

    def show(self) -> bool:
        self._place_center()
        self.grab_set()
        self.wait_window(self)
        return self.confirmed

    def _accept(self) -> None:
        self.confirmed = True
        self.destroy()

    def _cancel(self) -> None:
        self.confirmed = False
        self.destroy()

    def destroy(self) -> None:  # noqa: D401 - Qt/Tk 生命周期
        # 解除滚轮全局绑定，避免影响主窗口/其他弹窗
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    def _footer(self, buttons: list[tk.Button]) -> tk.Frame:
        footer = tk.Frame(self.body, bg=self.theme.color("bg"))
        footer.pack(fill="x", side="bottom", pady=(14, 0))
        for button in reversed(buttons):
            button.pack(side="right", padx=(8, 0))
        return footer


# ---------------------------------------------------------------------------
# 首次运行同意向导
# ---------------------------------------------------------------------------

class ConsentWizard(ModalDialog):
    """首次运行逐项勾选同意；全部勾选后「同意并开始使用」才可用。"""

    def __init__(self, window) -> None:
        super().__init__(window, f"{APP_NAME} · 首次使用确认", width=760, height=760)
        theme = self.theme
        label(self.body, theme, "开始使用前，请先了解这几件事", kind="h1", bg=theme.color("bg")).pack(
            fill="x", pady=(0, 4)
        )
        hint(
            self.body,
            theme,
            "AI 社交秘书是一个只读的消息整理工具。下面 5 项请你逐条阅读并勾选，"
            "全部勾选后才能读取消息。以后可以在「设置与隐私」中随时撤回同意。",
            wrap=690,
            bg=theme.color("bg"),
        ).pack(fill="x", pady=(0, 10))

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", side="bottom", pady=(12, 0))
        self.accept_button = big_button(self.body, theme, "同意并开始使用", self._accept, kind="primary")
        self.accept_button.configure(state="disabled")
        self.accept_button.pack(in_=footer, side="right", padx=(8, 0))
        self.reject_button = big_button(footer, theme, "暂不同意", self._cancel)
        self.reject_button.pack(side="right")
        hint(
            footer,
            theme,
            "全部勾选后才能开始读取；暂不同意不会读取任何消息。",
            wrap=300,
            bg=theme.color("bg"),
        ).pack(side="left")

        self._scroll_frame = self._make_scroll_area()
        self.checks: list[tk.Checkbutton] = []
        self._check_vars: list[tk.IntVar] = []
        for index, (title, detail, scope) in enumerate(_CONSENT_ITEMS, start=1):
            item = Card(self._scroll_frame, theme, padding=12)
            item.pack(fill="x", pady=6)
            label(item.body, theme, f"{index}. {title}", kind="bold").pack(fill="x", anchor="w")
            tk.Label(
                item.body,
                text=detail,
                font=theme.font("small"),
                fg=theme.color("muted"),
                bg=theme.color("card"),
                justify="left",
                anchor="w",
                wraplength=640,
            ).pack(fill="x", anchor="w", pady=(4, 6))
            var = tk.IntVar(value=0)
            check = tk.Checkbutton(
                item.body,
                text="我已阅读并同意以上内容",
                variable=var,
                command=self._update_accept_state,
                font=theme.font("base"),
                bg=theme.color("card"),
                activebackground=theme.color("card"),
                anchor="w",
                cursor="hand2",
            )
            check.pack(fill="x", anchor="w")
            check._consent_scope = scope.value if scope else ""  # type: ignore[attr-defined]
            check._var = var  # type: ignore[attr-defined]
            self.checks.append(check)
            self._check_vars.append(var)

    def _make_scroll_area(self) -> tk.Frame:
        theme = self.theme
        canvas = tk.Canvas(self.body, bg=theme.color("bg"), highlightthickness=0, bd=0, height=400)
        scrollbar = tk.Scrollbar(self.body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=theme.color("bg"))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _configure_inner(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _configure_canvas(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        inner.bind("<Configure>", _configure_inner)
        canvas.bind("<Configure>", _configure_canvas)

        def _on_mousewheel(event) -> None:
            delta = int(-event.delta / 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        # 鼠标滚轮在整个弹窗范围内都可滚动（非程序员更容易发现/翻阅 5 条同意项）。
        self.bind_all("<MouseWheel>", _on_mousewheel, add="+")
        self._wheel_handler = _on_mousewheel  # 保留引用，关闭时解绑
        self._scroll_canvas = canvas
        return inner

    # -- 状态 ---------------------------------------------------------
    def is_fully_confirmed(self) -> bool:
        return all(var.get() == 1 for var in self._check_vars)

    def _update_accept_state(self) -> None:
        self.accept_button.configure(state="normal" if self.is_fully_confirmed() else "disabled")

    @property
    def selected_consents(self) -> list[ConsentScope]:
        scopes: list[ConsentScope] = []
        for check in self.checks:
            raw = getattr(check, "_consent_scope", "")
            if raw and check._var.get() == 1:  # type: ignore[attr-defined]
                scopes.append(ConsentScope(str(raw)))
        return scopes

    def set_all_checked(self, checked: bool = True) -> None:
        for var in self._check_vars:
            var.set(1 if checked else 0)
        self._update_accept_state()


# ---------------------------------------------------------------------------
# 前台读取警告（倒计时、可取消）
# ---------------------------------------------------------------------------

class ForegroundWarningDialog(ModalDialog):
    """读取 QQ 前的前台警告：默认倒计时 5 秒，期间「开始读取」不可用。"""

    def __init__(self, window, *, seconds: int = 5) -> None:
        super().__init__(window, "即将短暂占用 QQ 前台窗口", width=580)
        theme = self.theme
        self._countdown = max(0, int(seconds))
        self.confirmed = False

        label(self.body, theme, "接下来会短暂打开 QQ 并切换窗口", kind="h2", bg=theme.color("bg")).pack(
            fill="x", pady=(0, 8)
        )
        Banner(
            self.body,
            theme,
            "读取期间请先放下鼠标和键盘，避免误操作。程序只是读取你勾选的会话，"
            "不会发送任何消息、不会修改任何聊天记录。",
            wrap=520,
        ).pack(fill="x", pady=(0, 8))
        tk.Label(
            self.body,
            text="如果现在不方便（例如正在开会、正在打字），请点「取消（不读取）」，稍后再试。",
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("bg"),
            justify="left",
            anchor="w",
            wraplength=520,
        ).pack(fill="x", pady=(0, 8))
        self.countdown_label = hint(self.body, theme, "", wrap=520, bg=theme.color("bg"))
        self.countdown_label.pack(fill="x")

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(16, 0))
        self.confirm_button = big_button(footer, theme, "开始读取", self._accept, kind="primary")
        self.confirm_button.pack(side="right", padx=(8, 0))
        cancel_button = big_button(footer, theme, "取消（不读取）", self._cancel)
        cancel_button.pack(side="right")

        if self._countdown > 0:
            self.confirm_button.configure(state="disabled")
            self.after(1000, self._tick)
        self._update_texts()

    def _update_texts(self) -> None:
        if self._countdown > 0:
            self.countdown_label.configure(
                text=f"「开始读取」按钮将在 {self._countdown} 秒后可用，请先确认手头操作已保存。"
            )
            self.confirm_button.configure(text=f"开始读取（{self._countdown}）")
        else:
            self.countdown_label.configure(text="确认手头操作已保存后，点「开始读取」。")
            self.confirm_button.configure(text="开始读取", state="normal")

    def _tick(self) -> None:
        if self._countdown > 0:
            self._countdown -= 1
        self._update_texts()
        if self._countdown > 0:
            self.after(1000, self._tick)


# ---------------------------------------------------------------------------
# 一键读取当前 QQ 聊天（实验）确认（P0-25）
# ---------------------------------------------------------------------------

class QuickQQReadDialog(ModalDialog):
    """一键读取当前 QQ 聊天的确认框（默认人工确认；测试可 auto_confirm）。"""

    def __init__(self, window, *, auto_confirm: bool = False) -> None:
        super().__init__(window, "读取 QQ 当前聊天（实验）", width=640)
        theme = self.theme
        self.auto_confirm = bool(auto_confirm)

        label(
            self.body, theme, "只读取你当前在 QQ 打开的那个聊天", kind="h2", bg=theme.color("bg")
        ).pack(fill="x", pady=(0, 8))
        Banner(
            self.body,
            theme,
            "确认后会只读读取当前打开的 QQ 聊天；不会发送任何消息、不会修改 QQ，也没有任何发送能力。",
            wrap=560,
            kind="warning",
        ).pack(fill="x", pady=(0, 8))
        hint(
            self.body,
            theme,
            "读取时可能需要短暂占用 QQ 前台窗口，可能打断你正在进行的电脑操作；"
            "读取结果只保存到本机，不联网。QQ 读取仍为实验性：可能遗漏、重复保存，"
            "无法可靠确认每条记录都是独立的新消息；图片/文件/表情可能不完整，"
            "QQ 隐藏或托盘时可能读取失败。重要通知请以原始 QQ 客户端为准。\n\n"
            "操作建议：先在 QQ 里打开你要读取的聊天窗口，再点「确认读取（只读）」。",
            wrap=560,
            bg=theme.color("bg"),
        ).pack(fill="x", pady=(0, 8))

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(14, 0))
        self.confirm_button = big_button(
            footer, theme, "确认读取（只读）", self._accept, kind="primary"
        )
        self.confirm_button.pack(side="right", padx=(8, 0))
        big_button(footer, theme, "取消（不读取）", self._cancel).pack(side="right")

    def show(self) -> bool:
        if self.auto_confirm:
            self.confirmed = True
            self.destroy()
            return True
        return super().show()


# ---------------------------------------------------------------------------
# 删除本机数据（双重确认）
# ---------------------------------------------------------------------------

class ConfirmDeleteDataDialog(ModalDialog):
    """删除全部本机数据：勾选「无法恢复」+ 手动输入「删除」双重确认。"""

    CONFIRM_WORD = "删除"

    def __init__(self, window) -> None:
        super().__init__(window, "删除全部本机数据", width=620)
        theme = self.theme
        self.confirmed = False

        label(self.body, theme, "确定要删除这台电脑上的全部数据吗？", kind="h2", bg=theme.color("bg")).pack(
            fill="x", pady=(0, 8)
        )
        Banner(
            self.body,
            theme,
            "将要删除：本机保存的消息与分类结果、待办、回复草稿、采集记录、同意记录、诊断导出文件。\n"
            "不会删除：QQ 与邮箱服务器里的原始消息（本程序只读，无法改动它们）。",
            wrap=560,
        ).pack(fill="x", pady=(0, 8))
        tk.Label(
            self.body,
            text="删除后无法恢复，请确认你不再需要这些整理结果。",
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("bg"),
            anchor="w",
            justify="left",
            wraplength=560,
        ).pack(fill="x", pady=(0, 10))

        self._check_var = tk.IntVar(value=0)
        self.check = tk.Checkbutton(
            self.body,
            text="我已了解，删除后无法恢复",
            variable=self._check_var,
            command=self._update_state,
            font=theme.font("base"),
            bg=theme.color("bg"),
            activebackground=theme.color("bg"),
            anchor="w",
            cursor="hand2",
        )
        self.check.pack(fill="x", anchor="w")

        self._type_var = tk.StringVar(value="")
        self._type_var.trace_add("write", lambda *_args: self._update_state())
        self.type_input = tk.Entry(
            self.body, textvariable=self._type_var, font=theme.font("base"), width=44
        )
        self.type_input.pack(fill="x", pady=(8, 0))
        hint(
            self.body,
            theme,
            f"请输入「{self.CONFIRM_WORD}」两个字以确认",
            bg=theme.color("bg"),
        ).pack(fill="x")

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(16, 0))
        self.delete_button = big_button(footer, theme, "永久删除", self._accept, kind="danger")
        self.delete_button.configure(state="disabled")
        self.delete_button.pack(side="right", padx=(8, 0))
        big_button(footer, theme, "取消", self._cancel).pack(side="right")
        self._update_state()

    def _update_state(self) -> None:
        ready = self._check_var.get() == 1 and self._type_var.get().strip() == self.CONFIRM_WORD
        self.delete_button.configure(state="normal" if ready else "disabled")


# ---------------------------------------------------------------------------
# 外部 AI 上传确认
# ---------------------------------------------------------------------------

class ExternalAIUploadDialog(ModalDialog):
    """外部 AI 上传前的确认框：明确列出将上传的内容范围。"""

    def __init__(
        self,
        window,
        *,
        model_label: str = "",
        endpoint: str = "",
        message_count: int = 1,
        include_context: bool = True,
    ) -> None:
        super().__init__(window, "确认上传到外部 AI", width=660)
        theme = self.theme
        self.consent_granted = False
        endpoint_text = endpoint or "未填写服务地址"
        model_text = model_label or "未填写模型"
        count = max(1, int(message_count))

        label(self.body, theme, "外部 AI 需要上传消息内容，请先确认", kind="h2", bg=theme.color("bg")).pack(
            fill="x", pady=(0, 8)
        )
        Banner(
            self.body,
            theme,
            "以下内容会通过网络发送到你配置的外部 AI 服务商。上传的内容可能被对方记录，"
            "请确认你同意后再继续。外部 AI 生成的草稿只保存在本机，程序不会发送到 QQ 或邮箱。",
            wrap=590,
        ).pack(fill="x", pady=(0, 10))

        scope_lines = [
            f"将上传（共 {count} 条消息）：",
            f"· 你选中的 {count} 条消息正文",
            "· 发送者昵称与会话标题",
        ]
        if include_context:
            scope_lines.append("· 最近几条上下文消息（用于理解语气，最多 5 条）")
        scope_lines.extend(
            [
                f"· 服务地址：{endpoint_text}",
                f"· 模型：{model_text}",
                "",
                "不会上传：邮箱授权码、AI 密钥、账号密码、其他会话内容。",
            ]
        )
        tk.Label(
            self.body,
            text="\n".join(scope_lines),
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("bg"),
            justify="left",
            anchor="w",
            wraplength=590,
        ).pack(fill="x", pady=(0, 10))

        self._check_var = tk.IntVar(value=0)
        self.check = tk.Checkbutton(
            self.body,
            text="我同意本次把以上内容发送到外部 AI 服务",
            variable=self._check_var,
            command=self._update_state,
            font=theme.font("base"),
            bg=theme.color("bg"),
            activebackground=theme.color("bg"),
            anchor="w",
            cursor="hand2",
        )
        self.check.pack(fill="x", anchor="w")

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(16, 0))
        self.confirm_button = big_button(footer, theme, "同意并生成草稿", self._accept, kind="primary")
        self.confirm_button.configure(state="disabled")
        self.confirm_button.pack(side="right", padx=(8, 0))
        big_button(footer, theme, "取消（不上传）", self._cancel).pack(side="right")

    def _update_state(self) -> None:
        self.confirm_button.configure(state="normal" if self._check_var.get() == 1 else "disabled")

    def _accept(self) -> None:
        self.consent_granted = True
        super()._accept()


# ---------------------------------------------------------------------------
# 来源详情
# ---------------------------------------------------------------------------

class SourceDetailDialog(ModalDialog):
    """「查看原始来源」：账号、会话、时间来源、原始时间文本与定位（不含凭据）。"""

    def __init__(self, window, detail: SourceDetail) -> None:
        super().__init__(window, "消息来源详情", width=720)
        theme = self.theme
        self.detail = detail
        self.value_labels: dict[str, tk.Label] = {}

        label(self.body, theme, "这条消息是从哪里读到的？", kind="h2", bg=theme.color("bg")).pack(
            fill="x", pady=(0, 4)
        )
        hint(
            self.body,
            theme,
            "下面的信息只用于核对消息来源，不包含密码或授权码。"
            "如果定位信息看起来不对，可以到「设置与隐私」中暂停采集。",
            wrap=660,
            bg=theme.color("bg"),
        ).pack(fill="x", pady=(0, 10))

        rows: list[tuple[str, str]] = [
            ("账号类型", account_kind_label(detail.account_kind)),
            ("会话", detail.conversation_title),
            ("发送者", detail.sender),
            ("时间来源", timestamp_source_label(detail.timestamp_source)),
            ("原始时间文本", detail.raw_time_text or "（界面没有显示时间）"),
            ("读取时刻", format_datetime(detail.collected_at)),
            ("来源定位", detail.origin_label),
            ("详细定位", detail.locator or "（无）"),
            ("原始编号", detail.external_id or "（无）"),
        ]
        grid = tk.Frame(self.body, bg=theme.color("bg"))
        grid.pack(fill="x")
        for row_index, (key, value) in enumerate(rows):
            tk.Label(
                grid,
                text=key,
                font=theme.font("small"),
                fg=theme.color("muted"),
                bg=theme.color("bg"),
                anchor="w",
                width=12,
            ).grid(row=row_index, column=0, sticky="nw", pady=3)
            value_label = tk.Label(
                grid,
                text=value,
                font=theme.font("base"),
                fg=theme.color("text"),
                bg=theme.color("bg"),
                anchor="w",
                justify="left",
                wraplength=520,
            )
            value_label.grid(row=row_index, column=1, sticky="nw", pady=3)
            self.value_labels[key] = value_label

        note = timestamp_source_note(detail.timestamp_source)
        if note:
            Banner(self.body, theme, note, kind="info", wrap=650).pack(fill="x", pady=(10, 0))

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(14, 0))
        big_button(footer, theme, "关闭", self._cancel).pack(side="right")


# ---------------------------------------------------------------------------
# 消息详情
# ---------------------------------------------------------------------------

class MessageDetailDialog(ModalDialog):
    """单条消息详情：完整正文、分类与理由，可继续查看原始来源。"""

    def __init__(self, window, message: Message, service=None) -> None:
        super().__init__(window, "消息详情", width=720)
        theme = self.theme
        self.message = message
        self._service = service if service is not None else window.service
        self.source_dialog: SourceDetailDialog | None = None

        header = tk.Frame(self.body, bg=theme.color("bg"))
        header.pack(fill="x", pady=(0, 6))
        label(header, theme, message.conversation_title, kind="h2", bg=theme.color("bg")).pack(side="left")
        label(header, theme, category_label(message.category), kind="base", bg=theme.color("bg")).pack(
            side="right"
        )
        meta = (
            f"发送者：{sender_label(message.sender, message.is_self)}　·　"
            f"时间：{format_datetime(message.timestamp)}　·　"
            f"分类：{category_label(message.category)}（{category_hint(message.category)}）"
        )
        hint(self.body, theme, meta, wrap=660, bg=theme.color("bg")).pack(fill="x", pady=(0, 8))

        text_box = tk.Text(
            self.body,
            height=9,
            font=theme.font("base"),
            wrap="word",
            bg="#FFFFFF",
            fg=theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
            padx=10,
            pady=8,
        )
        text_box.insert("1.0", message.text or "（空消息）")
        text_box.configure(state="disabled")
        text_box.pack(fill="both", expand=True, pady=(0, 8))

        reasons = (
            "分类理由：" + "；".join(message.category_reasons)
            if message.category_reasons
            else "分类理由：本地规则未给出额外说明。"
        )
        hint(self.body, theme, reasons, wrap=660, bg=theme.color("bg")).pack(fill="x")

        draft_card = Card(self.body, theme, "AI 草稿（语义，实验；只读，绝不发送）")
        draft_card.pack(fill="x", pady=(10, 0))
        style_row = tk.Frame(draft_card.body, bg=theme.color("card"))
        style_row.pack(fill="x")
        hint(style_row, theme, "风格：", bg=theme.color("card")).pack(side="left")
        self.draft_style_combo = ttk.Combobox(
            style_row,
            values=["正式", "简洁", "友好"],
            state="readonly",
            width=8,
            font=theme.font("base"),
        )
        self.draft_style_combo.current(2)  # 默认友好
        self.draft_style_combo.pack(side="left", padx=(0, 10))
        self.draft_button = big_button(
            style_row, theme, "生成 AI 草稿", self.generate_semantic_draft, kind="primary"
        )
        self.draft_button.pack(side="left")
        self.draft_copy_button = big_button(style_row, theme, "复制草稿", self._copy_draft)
        self.draft_copy_button.configure(state="disabled")
        self.draft_copy_button.pack(side="left", padx=(8, 0))
        self.draft_provider_label = hint(draft_card.body, theme, "生成方式：未生成", wrap=660, bg=theme.color("card"))
        self.draft_provider_label.pack(fill="x", pady=(6, 0))
        self.draft_uncertainty_label = hint(draft_card.body, theme, "", wrap=660, bg=theme.color("card"))
        self.draft_uncertainty_label.pack(fill="x")
        draft_text_wrap = tk.Frame(draft_card.body, bg=theme.color("card"))
        draft_text_wrap.pack(fill="x", pady=(6, 0))
        draft_scroll = ttk.Scrollbar(draft_text_wrap, orient="vertical")
        draft_scroll.pack(side="right", fill="y")
        self.draft_text = tk.Text(
            draft_text_wrap,
            height=5,
            font=theme.font("base"),
            wrap="word",
            bg="#FFFFFF",
            fg=theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
            padx=10,
            pady=8,
            yscrollcommand=draft_scroll.set,
        )
        self.draft_text.pack(side="left", fill="both", expand=True)
        draft_scroll.configure(command=self.draft_text.yview)
        self.draft_status_label = hint(draft_card.body, theme, "", wrap=660, bg=theme.color("card"))
        self.draft_status_label.pack(fill="x", pady=(6, 0))
        if self._service is None or not hasattr(self._service, "generate_semantic_draft"):
            self.draft_button.configure(state="disabled")
            self.draft_status_label.configure(
                text="AI 草稿功能还没有就绪（等待核心层实现）；可先用「回复草稿」页的本机规则。"
            )

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(14, 0))
        self.source_button = big_button(footer, theme, "查看原始来源", self._open_source)
        self.source_button.pack(side="left")
        big_button(footer, theme, "关闭", self._cancel).pack(side="right")
        if self._service is None:
            self.source_button.configure(state="disabled")

    def _open_source(self) -> None:
        if self._service is None:
            return
        try:
            detail = self._service.get_source_detail(self.message.id)
        except Exception as exc:  # noqa: BLE001 - 界面必须给出可操作提示
            notify.show_error(
                self,
                "读取来源详情失败",
                f"暂时读不到这条消息的来源信息：{exc}\n\n可以稍后重试；如果一直失败，请到「设置与隐私」导出诊断信息。",
            )
            return
        self.source_dialog = SourceDetailDialog(self.window, detail)
        self.source_dialog.show()

    def generate_semantic_draft(self) -> dict | None:
        """AI 语义草稿（默认本地规则；只读、绝不发送）。"""
        if self._service is None or not hasattr(self._service, "generate_semantic_draft"):
            self.draft_status_label.configure(
                text="AI 草稿功能还没有就绪（等待核心层实现）。",
                fg=self.theme.color("danger"),
            )
            return None
        style = self.draft_style_combo.get() or "友好"
        self.draft_button.configure(state="disabled", text="正在生成草稿…")
        self.draft_status_label.configure(text="正在用本地规则生成草稿…")
        try:
            result = self._service.generate_semantic_draft(
                self.message.id, style=style, use_external=False
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        finally:
            self.draft_button.configure(state="normal", text="生成 AI 草稿")
        result = dict(result or {})
        if not result.get("ok"):
            detail = result.get("error") or "请稍后重试；如果一直失败，请导出诊断信息。"
            self.draft_status_label.configure(
                text=f"生成草稿失败：{detail}", fg=self.theme.color("danger")
            )
            notify.show_error(
                self,
                "生成 AI 草稿失败",
                f"这次生成失败：{detail}\n\n可以稍后重试；如果一直失败，请导出诊断信息。",
            )
            return result
        provider = semantic_provider_label(result.get("provider"))
        context_ids = (
            result.get("context_message_ids")
            or result.get("source_message_ids")
            or []
        )
        uncertain = result.get("uncertain") or result.get("uncertainty_notes") or []
        if isinstance(uncertain, str):
            uncertain_text = uncertain
        else:
            uncertain_text = "；".join(str(item) for item in uncertain if str(item).strip())
        self.draft_provider_label.configure(
            text=f"生成方式：{provider} ｜ 上下文：{len(context_ids)} 条消息 ｜ 风格：{style}"
        )
        self.draft_uncertainty_label.configure(
            text=("不确定/注意：" + uncertain_text) if uncertain_text else ""
        )
        draft = str(result.get("draft") or result.get("text") or "")
        self.draft_text.delete("1.0", "end")
        self.draft_text.insert("1.0", draft)
        self.draft_copy_button.configure(state="normal" if draft else "disabled")
        self.draft_status_label.configure(
            text="草稿已生成（只读，不会发送；可编辑后手动复制）。",
            fg=self.theme.color("muted"),
        )
        return result

    def _copy_draft(self) -> None:
        draft = self.draft_text.get("1.0", "end-1c").strip()
        if not draft:
            notify.show_info(self, "草稿为空", "先生成或输入草稿，再点复制。")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(draft)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self, "复制失败", f"复制草稿失败：{exc}\n\n可以手动选中文字按 Ctrl+C。")
            return
        self.draft_status_label.configure(
            text="已复制到剪贴板。程序不会自动发送，请你自己粘贴到 QQ/邮箱后手动发送。",
            fg=self.theme.color("success"),
        )


# ---------------------------------------------------------------------------
# 连接 QQ 邮箱（P0-15：收件箱连接向导）
# ---------------------------------------------------------------------------

class MailConnectWizard(ModalDialog):
    """连接 QQ 邮箱：填写邮箱/授权码 → 保存并测试 → 勾选同意 → 同意并同步。

    对话框本身不直接碰服务，所有动作都调用 ``window.connect_mail_account``（与正式
    GUI E2E 使用同一个方法），保证“向导流程 = 真实产品代码”。
    """

    def __init__(self, window) -> None:
        super().__init__(window, "连接 QQ 邮箱（只读）", width=720)
        theme = self.theme
        self._email = ""
        self._connection_ok = False

        header = tk.Frame(self.body, bg=theme.color("bg"))
        header.pack(fill="x", pady=(0, 6))
        label(header, theme, "连接 QQ 邮箱", kind="h2", bg=theme.color("bg")).pack(side="left")
        hint(
            header,
            theme,
            "只读：程序不会发送邮件、不会删除或移动邮件。",
            bg=theme.color("bg"),
        ).pack(side="right")

        hint(
            self.body,
            theme,
            "授权码不是 QQ 密码：在浏览器打开 mail.qq.com → 设置 → 账号 → 开启 IMAP/SMTP → "
            "按提示短信验证 → 生成 16 位授权码。授权码只保存在 Windows 凭据管理器。",
            wrap=660,
            bg=theme.color("bg"),
        ).pack(fill="x", pady=(0, 8))

        form = Card(self.body, theme, "第一步：填写邮箱与授权码")
        form.pack(fill="x")
        email_row = tk.Frame(form.body, bg=theme.color("card"))
        email_row.pack(fill="x", pady=(0, 6))
        hint(email_row, theme, "邮箱地址：", bg=theme.color("card")).pack(side="left")
        self.email_entry = tk.Entry(email_row, font=theme.font("base"), width=34)
        self.email_entry.pack(side="left")
        hint(email_row, theme, "（例如 123456@qq.com）", bg=theme.color("card")).pack(
            side="left", padx=(6, 0)
        )
        auth_row = tk.Frame(form.body, bg=theme.color("card"))
        auth_row.pack(fill="x")
        hint(auth_row, theme, "授权码：", bg=theme.color("card")).pack(side="left")
        self.auth_entry = tk.Entry(auth_row, font=theme.font("base"), width=34, show="*")
        self.auth_entry.pack(side="left")
        hint(auth_row, theme, "（16 位专用密码，不是 QQ 密码）", bg=theme.color("card")).pack(
            side="left", padx=(6, 0)
        )
        self.saved_hint = hint(
            form.body,
            theme,
            "授权码只保存在 Windows 凭据管理器，界面不会显示明文。",
            wrap=640,
            bg=theme.color("card"),
        )
        self.saved_hint.pack(fill="x", pady=(6, 0))
        self.email_entry.bind("<KeyRelease>", lambda _event: self._detect_saved())
        self.email_entry.bind("<FocusOut>", lambda _event: self._detect_saved())

        test_row = tk.Frame(self.body, bg=theme.color("bg"))
        test_row.pack(fill="x", pady=(10, 0))
        self.test_button = big_button(
            test_row, theme, "保存并测试连接", self.save_and_test, kind="primary"
        )
        self.test_button.pack(side="left")
        self.status_label = hint(self.body, theme, "", wrap=660, bg=theme.color("bg"))
        self.status_label.pack(fill="x", pady=(8, 0))

        consent_card = Card(self.body, theme, "第二步：同意只读读取并同步")
        consent_card.pack(fill="x", pady=(10, 0))
        hint(
            consent_card.body,
            theme,
            "同步会用上面保存的授权码，只读读取你邮箱收件箱里的邮件；不会发邮件、不会删除邮件。",
            wrap=640,
            bg=theme.color("card"),
        ).pack(fill="x")
        self.consent_var = tk.IntVar(value=0)
        self.consent_check = tk.Checkbutton(
            consent_card.body,
            text="我同意读取该邮箱（只读）",
            variable=self.consent_var,
            command=self._update_consent_button,
            font=theme.font("base"),
            bg=theme.color("card"),
            activebackground=theme.color("card"),
            anchor="w",
            state="disabled",
        )
        self.consent_check.pack(fill="x", pady=(8, 6))
        self.sync_button = big_button(
            consent_card.body,
            theme,
            "同意并同步",
            self.grant_and_sync,
            kind="primary",
            state="disabled",
        )
        self.sync_button.pack(anchor="w")

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(12, 0))
        big_button(footer, theme, "取消", self._cancel).pack(side="right")

    # -- 状态 ---------------------------------------------------------
    def _set_status(self, text: str, *, error: bool = False, success: bool = False) -> None:
        color = "danger" if error else ("success" if success else "muted")
        self.status_label.configure(text=text, fg=self.theme.color(color))

    def _detect_saved(self) -> None:
        email = self.email_entry.get().strip()
        if not email:
            return
        try:
            saved = bool(self.window.service.has_mail_credential(email))
        except Exception:  # noqa: BLE001
            saved = False
        if saved:
            self.saved_hint.configure(
                text="已检测到本机已保存授权码，无需重新输入。可以直接点「保存并测试连接」。"
            )
        else:
            self.saved_hint.configure(
                text="授权码只保存在 Windows 凭据管理器，界面不会显示明文。"
            )

    def _update_consent_button(self) -> None:
        enabled = self._connection_ok and bool(self.consent_var.get())
        self.sync_button.configure(state="normal" if enabled else "disabled")

    # -- 第一步：保存并测试（不含同意/同步）----------------------------
    def save_and_test(self) -> dict:
        email = self.email_entry.get().strip()
        code = self.auth_entry.get().strip()
        if "@" not in email:
            self._set_status("邮箱地址格式不正确：请填写类似 123456@qq.com 的地址。", error=True)
            return {"ok": False, "error": "邮箱地址格式不正确"}
        self._email = email
        self._set_status("正在保存账号并测试连接（只读）…")
        result = self.window.connect_mail_account(email, code or None, grant_consent=False)
        if not result.get("connection_ok"):
            self._connection_ok = False
            self.consent_check.configure(state="disabled")
            self.consent_var.set(0)
            self._update_consent_button()
            detail = result.get("error") or "请检查授权码、网络或邮箱是否已开启 IMAP。"
            self._set_status(f"连接失败：{detail}", error=True)
            return result
        self._connection_ok = True
        self.consent_check.configure(state="normal")
        self._update_consent_button()
        self._set_status(
            "连接成功：账号与授权码已保存，并确认能登录邮箱。下一步请勾选下方同意项。"
        )
        return result

    # -- 第二步：同意并同步（完整流程）---------------------------------
    def grant_and_sync(self) -> dict:
        if not self._connection_ok:
            self._set_status("还没有通过连接测试，请先点「保存并测试连接」。", error=True)
            return {"ok": False, "error": "尚未通过连接测试"}
        if not self.consent_var.get():
            self._set_status("请先勾选「我同意读取该邮箱（只读）」。", error=True)
            return {"ok": False, "error": "尚未同意读取邮箱"}
        self._set_status("正在同意并同步邮件（只读）…")
        result = self.window.connect_mail_account(self._email or self.email_entry.get().strip())
        if not result.get("ok"):
            detail = result.get("error") or "请稍后重试；如果一直失败，请检查网络与授权码。"
            self._set_status(f"同意或同步失败：{detail}", error=True)
            return result
        new_count = sum(int(getattr(item, "messages_new", 0) or 0) for item in result.get("results", []))
        self._set_status(f"已同意读取并完成同步：本次读取到 {new_count} 封邮件（只读）。")
        self.confirmed = True
        self.destroy()
        return result


# ---------------------------------------------------------------------------
# QQ 会话选择器（隐私优先；P0-2x）
# ---------------------------------------------------------------------------

class QQSessionPickerDialog(ModalDialog):
    """选择要读取的 QQ 会话（实验性、隐私优先）。

    - 只有用户点「读取会话列表」后才调用 ``list_qq_visible_sessions(user_confirmed=True)``；
    - 只展示会话标题（本机、不写日志/证据），不读取聊天内容、不打开/切换会话、不触发扫描；
    - 默认全部不勾选；应用前二次确认（可能把会话在 QQ 标记已读，不可逆）；
    - 取消/关闭不调用 apply、不写任何设置。
    """

    def __init__(self, window, *, auto_confirm: bool = False) -> None:
        super().__init__(window, "选择要读取的 QQ 会话（实验）", width=780)
        theme = self.theme
        self.auto_confirm = bool(auto_confirm)
        self.sessions: list[dict] = []
        self.session_vars: list[tk.IntVar] = []
        self.session_checkbuttons: list[tk.Checkbutton] = []
        self.result: dict = {}
        self.applied = False

        label(
            self.body, theme, "只看会话名称，由你决定读哪几个", kind="h2", bg=theme.color("bg")
        ).pack(fill="x", pady=(0, 6))
        Banner(
            self.body,
            theme,
            "QQ 聊天读取仍为实验性功能：只能读取部分已授权会话的可见消息，不能保证完整读取全部历史，"
            "部分消息可能遗漏，同一条消息可能重复保存，无法可靠确认每条入库记录都是独立的新消息；"
            "图片、文件、表情及群聊格式可能不完整；切换会话可能改变 QQ 未读状态；"
            "QQ 处于隐藏或托盘状态时可能读取失败。重要通知请以原始 QQ 客户端为准。",
            wrap=700,
            kind="warning",
        ).pack(fill="x", pady=(0, 8))
        Banner(
            self.body,
            theme,
            "隐私说明：只读取 QQ 会话名称用于选择，不读取任何聊天内容；不会自动打开或切换到任何会话，"
            "也不会触发扫描。标题只在本机显示，不写日志/证据。你可以随时取消，取消不会保存任何选择。",
            wrap=700,
            kind="warning",
        ).pack(fill="x", pady=(0, 8))

        # 用户要求：在选择列表里明确写出“哪些会话已经授权/会读取”，
        # 避免“授权列表里没有某个会话”造成误解。
        self.saved_auth_label = Banner(self.body, theme, "", wrap=700, kind="info")
        try:
            self.saved_auth_label.set_text(self._saved_auth_text())
        except Exception:  # noqa: BLE001
            self.saved_auth_label.set_text("当前已保存的授权范围暂时读取不到。")
        self.saved_auth_label.pack(fill="x", pady=(0, 8))

        self.privacy_frame = tk.Frame(self.body, bg=theme.color("bg"))
        self.privacy_frame.pack(fill="x")
        hint(
            self.privacy_frame,
            theme,
            "点下面的按钮后，程序才会读取当前 QQ 可见会话的名称列表（只读标题，不读消息）。",
            wrap=700,
            bg=theme.color("bg"),
        ).pack(fill="x", pady=(0, 8))
        self.load_button = big_button(
            self.privacy_frame,
            theme,
            "读取会话列表（仅标题）",
            self.load_sessions,
            kind="primary",
        )
        self.load_button.pack(anchor="w")

        self.list_frame = tk.Frame(self.body, bg=theme.color("bg"))
        self.list_canvas, self.list_content = make_scrollable(self.list_frame, theme)
        self.list_canvas.configure(height=260)

        action_row = tk.Frame(self.list_frame, bg=theme.color("bg"))
        action_row.pack(fill="x", pady=(8, 0))
        big_button(action_row, theme, "全选", lambda: self.toggle_all(True)).pack(side="left")
        big_button(action_row, theme, "清空", lambda: self.toggle_all(False)).pack(
            side="left", padx=(8, 0)
        )
        hint(
            action_row,
            theme,
            "默认不勾选任何会话；你勾选哪几个，程序才只读哪几个。",
            wrap=430,
            bg=theme.color("bg"),
        ).pack(side="left", padx=(10, 0))
        self.apply_button = big_button(
            self.list_frame,
            theme,
            "只读取勾选的会话（0 个）",
            self.confirm_selection,
            kind="primary",
            state="disabled",
        )
        self.apply_button.pack(anchor="w", pady=(10, 0))

        self.status_label = hint(self.body, theme, "", wrap=700, bg=theme.color("bg"))
        self.status_label.pack(side="bottom", fill="x", pady=(10, 0))

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        big_button(footer, theme, "取消（不保存）", self._cancel).pack(side="right")
        self._set_status("还没有读取会话列表；取消不会调用任何读取或保存。")

    # -- 读取会话标题 ---------------------------------------------------
    def _saved_auth_text(self) -> str:
        """当前已保存的授权范围（本机 UI 展示；不读取聊天内容/不写日志）。"""
        try:
            accounts = list(self.window.service.list_accounts())
            conversations = list(self.window.service.list_conversations())
        except Exception:  # noqa: BLE001
            return "当前已保存的授权范围暂时读取不到。"
        qq_ids = {
            str(getattr(account, "id", ""))
            for account in accounts
            if str(getattr(getattr(account, "kind", None), "value", getattr(account, "kind", ""))) == "qq"
        }
        enabled: list[str] = []
        disabled: list[str] = []
        for conversation in conversations:
            if str(getattr(conversation, "account_id", "")) not in qq_ids:
                continue
            title = str(getattr(conversation, "title", "") or "（无标题会话）")
            (enabled if bool(getattr(conversation, "enabled", False)) else disabled).append(title)
        mail = [
            str(getattr(account, "display_name", "") or "邮箱")
            for account in accounts
            if str(getattr(getattr(account, "kind", None), "value", getattr(account, "kind", ""))) == "email"
            and bool(getattr(account, "enabled", False))
        ]
        lines = ["当前已保存的授权范围（以这里为准）："]
        if enabled:
            shown = "、".join(enabled[:8]) + ("…" if len(enabled) > 8 else "")
            lines.append(f"✅ 已授权、会读取的 QQ 会话（{len(enabled)} 个）：{shown}")
        else:
            lines.append("还没有已授权的 QQ 会话。")
        if disabled:
            lines.append(f"⛔ 未授权/未勾选、不会读取的 QQ 会话：{len(disabled)} 个")
        if mail:
            lines.append(f"✅ 已授权的邮箱：{len(mail)} 个")
        lines.append("提示：选择列表只显示 QQ 当前可见的最近会话；不在这里不代表授权被删除。")
        return "\n".join(lines)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, fg=self.theme.color("danger" if error else "muted")
        )

    def status_text(self) -> str:
        return self.status_label.cget("text")

    def load_sessions(self) -> dict:
        if not hasattr(self.window.service, "list_qq_visible_sessions"):
            detail = "会话选择接口还没有就绪（等待核心层实现）。"
            self._set_status(f"读取会话列表失败：{detail}", error=True)
            return {"ok": False, "sessions": [], "error": detail}
        self.load_button.configure(state="disabled", text="正在读取会话列表…")
        self._set_status("正在只读读取 QQ 会话名称，请稍候…")
        try:
            result = self.window.service.list_qq_visible_sessions(user_confirmed=True)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "sessions": [], "error": str(exc)}
        finally:
            self.load_button.configure(state="normal", text="读取会话列表（仅标题）")
        result = dict(result or {})
        self.result = result
        if not result.get("ok"):
            detail = result.get("error") or "请确认 QQ 已登录、没有被其他窗口挡住。"
            self._set_status(f"读取会话列表失败：{detail}", error=True)
            return result
        self.sessions = [
            dict(item) for item in (result.get("sessions") or []) if isinstance(item, dict)
        ][:20]
        if not self.sessions:
            self._set_status("读取成功，但没有发现可选择的 QQ 会话。请先在 QQ 打开聊天窗口后再试。")
            return result
        self._render_sessions()
        self.privacy_frame.pack_forget()
        self.list_frame.pack(fill="both", expand=True)
        self._set_status(
            f"已在本机读取到 {len(self.sessions)} 个会话名称（未读取聊天内容）。请勾选要读取的会话。"
        )
        return result

    def _render_sessions(self) -> None:
        for child in list(self.list_content.winfo_children()):
            child.destroy()
        self.session_vars = []
        self.session_checkbuttons = []
        for item in self.sessions:
            title = str(item.get("title") or "（无标题会话）")
            unread = item.get("unread")
            text = f"{title}（未读 {unread}）" if unread else title
            var = tk.IntVar(value=0)  # 隐私优先：默认全部不勾选
            check = tk.Checkbutton(
                self.list_content,
                text=text,
                variable=var,
                command=self._update_apply_button,
                font=self.theme.font("base"),
                bg=self.theme.color("bg"),
                activebackground=self.theme.color("bg"),
                anchor="w",
            )
            check.pack(fill="x", anchor="w")
            self.session_vars.append(var)
            self.session_checkbuttons.append(check)
        self._update_apply_button()

    def selected_titles(self) -> list[str]:
        titles: list[str] = []
        for item, var in zip(self.sessions, self.session_vars):
            if var.get():
                title = str(item.get("title") or "").strip()
                if title:
                    titles.append(title)
        return titles

    def toggle_all(self, checked: bool) -> None:
        for var in self.session_vars:
            var.set(1 if checked else 0)
        self._update_apply_button()

    def _update_apply_button(self) -> None:
        try:
            count = len(self.selected_titles())
        except Exception:  # noqa: BLE001
            count = 0
        self.apply_button.configure(
            text=f"只读取勾选的会话（{count} 个）",
            state="normal" if count else "disabled",
        )

    # -- 应用选择 -------------------------------------------------------
    def confirm_selection(self) -> dict:
        titles = self.selected_titles()
        if not titles:
            self._set_status("请先勾选要读取的会话（默认不勾选）。", error=True)
            return {"ok": False, "error": "未选择任何会话"}
        if not self.auto_confirm:
            confirmed = notify.ask_confirm(
                self.window.root,
                "确认只读取这些 QQ 会话",
                "接下来只会读取你勾选的会话。打开这些会话可能把它们在 QQ 里标记为已读，且不可恢复；"
                "程序只读、不发送任何消息。\n\n确定只读取这 "
                f"{len(titles)} 个会话吗？",
                confirm_text="只读取勾选的会话",
                danger=True,
            )
            if not confirmed:
                self._set_status("已取消：没有保存任何选择。")
                return {"ok": False, "error": "cancelled", "applied": False}
        self.apply_button.configure(state="disabled", text="正在保存选择…")
        self._set_status("正在保存会话选择（不会触发扫描）…")
        try:
            result = self.window.service.apply_qq_session_selection(titles)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        result = dict(result or {})
        self.result = result
        self._update_apply_button()
        if not result.get("ok"):
            detail = result.get("error") or "请稍后重试；如果一直失败，请到设置页查看 QQ 状态。"
            self._set_status(f"保存会话选择失败：{detail}", error=True)
            return result
        self.applied = True
        selected_count = int(result.get("selected_count") or len(titles))
        self._set_status(
            f"已保存：只读取你勾选的 {selected_count} 个会话。"
            "点「立即读取一次」开始读取（只读，不发送任何消息）。"
        )
        return result

    def show(self) -> bool:
        super().show()
        return self.applied


# ---------------------------------------------------------------------------
# AI 语义摘要 / 每日简报（B/C）
# ---------------------------------------------------------------------------

_PROVIDER_LABELS = {
    "local": "本地规则",
    "local_rules": "本地规则",
    "local_llm": "本地规则",
    "rules": "本地规则",
    "external": "外部 AI（可能上传授权范围内内容）",
    "external_ai": "外部 AI（可能上传授权范围内内容）",
    "external_llm": "外部 AI（可能上传授权范围内内容）",
    "": "未知来源",
}

_SECTION_TITLES = {
    "questions": "问题",
    "requests": "请求",
    "deadlines": "截止",
    "promises": "承诺",
    "unresolved": "未解决",
    "chitchat": "闲聊",
    "must_today": "今天必须处理",
    "soon": "近期",
    "batch_reply": "可集中回复",
    "fyi": "仅供了解",
}

_KIND_SECTION_TITLES = {
    "question": "问题",
    "request": "请求",
    "deadline": "截止",
    "promise": "承诺",
    "unresolved": "未解决",
    "chitchat": "闲聊",
}


def semantic_provider_label(value) -> str:
    text = str(value or "").strip().lower()
    return _PROVIDER_LABELS.get(text, str(value or "未知来源"))


def _semantic_item_from_raw(item, allowed_prefix: str = "") -> dict:
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("content") or item.get("summary") or "").strip()
        ids = (
            item.get("source_message_ids")
            or item.get("message_ids")
            or item.get("source_ids")
            or []
        )
        due_hint = str(item.get("due_hint") or "").strip()
        uncertain = bool(item.get("uncertain"))
    else:
        text = str(item).strip()
        ids = []
        due_hint = ""
        uncertain = False
    if due_hint:
        text = f"{text}（截止提示：{due_hint}）" if text else f"（截止提示：{due_hint}）"
    if uncertain:
        text = f"{text}（不确定）"
    return {"text": text, "source_message_ids": list(ids or [])}


def _normalize_semantic_sections(data: dict) -> list[tuple[str, list[dict]]]:
    """把 core/intel 的多种返回结构统一成 [(中文分区标题, items)]。

    兼容：
    - daily: ``sections={must_today/soon/batch_reply/fyi: [item...]}``；
    - conversation: ``questions/requests/deadlines/promises/unresolved/chitchat`` 分类键
      或 ``items``（按 item.kind 分组）；
    - 已有 UI 测试/Fake 使用的 ``sections`` 列表。
    """
    sections = data.get("sections")
    normalized: list[tuple[str, list[dict]]] = []
    if isinstance(sections, dict) and sections:
        iterable = [
            {"title": _SECTION_TITLES.get(str(title), str(title)), "items": items}
            for title, items in sections.items()
        ]
    elif isinstance(sections, list) and sections:
        iterable = sections
    else:
        iterable = []
        for key in (
            "questions",
            "requests",
            "deadlines",
            "promises",
            "unresolved",
            "chitchat",
            "must_today",
            "soon",
            "batch_reply",
            "fyi",
        ):
            raw = data.get(key)
            if isinstance(raw, (list, tuple)) and raw:
                iterable.append({"title": _SECTION_TITLES.get(key, key), "items": raw})
        if not iterable:
            raw_items = data.get("items")
            if isinstance(raw_items, (list, tuple)) and raw_items:
                grouped: dict[str, list] = {}
                for item in raw_items:
                    kind = str(item.get("kind") or "") if isinstance(item, dict) else ""
                    title = _KIND_SECTION_TITLES.get(kind, "其他")
                    grouped.setdefault(title, []).append(item)
                iterable = [{"title": title, "items": items} for title, items in grouped.items()]
    for section in iterable:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or section.get("name") or "分区")
        raw_items = section.get("items") or section.get("messages") or section.get("entries") or []
        items: list[dict] = []
        if isinstance(raw_items, (list, tuple)):
            for item in raw_items:
                normalized_item = _semantic_item_from_raw(item)
                if normalized_item["text"]:
                    items.append(normalized_item)
        normalized.append((title, items))
    return normalized


class SemanticSummaryDialog(ModalDialog):
    """AI 会话摘要 / 每日简报的展示对话框。

    - provider 明确显示「本地规则」/「外部 AI（可能上传授权范围内内容）」；
    - 每条事项提供「查看原文」按钮（source_message_ids → open_message_detail）；
    - 显示不确定性/注意事项与调用量（若服务返回 usage）；
    - ``reload_callback(use_external=bool) -> dict`` 支持本地/外部对照重新生成。
    """

    def __init__(
        self,
        window,
        data: dict,
        *,
        title: str = "AI 语义摘要",
        reload_callback=None,
    ) -> None:
        super().__init__(window, title, width=780)
        theme = self.theme
        self.data = dict(data or {})
        self.reload_callback = reload_callback
        self.section_titles: list[str] = []
        self.source_buttons: list[tk.Button] = []
        self.last_error = ""

        header = tk.Frame(self.body, bg=theme.color("bg"))
        header.pack(fill="x", pady=(0, 6))
        label(header, theme, title, kind="h2", bg=theme.color("bg")).pack(side="left")
        self.provider_label = hint(header, theme, "", bg=theme.color("bg"))
        self.provider_label.pack(side="right")

        self.status_label = hint(self.body, theme, "", wrap=700, bg=theme.color("bg"))
        self.status_label.pack(fill="x", pady=(0, 6))
        self.overview_label = tk.Label(
            self.body,
            text="",
            font=theme.font("base"),
            fg=theme.color("text"),
            bg=theme.color("card"),
            justify="left",
            anchor="w",
            wraplength=700,
            padx=10,
            pady=8,
        )
        self.overview_label.pack(fill="x", pady=(0, 8))

        self.sections_frame = tk.Frame(self.body, bg=theme.color("bg"))
        self.sections_frame.pack(fill="both", expand=True)
        self.uncertain_label = hint(self.body, theme, "", wrap=700, bg=theme.color("bg"))
        self.uncertain_label.pack(fill="x", pady=(6, 0))
        self.usage_label = hint(self.body, theme, "", wrap=700, bg=theme.color("bg"))
        self.usage_label.pack(fill="x", pady=(2, 0))

        footer = tk.Frame(self.body, bg=theme.color("bg"))
        footer.pack(fill="x", pady=(12, 0))
        if self.reload_callback is not None:
            big_button(
                footer,
                theme,
                "使用本地规则对照",
                lambda: self._reload(False),
            ).pack(side="left")
            big_button(
                footer,
                theme,
                "使用外部 AI（可能上传授权范围内内容）",
                lambda: self._reload(True),
            ).pack(side="left", padx=(8, 0))
        big_button(footer, theme, "关闭", self._cancel).pack(side="right")

        self.render(self.data)

    def provider_text(self) -> str:
        return str(self.provider_label.cget("text"))

    def render(self, data: dict) -> None:
        self.data = dict(data or {})
        for child in list(self.sections_frame.winfo_children()):
            child.destroy()
        self.section_titles = []
        self.source_buttons = []
        self.last_error = ""
        provider = semantic_provider_label(self.data.get("provider"))
        self.provider_label.configure(text=f"生成方式：{provider}")
        error = self.data.get("error")
        if error:
            self.last_error = str(error)
            self.status_label.configure(text=f"生成失败：{error}", fg=self.theme.color("danger"))
        else:
            self.status_label.configure(text="", fg=self.theme.color("muted"))
        overview = str(self.data.get("overview") or "").strip()
        title_text = str(
            self.data.get("conversation_title")
            or (f"日期：{self.data['day']}" if self.data.get("day") else "")
        )
        if title_text:
            self.overview_label.configure(text=f"{title_text}\n\n{overview}" if overview else title_text)
        else:
            self.overview_label.configure(text=overview or "（本摘要没有概述）")

        sections = _normalize_semantic_sections(self.data)
        if not sections:
            hint(
                self.sections_frame,
                self.theme,
                "暂时没有可显示的摘要内容。" if not error else "生成失败，没有可显示的内容。",
                wrap=700,
                bg=self.theme.color("bg"),
            ).pack(fill="x")
        for section_title, items in sections:
            self.section_titles.append(section_title)
            card = Card(self.sections_frame, self.theme, section_title)
            card.pack(fill="x", pady=(0, 6))
            if not items:
                hint(card.body, self.theme, "（这一类没有内容）", bg=self.theme.color("card")).pack(fill="x")
                continue
            for item in items:
                row = tk.Frame(card.body, bg=self.theme.color("card"))
                row.pack(fill="x", pady=1)
                tk.Label(
                    row,
                    text="· " + str(item["text"]),
                    font=self.theme.font("base"),
                    fg=self.theme.color("text"),
                    bg=self.theme.color("card"),
                    justify="left",
                    anchor="w",
                    wraplength=600,
                ).pack(side="left", fill="x", expand=True)
                ids = [str(value) for value in item.get("source_message_ids") or [] if str(value)]
                if ids:
                    button = big_button(
                        row,
                        self.theme,
                        "查看原文",
                        lambda message_id=ids[0]: self._open_source(message_id),
                        kind="link",
                    )
                    button.pack(side="right")
                    self.source_buttons.append(button)

        uncertain = (
            self.data.get("uncertain")
            or self.data.get("uncertainty_notes")
            or []
        )
        caveats = self.data.get("caveats") or []
        notes: list[str] = []
        if isinstance(uncertain, str):
            notes.append(uncertain)
        elif isinstance(uncertain, list):
            notes.extend(str(item) for item in uncertain if str(item).strip())
        if isinstance(caveats, str):
            notes.append(caveats)
        elif isinstance(caveats, list):
            notes.extend(str(item) for item in caveats if str(item).strip())
        if notes:
            self.uncertain_label.configure(text="不确定/注意：" + "；".join(notes))
        else:
            self.uncertain_label.configure(text="")

        usage = self.data.get("usage") or {}
        usage_text = ""
        if isinstance(usage, dict) and usage:
            parts = []
            if "calls" in usage:
                parts.append(f"调用 {usage['calls']} 次")
            if "tokens" in usage:
                parts.append(f"tokens：{usage['tokens']}")
            if parts:
                usage_text = "本次用量：" + "；".join(parts)
        self.usage_label.configure(text=usage_text)

    def _reload(self, use_external: bool) -> None:
        if self.reload_callback is None:
            return
        try:
            data = self.reload_callback(use_external)
        except Exception as exc:  # noqa: BLE001
            self.render({"provider": "external_ai" if use_external else "local_rules", "error": str(exc)})
            return
        if isinstance(data, dict):
            self.render(data)

    def _open_source(self, message_id: str) -> None:
        try:
            self.window.open_message_detail(message_id)
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self, "打开原文失败", f"暂时打不开这条消息：{exc}")


# ---------------------------------------------------------------------------
# 关于信息
# ---------------------------------------------------------------------------

ABOUT_TEXT = (
    f"{APP_NAME} 是一个只读的桌面消息整理工具，帮助你从 QQ 与 QQ 邮箱里找出值得处理的消息。\n\n"
    "它不会发送 QQ 消息、不会发送邮件、不会删除远端数据，也没有任何发送按钮。\n"
    "默认完全离线运行；只有你在「设置与隐私 → AI 回复草稿」中明确同意后，"
    "才会把选中的消息内容发送给外部 AI。\n"
    "所有整理结果与日志默认保存在本机，日志不记录消息正文与凭据。\n\n"
    f"版本：{APP_VERSION}"
)


def about_text() -> str:
    return ABOUT_TEXT
