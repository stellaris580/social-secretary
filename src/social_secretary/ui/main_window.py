"""主窗口：左侧导航 + 顶部主要操作 + 底部状态栏（Tkinter 实现）。

页面：今日摘要、待办、回复草稿、消息来源、设置与隐私。
顶部：立即读取一次（只读）、生成今日摘要、暂停/恢复采集；演示模式标记。
状态栏：采集状态、最后摘要时间、最近读取时间、演示模式；右侧常驻只读声明。

安全边界：整个界面没有任何发送 QQ 消息 / 发送邮件 / 粘贴到 QQ 输入框的能力。
不使用系统托盘（托盘需要第三方依赖；关闭窗口即退出，数据都保存在本机）。
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime

from ..contracts import (
    APP_NAME,
    APP_VERSION,
    ConsentScope,
    DailySummary,
    Message,
    ScanStatus,
)
from . import notify
from .dialogs import (
    ConsentWizard,
    ForegroundWarningDialog,
    MailConnectWizard,
    MessageDetailDialog,
    QQSessionPickerDialog,
    QuickQQReadDialog,
)
from .formatting import format_datetime, scan_status_label
from .pages import (
    DraftsPage,
    ImportPage,
    InboxPage,
    SettingsPage,
    SourcesPage,
    SummaryPage,
    TodoPage,
)
from .theme import APP_TITLE, apply_theme
from .widgets import big_button, hint, label

__all__ = ["AppWindow", "MainWindow"]

_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("summary", "今日摘要"),
    ("inbox", "收件箱"),
    ("todos", "待办"),
    ("drafts", "回复草稿"),
    ("sources", "消息来源"),
    ("import", "聊天导入（实验）"),
    ("settings", "设置与隐私"),
)


class AppWindow:
    """应用主窗口（Tkinter）。只依赖 ``contracts.ServiceAPI``。"""

    def __init__(self, service, root: tk.Tk | None = None) -> None:
        self.service = service
        self.root = root if root is not None else tk.Tk()
        self.theme = apply_theme(self.root)
        self.root.title(APP_TITLE)
        self.root.geometry("1220x840")
        self.root.minsize(1000, 700)
        self.root.configure(bg=self.theme.color("bg"))

        self._first_run_checked = False
        self._busy = False
        self._last_summary: DailySummary | None = None
        self._last_scan_results: list | None = None
        self._last_error: str | None = None
        self.last_organize_result: dict | None = None
        self.pages: dict[str, tk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self._current_page = "summary"

        self._build_ui()
        self.show_page("summary")
        self.refresh_all()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ==================================================================
    # 构建界面
    # ==================================================================
    def _build_ui(self) -> None:
        theme = self.theme
        self.root.configure(bg=theme.color("bg"))

        # 状态栏先占底部
        self.status_bar = tk.Frame(
            self.root,
            bg="#FFFFFF",
            highlightbackground=theme.color("border"),
            highlightthickness=1,
        )
        self.status_bar.pack(side="bottom", fill="x")
        self.status_label = hint(self.status_bar, theme, "准备就绪", bg="#FFFFFF")
        self.status_label.pack(side="left", padx=10, pady=4)
        self.readonly_label = tk.Label(
            self.status_bar,
            text="只读模式：程序不能发送 QQ 消息或邮件",
            font=theme.font("small"),
            fg=theme.color("success"),
            bg="#FFFFFF",
        )
        self.readonly_label.pack(side="right", padx=10, pady=4)
        # 暂停采集移到状态栏的次级小按钮（主界面减法）。
        self.btn_pause = big_button(
            self.status_bar, self.theme, "暂停采集", self.toggle_pause, kind="link"
        )
        self.btn_pause.pack(side="right", padx=(0, 12))

        container = tk.Frame(self.root, bg=theme.color("bg"))
        container.pack(side="top", fill="both", expand=True)

        # ---------------- 左侧导航 ----------------
        sidebar = tk.Frame(container, bg=theme.color("sidebar"), width=228)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=theme.color("sidebar"))
        brand.pack(fill="x", padx=12, pady=(16, 12))
        tk.Label(
            brand,
            text="秘",
            font=(theme.family, 16, "bold"),
            fg="#FFFFFF",
            bg=theme.color("primary"),
            width=2,
            height=1,
            padx=2,
            pady=2,
        ).pack(side="left")
        brand_text = tk.Frame(brand, bg=theme.color("sidebar"))
        brand_text.pack(side="left", padx=(8, 0))
        tk.Label(
            brand_text,
            text=APP_NAME,
            font=theme.font("card_title"),
            fg=theme.color("text"),
            bg=theme.color("sidebar"),
        ).pack(anchor="w")
        tk.Label(
            brand_text,
            text="只读消息整理助手",
            font=theme.font("small"),
            fg=theme.color("muted"),
            bg=theme.color("sidebar"),
        ).pack(anchor="w")

        for key, title in _NAV_ITEMS:
            button = tk.Button(
                sidebar,
                text=title,
                command=lambda page_key=key: self.show_page(page_key),
                font=theme.font("nav"),
                fg=theme.color("text"),
                bg=theme.color("sidebar"),
                activebackground=theme.color("primary_soft"),
                activeforeground=theme.color("primary_dark"),
                relief="flat",
                borderwidth=0,
                anchor="w",
                padx=16,
                pady=9,
                cursor="hand2",
                highlightthickness=0,
            )
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = button

        self.version_label = tk.Label(
            sidebar,
            text=f"版本 {APP_VERSION}",
            font=theme.font("tiny"),
            fg=theme.color("faint"),
            bg=theme.color("sidebar"),
        )
        self.version_label.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        hint(
            sidebar,
            theme,
            "程序只能看，不能发\n没有任何发送消息或邮件的按钮",
            wrap=190,
            bg=theme.color("sidebar"),
        ).pack(side="bottom", fill="x", padx=14, pady=(0, 14))

        # ---------------- 右侧主区域 ----------------
        main = tk.Frame(container, bg=theme.color("bg"))
        main.pack(side="left", fill="both", expand=True)

        top_bar = tk.Frame(main, bg=theme.color("bg"))
        top_bar.pack(fill="x", padx=18, pady=(12, 6))
        self.today_label = hint(top_bar, self.theme, "", bg=self.theme.color("bg"))
        self.today_label.pack(side="left")
        self.demo_badge = tk.Label(
            top_bar,
            text="演示模式（仅合成数据）",
            font=self.theme.font("small"),
            fg=self.theme.color("demo"),
            bg=self.theme.color("demo_soft"),
            padx=10,
            pady=3,
        )

        # 用户正式版实测后的界面减法：顶部只保留唯一主按钮「一键整理所有消息」
        # 与「管理授权会话」；其余入口移到设置页/高级区。
        self.btn_organize = big_button(
            top_bar,
            self.theme,
            "一键整理所有消息",
            self.organize_all,
            kind="primary",
        )
        self.btn_organize.pack(side="right")
        self.btn_manage_sources = big_button(
            top_bar, self.theme, "管理授权会话", self.manage_sources
        )
        self.btn_manage_sources.pack(side="right", padx=(8, 0))

        # 兼容旧测试/旧方法对象：保留这些按钮对象，但不再 pack 到主界面。
        # 高级入口在「设置与隐私」页；这些 method 仍可直接调用。
        self.btn_generate = big_button(
            top_bar, self.theme, "生成今日摘要", self.generate_summary, kind="primary"
        )
        self.btn_scan = big_button(top_bar, self.theme, "立即读取一次", self.run_scan)
        self.btn_quick_qq = big_button(
            top_bar,
            self.theme,
            "读取 QQ 当前聊天（实验）",
            self.quick_qq_read,
            kind="primary",
        )
        self.btn_qq_session_picker = big_button(
            top_bar,
            self.theme,
            "选择要读取的 QQ 会话（实验）",
            self.open_qq_session_picker,
        )

        self.page_container = tk.Frame(main, bg=self.theme.color("bg"))
        self.page_container.pack(fill="both", expand=True)
        self.page_container.grid_rowconfigure(0, weight=1)
        self.page_container.grid_columnconfigure(0, weight=1)

        page_classes = {
            "summary": SummaryPage,
            "inbox": InboxPage,
            "todos": TodoPage,
            "drafts": DraftsPage,
            "sources": SourcesPage,
            "import": ImportPage,
            "settings": SettingsPage,
        }
        for key, page_class in page_classes.items():
            page = page_class(self.page_container, self)
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
            self.pages[key] = page

    # ==================================================================
    # 导航与刷新
    # ==================================================================
    def show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        for page_key, page in self.pages.items():
            if page_key == key:
                page.grid()
                page.tkraise()
            else:
                page.grid_remove()
        for page_key, button in self.nav_buttons.items():
            if page_key == key:
                button.configure(
                    bg=self.theme.color("primary_soft"),
                    fg=self.theme.color("primary_dark"),
                    font=self.theme.font("nav_active"),
                )
            else:
                button.configure(
                    bg=self.theme.color("sidebar"),
                    fg=self.theme.color("text"),
                    font=self.theme.font("nav"),
                )
        self._current_page = key
        page = self.pages[key]
        try:
            page.refresh()
        except Exception as exc:  # noqa: BLE001 - 单个页面刷新失败不应崩溃
            self.set_status(f"打开页面失败：{exc}")

    def current_page_key(self) -> str:
        return self._current_page

    def current_page(self) -> tk.Frame:
        return self.pages[self._current_page]

    def refresh_all(self) -> None:
        for page in self.pages.values():
            try:
                page.refresh()
            except Exception as exc:  # noqa: BLE001
                self.set_status(f"刷新页面失败：{exc}")
        self.refresh_status()

    def refresh_status(self) -> None:
        paused = self.is_collection_paused()
        demo = self.is_demo_active()
        self.btn_pause.configure(text="恢复采集" if paused else "暂停采集")

        summary = self._last_summary
        if summary is None:
            try:
                summary = self.service.get_cached_summary()
            except Exception:  # noqa: BLE001
                summary = None
        self._last_summary = summary
        last_summary_text = format_datetime(summary.generated_at) if summary is not None else "尚未生成"

        try:
            status = self.service.collection_status() or {}
        except Exception:  # noqa: BLE001
            status = {}
        parts = [f"采集：{'已暂停' if paused else '待命'}", f"最后摘要：{last_summary_text}"]
        last_scan = status.get("last_scan_at")
        if last_scan:
            parts.append(f"最近读取：{format_datetime(last_scan)}")
        last_status = status.get("last_scan_status")
        if last_status:
            try:
                parts.append(f"读取状态：{scan_status_label(ScanStatus(str(last_status)))}")
            except ValueError:
                parts.append(f"读取状态：{last_status}")
        if demo:
            parts.append("演示模式：仅合成数据")
        self.status_label.configure(text="　·　".join(parts))

        if demo:
            self.demo_badge.pack(side="right", padx=(0, 8))
        else:
            self.demo_badge.pack_forget()
        self.today_label.configure(text=f"今天是 {format_datetime(datetime.now().astimezone())}")

    def is_collection_paused(self) -> bool:
        try:
            status = self.service.collection_status() or {}
            return bool(status.get("paused", False))
        except Exception:  # noqa: BLE001
            try:
                return bool(self.service.get_settings().collection_paused)
            except Exception:  # noqa: BLE001
                return True

    def is_demo_active(self) -> bool:
        try:
            return bool(self.service.is_demo_active())
        except Exception:  # noqa: BLE001
            return False

    def last_summary(self) -> DailySummary | None:
        return self._last_summary

    def last_scan_results(self) -> list | None:
        return self._last_scan_results

    # ==================================================================
    # 主要操作：生成摘要 / 读取 / 暂停
    # ==================================================================
    def generate_summary(self) -> DailySummary | None:
        if self._busy:
            return None
        self._set_busy(True)
        self.set_status("正在本机整理今天的消息，请稍候…（不会联网）")
        try:
            summary = self.service.generate_daily_summary()
        except Exception as exc:  # noqa: BLE001
            self._set_busy(False)
            self._last_error = str(exc)
            self.set_status(f"生成摘要失败：{exc}", error=True)
            notify.show_error(
                self.root,
                "生成今日摘要失败",
                f"这次生成失败：{exc}\n\n可以稍后重试；如果一直失败，请到「设置与隐私」"
                "检查采集状态或导出诊断信息。",
            )
            return None
        self._set_busy(False)

        # P0-15：今日摘要为空但收件箱有真实邮件时，自动回退到「最近有邮件的那一天」。
        fallback_notice: str | None = None
        today_total = len(summary.important) + len(summary.normal) + len(summary.fyi)
        if today_total == 0:
            try:
                latest = getattr(self.service, "generate_latest_mail_summary", None)
                candidate = latest() if callable(latest) else None
            except Exception:  # noqa: BLE001 - 回退失败不应影响今日摘要
                candidate = None
            if candidate is not None:
                candidate_total = len(candidate.important) + len(candidate.normal) + len(candidate.fyi)
                if candidate_total > 0 and candidate.day != summary.day:
                    fallback_notice = (
                        f"今日无邮件，展示最近有邮件的日期（{candidate.day.isoformat()}）。"
                    )
                    summary = candidate

        self._last_summary = summary
        self.pages["summary"].show_summary(summary, fallback_notice=fallback_notice)
        self._refresh_after_data_change()
        self.refresh_status()
        total = len(summary.important) + len(summary.normal) + len(summary.fyi)
        if fallback_notice:
            self.set_status(f"{fallback_notice}共 {total} 条消息，{len(summary.todos)} 条待办。")
        else:
            self.set_status(f"今日摘要已生成：共 {total} 条消息，{len(summary.todos)} 条待办。")
        return summary

    def run_scan(self) -> list | None:
        if self._busy:
            return None
        try:
            settings = self.service.get_settings()
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.root, "读取设置失败", f"暂时读不到设置：{exc}\n\n可以稍后重试。")
            return None
        if settings.collection_paused:
            notify.show_warning(
                self.root,
                "采集已暂停",
                "当前处于暂停状态，不会读取任何消息。\n\n"
                "如果现在想读取，请点右上角的「恢复采集」，然后再点「立即读取一次」。",
            )
            return None
        if not (settings.qq_read_consent or settings.mail_read_consent):
            notify.show_warning(
                self.root,
                "还没有同意读取消息",
                "为保护隐私，读取前必须先同意读取 QQ 或邮箱。\n\n"
                "请到「设置与隐私」按提示同意后再读取；也可以先在设置里加载演示数据体验界面。",
            )
            return None

        experimental_qq = bool(getattr(settings, "qq_read_experimental", False))
        if self._has_enabled_qq_account() and not experimental_qq:
            notify.show_warning(
                self.root,
                "QQ 实验读取未启用",
                "这次只会读取邮箱；如需读取 QQ，请到「设置与隐私 → QQ 读取（实验）」开启"
                "（仅只读白名单授权会话、需 QQ 主窗口在前台，可能短暂打断你的操作）。\n\n"
                "邮箱读取不受影响，可以继续。",
            )

        foreground_ack = False
        if (
            settings.show_foreground_warning
            and self._has_enabled_qq_account()
            and experimental_qq
        ):
            dialog = ForegroundWarningDialog(self)
            if not dialog.show():
                self.set_status("已取消读取：没有占用 QQ 前台窗口。")
                return None
            foreground_ack = True

        self._set_busy(True)
        self.set_status("正在只读读取消息，请稍候…")
        try:
            results = self.service.run_scan(trigger="manual", foreground_ack=foreground_ack)
        except Exception as exc:  # noqa: BLE001
            self._set_busy(False)
            self._last_error = str(exc)
            self.set_status(f"读取失败：{exc}", error=True)
            notify.show_error(
                self.root,
                "读取消息失败",
                f"这次读取失败：{exc}\n\n如果读取 QQ，请确认 QQ 已登录、没有被其他窗口挡住；"
                "也可以只读取邮箱，或稍后再试。",
            )
            return None
        self._set_busy(False)

        self._last_scan_results = list(results)
        self._refresh_after_data_change()
        self.refresh_status()
        summary_text = self._format_scan_results(results)
        self.set_status(summary_text)
        if any(result.status != ScanStatus.OK for result in results):
            notify.show_warning(
                "部分消息读取失败", summary_text + "\n\n可以到「设置与隐私」查看状态，或稍后重试。"
            )
        return list(results)

    # ==================================================================
    # 唯一主入口：一键整理所有消息
    # ==================================================================
    def organize_all(self) -> dict:
        """一键整理所有已授权 QQ 会话 + 邮箱，并展示今日简报。

        - 不要求用户逐窗口打开 QQ，也不再逐次弹确认（首次授权说明在会话选择器/首次向导）。
        - 只读：不会发送/删除任何消息；失败项用普通中文列出，不显示“全部完成”。
        - 固定钩子：``window.btn_organize`` / ``window.organize_all`` /
          ``window.last_organize_result``。
        """
        if self._busy:
            return {"ok": False, "error": "正在整理，请稍候"}
        if not hasattr(self.service, "organize_all"):
            detail = "一键整理功能还没有就绪（等待核心层实现）。"
            self.set_status(f"整理消息失败：{detail}", error=True)
            notify.show_error(self.root, "整理消息失败", detail)
            return {"ok": False, "error": detail}
        self._set_busy(True)
        self.set_status("正在整理所有消息…（只读，不会发送任何消息）")
        self.btn_organize.configure(text="正在整理所有消息…")
        crashed = False
        try:
            result = self.service.organize_all(foreground_ack=True)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
            crashed = True
        finally:
            self.btn_organize.configure(text="一键整理所有消息")
            self._set_busy(False)
        result = dict(result or {})
        self.last_organize_result = result
        if crashed:
            detail = result.get("error") or "未知错误"
            self.set_status(f"整理消息失败：{detail}。可以稍后重试；如果一直失败，请导出诊断信息。", error=True)
            notify.show_error(
                self.root,
                "整理消息失败",
                f"这次整理失败：{detail}\n\n可以稍后重试；如果一直失败，请到「设置与隐私」"
                "检查账号授权或导出诊断信息。",
            )
            return result
        configured = result.get("configured") or {}
        if not configured.get("qq") and not configured.get("mail"):
            detail = result.get("error") or "还没有可整理的账号，请先完成 QQ/邮箱授权。"
            self.set_status(f"整理消息失败：{detail}", error=True)
            notify.show_warning(
                self.root,
                "还没有可整理的账号",
                detail + "\n\n请到「设置与隐私」完成 QQ 或邮箱授权后再点「一键整理所有消息」。",
            )
            return result
        qq_success = int(result.get("qq_success", 0) or 0)
        qq_failed = int(result.get("qq_failed", 0) or 0)
        mail_success = int(result.get("mail_success", 0) or 0)
        mail_failed = int(result.get("mail_failed", 0) or 0)
        results = result.get("results") or []
        errors = result.get("errors") or []
        authorized = len(results)
        summary = result.get("summary")

        for key in ("sources", "inbox", "todos", "settings"):
            try:
                self.pages[key].refresh()
            except Exception:  # noqa: BLE001
                pass
        # P0：把详细结果（原因/解决方法/历史）直接交给首页展示；refresh 后仍保留。
        try:
            self.pages["summary"].show_organize_result(result)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(summary, DailySummary):
            self._last_summary = summary
        try:
            self.show_page("summary")
        except Exception:  # noqa: BLE001
            pass
        self.refresh_status()

        total_success = qq_success + mail_success
        total_failed = qq_failed + mail_failed
        # 优先使用 app-core 的新字段：qq_authorized / qq_sessions_scanned /
        # qq_sessions_failed / qq_failure_hint；旧字段仅作兼容回退。
        qq_authorized = int(result.get("qq_authorized", 0) or 0)
        qq_sessions_scanned = int(result.get("qq_sessions_scanned", 0) or 0)
        qq_sessions_failed = int(
            result.get("qq_sessions_failed", qq_failed) or 0
        )
        qq_failure_hint = str(result.get("qq_failure_hint") or "").strip()
        if not qq_authorized:
            try:
                account_kinds = {
                    str(account.id): str(getattr(account.kind, "value", account.kind))
                    for account in self.service.list_accounts()
                }
                qq_authorized = sum(
                    1
                    for conversation in self.service.list_conversations()
                    if account_kinds.get(str(conversation.account_id)) == "qq"
                    and bool(getattr(conversation, "enabled", False))
                )
            except Exception:  # noqa: BLE001
                qq_authorized = 0
        qq_sessions_success = max(0, qq_sessions_scanned - qq_sessions_failed)
        counts_text = (
            f"已授权 QQ 会话 {qq_authorized} 个：成功 {qq_sessions_success} 个，"
            f"失败 {qq_sessions_failed} 个；邮箱成功 {mail_success}，失败 {mail_failed}。"
        )
        failure_reasons = []
        if qq_failure_hint:
            failure_reasons.append(qq_failure_hint)
        for item in errors:
            failure_reasons.append(
                str(item.get("message") or item.get("error") or item)
                if isinstance(item, dict)
                else str(item)
            )
        for item in results:
            if isinstance(item, dict):
                if item.get("ok") is False:
                    failure_reasons.append("有来源整理失败")
                continue
            status_value = getattr(getattr(item, "status", None), "value", str(getattr(item, "status", "")))
            if str(status_value) in ("ok", "partial"):
                continue
            kind = str(getattr(item, "account_id", ""))
            label = "QQ" if "qq" in kind else ("邮箱" if "mail" in kind else "来源")
            reason = str(getattr(item, "error", "") or "").strip()
            failure_reasons.append(f"{label}读取失败" + (f"：{reason}" if reason else "，请确认已登录/打开 QQ 主面板"))
        has_failures = (
            total_failed > 0
            or qq_sessions_failed > 0
            or bool(qq_failure_hint)
            or any(bool(item.get("message")) for item in errors if isinstance(item, dict))
        )
        if has_failures or failure_reasons:
            reason_text = "；".join(dict.fromkeys(failure_reasons)) or "部分来源未读取成功"
            reason_text = reason_text.rstrip("。")
            if qq_failure_hint or qq_sessions_failed:
                reason_text = qq_failure_hint.rstrip("。") or reason_text
                reason_text += (
                    "。解决方法：先打开 QQ 主面板并保持可见（不要最小化到托盘/被遮挡），"
                    "再点「一键整理所有消息」重试"
                )
            self.set_status(
                f"整理消息部分失败：{counts_text}原因：{reason_text}。",
                error=True,
            )
        elif not result.get("ok"):
            self.set_status(
                f"整理消息失败：{result.get('error') or counts_text}。"
                "可以稍后重试，或先完成账号授权。",
                error=True,
            )
        else:
            self.set_status(f"整理完成：{counts_text}")
        return result

    def manage_sources(self) -> dict:
        """管理授权会话：优先打开会话选择器；没有 QQ 接口时打开设置页。"""
        if hasattr(self.service, "list_qq_visible_sessions"):
            try:
                return self.open_qq_session_picker()
            except Exception as exc:  # noqa: BLE001
                notify.show_error(
                    self.root,
                    "打开会话选择器失败",
                    f"这次打开失败：{exc}\n\n已为你打开「设置与隐私」，可在那里管理授权。",
                )
        self.show_page("settings")
        self.set_status("已打开「设置与隐私」：在这里管理 QQ/邮箱授权与会话。")
        return {"ok": True, "opened": "settings"}

    # ==================================================================
    # P0-25：一键读取当前 QQ 聊天（实验）
    # ==================================================================
    def quick_qq_read(self, *, auto_confirm: bool = False) -> dict:
        """只读读取用户当前在 QQ 打开的聊天。

        - 默认弹确认框（人工确认）；测试/E2E 可传 ``auto_confirm=True`` 跳过。
        - 返回 service 的结果 dict；失败时红色提示且绝不含「成功」。
        - 取消不做任何事（不调用 service）。
        """
        if self._busy:
            return {"ok": False, "error": "正在读取，请稍候"}
        if not hasattr(self.service, "quick_qq_read"):
            detail = "一键读取接口还没有就绪（等待核心层实现）。可以先点「立即读取一次」读取邮箱。"
            self.set_status(f"读取当前 QQ 聊天失败：{detail}", error=True)
            notify.show_error(self.root, "读取 QQ 当前聊天失败", detail)
            return {"ok": False, "error": detail}
        if not auto_confirm:
            dialog = QuickQQReadDialog(self)
            if not dialog.show():
                self.set_status("已取消：没有读取 QQ。")
                return {"ok": False, "error": "cancelled", "cancelled": True}
        self._set_busy(True)
        old_text = self.btn_quick_qq.cget("text")
        self.btn_quick_qq.configure(text="正在读取 QQ 当前聊天…")
        self.set_status("正在只读读取当前 QQ 聊天，请稍候…")
        try:
            result = self.service.quick_qq_read(user_confirmed=True)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        finally:
            self.btn_quick_qq.configure(text=old_text)
            self._set_busy(False)
        result = dict(result or {})
        if not result.get("ok"):
            detail = result.get("error") or (
                "请先在 QQ 打开要读取的聊天窗口，再点「读取 QQ 当前聊天（实验）」。"
            )
            self._last_error = str(detail)
            self.set_status(f"读取当前 QQ 聊天失败：{detail}", error=True)
            notify.show_error(
                self.root,
                "读取 QQ 当前聊天失败",
                f"这次读取失败：{detail}\n\n"
                "请确认：① 已在 QQ 打开聊天窗口；② 已确认读取；③ QQ 没有被其他窗口挡住。"
                "也可以先用「立即读取一次」只读邮箱。",
            )
            return result
        messages_seen = int(result.get("messages_seen", 0) or 0)
        messages_new = int(result.get("messages_new", 0) or 0)
        self._last_error = None
        for key in ("sources", "summary", "settings"):
            try:
                self.pages[key].refresh()
            except Exception:  # noqa: BLE001
                pass
        self.refresh_status()
        # refresh_status 会重写状态栏文本，因此成功文案必须放在最后。
        self.set_status(
            f"已读取当前 QQ 聊天：看到 {messages_seen} 个气泡，本机新保存 {messages_new} 条。"
            "QQ 消息身份无法完全确认，可能有重复；只读，未发送任何消息。"
        )
        try:
            self.show_page("sources")
        except Exception:  # noqa: BLE001
            pass
        return result

    # ==================================================================
    # 隐私优先：选择要读取的 QQ 会话（实验）
    # ==================================================================
    def open_qq_session_picker(self, *, auto_confirm: bool = False) -> dict:
        """打开 QQ 会话选择器；返回 service/apply 结果 dict。

        - 默认需要用户在对话框内先点「读取会话列表」、勾选会话、再二次确认；
        - ``auto_confirm=True`` 仅供测试/E2E：只自动读取会话标题列表（不勾选、不 apply），
          返回 ``{"ok","sessions","applied":False,"error"}``；
        - 不自动扫描、不自动打开任何会话、不写日志/证据。
        """
        if not hasattr(self.service, "list_qq_visible_sessions"):
            detail = "会话选择接口还没有就绪（等待核心层实现）。"
            self.set_status(f"选择 QQ 会话失败：{detail}", error=True)
            notify.show_error(self.root, "选择 QQ 会话失败", detail)
            return {"ok": False, "sessions": [], "applied": False, "error": detail}
        dialog = QQSessionPickerDialog(self, auto_confirm=auto_confirm)
        if auto_confirm:
            result = dict(dialog.load_sessions() or {})
            result.setdefault("applied", False)
            dialog.destroy()
            return result
        dialog.show()
        result = dict(dialog.result or {})
        result["applied"] = bool(dialog.applied)
        if dialog.applied:
            for key in ("settings", "sources"):
                try:
                    self.pages[key].refresh()
                except Exception:  # noqa: BLE001
                    pass
            self.refresh_status()
            selected = result.get("selected_count", "?")
            self.set_status(
                f"已保存 QQ 会话选择：{selected} 个（只读）。点「立即读取一次」开始读取，"
                "程序不会发送任何消息。"
            )
        elif result.get("error") == "cancelled":
            self.set_status("已取消：没有保存 QQ 会话选择。")
        return result

    def toggle_pause(self) -> None:
        paused = self.is_collection_paused()
        try:
            if paused:
                self.service.resume_collection()
                self.set_status("采集已恢复。程序只会在你允许的会话里只读读取。")
            else:
                self.service.pause_collection()
                self.set_status("采集已暂停。程序不会再主动读取任何消息，仍可手动生成摘要。")
        except Exception as exc:  # noqa: BLE001
            notify.show_error(self.root, "切换采集状态失败", f"这次切换失败：{exc}\n\n可以再试一次。")
            self.set_status(f"切换采集状态失败：{exc}", error=True)
            return
        self.refresh_status()
        try:
            self.pages["settings"].refresh()
        except Exception:  # noqa: BLE001
            pass

    def _has_enabled_qq_account(self) -> bool:
        try:
            accounts = self.service.list_accounts()
        except Exception:  # noqa: BLE001
            return False
        return any(account.kind.value == "qq" and account.enabled for account in accounts)

    def _format_scan_results(self, results) -> str:
        if not results:
            return "读取完成：没有需要读取的账号。可以到「设置与隐私」添加账号。"
        new_messages = sum(int(result.messages_new or 0) for result in results)
        seen = sum(int(result.messages_seen or 0) for result in results)
        conversations = sum(int(result.conversations_scanned or 0) for result in results)
        statuses = {result.status for result in results}
        if statuses == {ScanStatus.OK}:
            state = "正常"
        elif ScanStatus.BLOCKED in statuses:
            state = "环境不满足（可能未登录或窗口不可用）"
        elif ScanStatus.PAUSED in statuses:
            state = "已暂停"
        elif statuses == {ScanStatus.FAILED}:
            state = "失败"
        else:
            state = "部分完成"
        text = (
            f"读取完成：本机新增 {new_messages} 条记录（共看到 {seen} 条），扫描 {conversations} 个会话；状态：{state}。"
            "QQ 消息身份无法完全确认，可能有重复，不能保证每条都是新消息。"
        )
        # 本地 UI 只展示失败会话名称/原因，方便用户决定是否重试；不写入证据/日志。
        failed = []
        for result in results:
            for warning in getattr(result, "warnings", ()) or ():
                if "重试仍失败" in warning or ("读取失败" in warning and "将自动重试" not in warning):
                    failed.append(str(warning).strip())
        if failed:
            text += " 未读到的会话：" + "；".join(failed[:4])
            if len(failed) > 4:
                text += f" 等 {len(failed)} 个"
        return text

    # ==================================================================
    # 连接 QQ 邮箱（P0-15 冻结 UI 钩子；连接向导与 GUI E2E 共用）
    # ==================================================================
    def connect_mail_account(
        self,
        email: str,
        auth_code: str | None = None,
        *,
        grant_consent: bool = True,
    ) -> dict:
        """连接 QQ 邮箱：保存账号/授权码 → 测试连接 → 同意 → 恢复采集 → 同步。

        返回固定结构（Lead 冻结，供 GUI E2E 断言）：
        ``{ok, added_account, connection_ok, consent_ok, resumed, sync_ok, results, error}``。
        任一步失败都返回 ``ok=False`` 且 ``error`` 为可操作中文说明，绝不显示“成功”。

        ``grant_consent=False`` 只做“保存 + 测试连接”（连接向导第一步用）。
        """
        result: dict = {
            "ok": False,
            "added_account": False,
            "connection_ok": False,
            "consent_ok": False,
            "resumed": False,
            "sync_ok": False,
            "results": [],
            "error": None,
        }
        email = (email or "").strip()
        if "@" not in email:
            result["error"] = "邮箱地址格式不正确：请填写类似 123456@qq.com 的地址。"
            return result
        try:
            accounts = list(self.service.list_mail_accounts())
        except Exception:  # noqa: BLE001 - 兼容旧核心：从账号列表里筛
            try:
                accounts = [
                    account
                    for account in self.service.list_accounts()
                    if account.kind.value == "email"
                ]
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"读取邮箱账号失败：{exc}"
                return result
        account = next(
            (
                account
                for account in accounts
                if email in (account.identifier, account.display_name)
            ),
            None,
        )
        if account is None:
            try:
                account = self.service.add_mail_account(email, email)
                result["added_account"] = True
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"保存邮箱账号失败：{exc}"
                return result

        if auth_code:
            try:
                self.service.set_mail_credential(email, auth_code)
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"保存授权码失败：{exc}。请检查 Windows 凭据管理器是否可用。"
                return result
        else:
            try:
                has_credential = bool(self.service.has_mail_credential(email))
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"读取本机授权码失败：{exc}"
                return result
            if not has_credential:
                result["error"] = "还没有可用的邮箱授权码：请输入 16 位授权码后重试。"
                return result

        try:
            check = self.service.test_mail_credential(email)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"连接失败：{exc}。请检查网络、授权码，以及邮箱是否已开启 IMAP 服务。"
            return result
        result["connection_ok"] = bool(check.ok)
        if not check.ok:
            result["error"] = (
                f"连接失败：{check.detail}。请检查授权码是否正确、邮箱是否已开启 IMAP 服务。"
            )
            return result
        if not grant_consent:
            result["ok"] = True
            return result

        try:
            self.service.grant_consent(
                ConsentScope.MAIL_READ,
                confirmation_text="连接邮箱向导：用户明确同意只读读取该邮箱",
            )
            result["consent_ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"保存读取同意失败：{exc}。可以稍后重试，或到「设置与隐私」手动同意。"
            return result
        try:
            self.service.resume_collection()
            result["resumed"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"恢复采集失败：{exc}。可以到「设置与隐私」手动恢复采集。"
            return result
        try:
            results = list(self.service.sync_mail(user_confirmed=True))
        except Exception as exc:  # noqa: BLE001
            result["error"] = (
                f"同步邮件失败：{exc}。可以点「测试连接」检查授权码，或稍后再试。"
            )
            return result
        result["results"] = results
        if not results:
            result["error"] = "同步没有返回任何邮件结果。请检查邮箱账号是否启用，或稍后再试。"
            return result
        failed = [item for item in results if getattr(item.status, "value", item.status) != "ok"]
        if failed:
            details = "；".join(
                f"{item.account_id}：{item.error or getattr(item.status, 'value', item.status)}"
                for item in failed
            )
            result["error"] = f"同步邮件失败：{details}。请检查网络/授权码后再试。"
            return result
        result["sync_ok"] = True
        result["ok"] = True
        try:
            self.pages["inbox"].refresh()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.pages["settings"].refresh()
        except Exception:  # noqa: BLE001
            pass
        self.refresh_status()
        return result

    def open_mail_connect_wizard(self) -> bool:
        """打开连接 QQ 邮箱向导；返回是否完成连接。"""
        wizard = MailConnectWizard(self)
        accepted = bool(wizard.show())
        if accepted:
            self.show_page("inbox")
            self.set_status("邮箱已连接并同步完成（只读）。收件箱已刷新。")
        return accepted

    # ==================================================================
    # 内部刷新
    # ==================================================================
    def _refresh_after_data_change(self) -> None:
        for key in ("inbox", "todos", "sources", "drafts"):
            try:
                self.pages[key].refresh()
            except Exception:  # noqa: BLE001
                pass

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.btn_organize,
            self.btn_manage_sources,
            self.btn_scan,
            self.btn_generate,
            self.btn_pause,
            self.btn_quick_qq,
        ):
            try:
                button.configure(state=state)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.root.configure(cursor="watch" if busy else "")
            self.root.update_idletasks()
        except Exception:  # noqa: BLE001
            pass

    def set_status(self, text: str, *, error: bool = False) -> None:
        """更新状态栏文案；``error=True`` 时用醒目红色显示失败状态。"""
        self.status_label.configure(
            text=text,
            fg=self.theme.color("danger" if error else "muted"),
        )

    # ==================================================================
    # 消息详情
    # ==================================================================
    def open_message_detail(self, message_id: str) -> MessageDetailDialog | None:
        message = self._find_message(message_id)
        if message is None:
            notify.show_info(
                self.root,
                "这条消息已不在本机",
                "可能它已经被清理，或者本机数据已被删除。可以点「生成今日摘要」刷新列表。",
            )
            return None
        dialog = MessageDetailDialog(self, message, self.service)
        dialog.show()
        return dialog

    def _find_message(self, message_id: str) -> Message | None:
        try:
            messages = self.service.list_messages(limit=500)
        except Exception:  # noqa: BLE001
            return None
        for message in messages:
            if message.id == message_id:
                return message
        return None

    # ==================================================================
    # 首次运行 / 同意
    # ==================================================================
    def maybe_show_first_run(self) -> bool:
        """首次运行时显示同意向导；返回用户是否完成同意。"""
        if self._first_run_checked:
            return False
        self._first_run_checked = True
        try:
            settings = self.service.get_settings()
        except Exception:  # noqa: BLE001
            return False
        if settings.first_run_completed:
            return False
        wizard = ConsentWizard(self)
        if wizard.show():
            self.apply_first_run_consents(wizard.selected_consents)
            return True
        notify.show_info(
            self.root,
            "还没有开始读取",
            "你可以先看看界面，或者到「设置与隐私」加载演示数据体验。\n\n"
            "在你明确同意之前，程序不会读取任何消息。",
        )
        return False

    def apply_first_run_consents(self, scopes: list[ConsentScope]) -> None:
        for scope in scopes:
            self.service.grant_consent(scope, confirmation_text="首次运行向导逐项确认")
        self.service.update_settings(first_run_completed=True)
        self.refresh_status()
        try:
            self.pages["settings"].refresh()
        except Exception:  # noqa: BLE001
            pass
        self.set_status(
            "同意已保存。下一步：在 QQ 打开你要读取的聊天，点「读取 QQ 当前聊天（实验）」一次即可"
            "（实验性：只读当前聊天、需 QQ 前台、默认关闭；邮箱需授权码）。"
        )

    # ==================================================================
    # 窗口生命周期
    # ==================================================================
    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:  # pragma: no cover - 无焦点环境
            pass
        self.root.update_idletasks()

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已销毁
            pass


# 兼容旧命名（部分代码/测试用 MainWindow 指代主窗口）
MainWindow = AppWindow

__all__ = ["AppWindow", "MainWindow"]
