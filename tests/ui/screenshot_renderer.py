"""独立子进程生成 evidence/ui 截图（P0-15 修复）。

背景：把截图（Tk 窗口 + PrintWindow/GDI）放在 pytest 主进程里、在大量 Tk 窗口创建之后执行，
本机偶发 native 崩溃（exit -1、无 traceback）。本脚本由 ``test_screenshots*.py`` 通过
``subprocess`` 调用：独立进程、独立 Tk 根窗口，父测试只检查产物文件。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import tkinter as tk
from ctypes import windll

import win32gui
import win32ui
from PIL import Image, ImageGrab

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for extra in (REPO_ROOT / "src", HERE):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from fake_service import FakeService  # noqa: E402

from social_secretary.ui import create_main_window  # noqa: E402
from social_secretary.ui.dialogs import (  # noqa: E402
    ConfirmDeleteDataDialog,
    ConsentWizard,
    ExternalAIUploadDialog,
    ForegroundWarningDialog,
    MailConnectWizard,
    MessageDetailDialog,
    QQSessionPickerDialog,
    QuickQQReadDialog,
    SourceDetailDialog,
)


def _toplevel_hwnd(widget) -> int:
    hwnd = int(widget.winfo_id())
    parent = win32gui.GetParent(hwnd)
    return parent or hwnd


def _print_window(widget) -> Image.Image | None:
    hwnd = _toplevel_hwnd(widget)
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]), bmpstr, "raw", "BGRX", 0, 1
        )
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        if not result or image.width <= 0 or image.height <= 0:
            return None
        return image
    except Exception:  # noqa: BLE001 - 截图失败由上层重试/报错
        return None


def _image_colors(image: Image.Image) -> int:
    colors = image.convert("RGB").getcolors(maxcolors=200_000)
    return len(colors) if colors else 200_000


def _screen_grab(widget) -> Image.Image | None:
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    width, height = widget.winfo_width(), widget.winfo_height()
    if width <= 0 or height <= 0:
        return None
    return ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)


def _capture(widget, path: Path, *, min_colors: int = 30) -> None:
    """截图（带重试）：对话框第一次渲染可能是空白，稍等并置顶后再试。

    只有确认非空白才保存；保存用「临时文件 + os.replace 原子替换」，避免其它读者
    看到半写入/空白 PNG（Lead 复跑瞬时失败的根因之一）。
    """
    last_error = "未知原因"
    for attempt in range(6):
        top = widget if isinstance(widget, tk.Toplevel) else widget.winfo_toplevel()
        try:
            widget.update_idletasks()
            try:
                # 桌面被其它程序遮挡时（打包/真机 E2E 期间很常见），PrintWindow 可能返回空白；
                # 截图前把目标窗口置顶并强制重绘，截图后恢复。
                if attempt > 0 and isinstance(widget, tk.Toplevel):
                    widget.withdraw()
                    widget.update()
                    time.sleep(0.2)
                if not top.winfo_viewable():
                    top.deiconify()
                top.lift()
                try:
                    top.focus_force()
                except Exception:  # noqa: BLE001
                    pass
                top.attributes("-topmost", True)
                if isinstance(widget, tk.Toplevel):
                    if not widget.winfo_viewable():
                        widget.deiconify()
                    widget.lift()
                    widget.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass
            widget.update()
            time.sleep(0.3 + attempt * 0.4)
            widget.update()
            image = _print_window(widget)
            if image is None or _image_colors(image) < min_colors:
                image = _screen_grab(widget)
            if image is not None and _image_colors(image) >= min_colors:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
                try:
                    image.save(tmp, format="PNG")
                    if tmp.stat().st_size <= 3000:
                        raise RuntimeError("截图文件过小")
                    os.replace(tmp, path)
                finally:
                    if tmp.exists():
                        tmp.unlink(missing_ok=True)
                return
            last_error = "截图疑似空白"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        finally:
            for target in {id(top): top, id(widget): widget}.values():
                try:
                    target.attributes("-topmost", False)
                except Exception:  # noqa: BLE001
                    pass
    raise RuntimeError(f"截图失败（重试 6 次）：{path.name}：{last_error}")


def _new_window(service: FakeService) -> tuple[tk.Tk, object]:
    root = tk.Tk()
    window = create_main_window(service, root=root)
    root.geometry("1240x880+60+60")
    window.show()
    root.update()
    return root, window


def _close(root: tk.Tk) -> None:
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.15)


def render_main_pages(out: Path) -> None:
    service = FakeService()
    root, window = _new_window(service)
    try:
        # P0：主界面唯一主按钮「一键整理所有消息」；截图展示整理后的今日简报。
        window.organize_all()
        root.update()
        _capture(root, out / "one-click-organize.png")
        window.pages["drafts"].generate_local_button.invoke()
        root.update()
        for key, filename in (
            ("summary", "01-today-summary.png"),
            ("todos", "02-todos.png"),
            ("drafts", "03-reply-drafts.png"),
            ("sources", "04-message-sources.png"),
            ("settings", "05-settings-accounts.png"),
        ):
            window.show_page(key)
            root.update()
            _capture(root, out / filename)

        # P2 B2：消息中心处理状态（选中一行，展示筛选/计数/四个状态按钮）
        sources_page = window.pages["sources"]
        window.show_page("sources")
        sources_page.tree.configure(height=6)
        if sources_page.tree.get_children():
            sources_page.tree.selection_set("0")
            sources_page._on_tree_select()
        root.update()
        _capture(root, out / "message-center-status.png")

        # B/C：AI 语义摘要对话框（本地规则；合成数据）
        window.show_page("summary")
        semantic_dialog = window.pages["summary"].open_conversation_semantic(show=False)
        if semantic_dialog is not None:
            root.update()
            semantic_dialog._place_center()
            _capture(semantic_dialog, out / "ai-semantic-summary.png")
            semantic_dialog._cancel()

        settings = window.pages["settings"]
        settings._canvas.yview_moveto(1.0)
        root.update()
        _capture(root, out / "06-settings-privacy.png")

        if not settings.auth_help_panel.winfo_ismapped():
            settings.toggle_auth_help()
        settings._canvas.yview_moveto(0.16)
        root.update()
        _capture(root, out / "07-auth-code-help.png")

        # P0-23：滚动到账号区，截取「启用实验性 QQ 客户端读取」勾选框（默认关闭）
        settings._canvas.yview_moveto(0.33)
        root.update()
        _capture(root, out / "05b-settings-qq-experimental.png")

        # P0-25：一键读取当前 QQ 聊天的确认框
        quick = QuickQQReadDialog(window)
        root.update()
        quick._place_center()
        _capture(quick, out / "quick-qq-confirm.png")
        quick._cancel()

        # 隐私优先：QQ 会话选择器（先隐私说明，再只显示标题供勾选）
        picker = QQSessionPickerDialog(window, auto_confirm=True)
        root.update()
        picker._place_center()
        _capture(picker, out / "qq-session-picker.png")
        picker.load_sessions()
        root.update()
        picker.session_vars[0].set(1)
        if len(picker.session_vars) > 1:
            picker.session_vars[1].set(1)
        picker._update_apply_button()
        root.update()
        _capture(picker, out / "qq-session-picker-selected.png")
        picker._cancel()

        wizard = ConsentWizard(window)
        root.update()
        wizard._place_center()
        _capture(wizard, out / "08-first-run-consent.png")
        wizard._cancel()

        warning = ForegroundWarningDialog(window, seconds=5)
        root.update()
        warning._place_center()
        _capture(warning, out / "09-foreground-warning.png")
        warning._cancel()

        delete_dialog = ConfirmDeleteDataDialog(window)
        delete_dialog._check_var.set(1)
        delete_dialog._type_var.set("删除")
        root.update()
        delete_dialog._place_center()
        _capture(delete_dialog, out / "10-delete-data-confirm.png")
        delete_dialog._cancel()

        upload_dialog = ExternalAIUploadDialog(
            window,
            model_label="演示模型（假）",
            endpoint="https://example.invalid/v1",
            message_count=1,
        )
        upload_dialog._check_var.set(1)
        upload_dialog._update_state()
        root.update()
        upload_dialog._place_center()
        _capture(upload_dialog, out / "11-external-ai-confirm.png")
        upload_dialog._cancel()

        detail = service.get_source_detail("msg-1")
        source_dialog = SourceDetailDialog(window, detail)
        root.update()
        source_dialog._place_center()
        _capture(source_dialog, out / "12-source-detail.png")
        source_dialog._cancel()

        message = next(item for item in service.list_messages() if item.id == "msg-2")
        message_dialog = MessageDetailDialog(window, message, service)
        root.update()
        message_dialog._place_center()
        _capture(message_dialog, out / "13-message-detail.png")
        message_dialog._cancel()
    finally:
        _close(root)


def render_inbox_pages(out: Path) -> None:
    service = FakeService()
    root, window = _new_window(service)
    try:
        window.show_page("inbox")
        root.update()
        _capture(root, out / "inbox-demo.png")
        service.seed_inbox_mail(
            sender="活动负责人 <organizer@example.com>",
            subject="周末活动场地确认（合成验收邮件）",
            text="你好，请确认周六活动的场地和人数，并在周五下班前回复。\n\n（本邮件为验收用合成数据）",
            is_demo=False,
            timestamp=service._now_dt - timedelta(minutes=30),
            attachment_names=("活动场地确认.pdf", "参与人数统计.xlsx"),
        )
        window.pages["inbox"].refresh()
        root.update()
        page = window.pages["inbox"]
        rows = page.rows()
        real_index = next(index for index, row in enumerate(rows) if row["values"][0] == "真实")
        # 屏幕高度 864 下 Tk 窗口最高约 845px，详情区会被裁掉；截图前缩短列表高度，
        # 让「附件：…」一行出现在证据截图里（仅截图进程内调整，不影响产品行为）。
        page.tree.configure(height=4)
        root.update()
        page.select_row(real_index)
        root.update()
        _capture(root, out / "inbox-real-detail.png")
    finally:
        _close(root)

    service = FakeService()
    root, window = _new_window(service)
    try:
        service.clear_demo_data()
        window.show_page("inbox")
        root.update()
        _capture(root, out / "inbox-empty.png")
    finally:
        _close(root)

    service = FakeService()
    root, window = _new_window(service)
    try:
        wizard = MailConnectWizard(window)
        root.update()
        wizard._place_center()
        _capture(wizard, out / "inbox-connect.png")
        wizard._cancel()
    finally:
        _close(root)

    service = FakeService()
    root, window = _new_window(service)
    try:
        service.clear_demo_data()
        service.add_mail_account("me@qq.com", "me@qq.com")
        service.set_mail_credential("me@qq.com", "x" * 16)
        service.seed_inbox_mail(
            sender="活动负责人 <organizer@example.com>",
            subject="三天前的活动确认（合成验收邮件）",
            text="这是最近一封真实邮件（合成数据），用于验证『今日无邮件时展示最近有邮件的日期』。",
            is_demo=False,
            timestamp=service._now_dt - timedelta(days=3),
        )
        summary = window.generate_summary()
        if summary is None:
            raise RuntimeError("摘要生成失败，无法截图")
        window.show_page("summary")
        root.update()
        _capture(root, out / "inbox-summary-fallback.png")
    finally:
        _close(root)


def render_import_page(out: Path) -> None:
    service = FakeService()
    root, window = _new_window(service)
    try:
        window.show_page("import")
        page = window.pages["import"]
        page.set_text(
            "[2026-11-01 10:00] 张三: 你好，周末活动确认一下\n"
            "李四: 收到，下午三点前回复\n"
            "这一行没有冒号无法识别\n"
            "[2026-11-01 10:05] 王五: 我也会到"
        )
        page.parse_preview()
        root.update()
        _capture(root, out / "import-preview.png")
        page.confirm_import()
        root.update()
        _capture(root, out / "import-imported.png")
    finally:
        _close(root)


def render_error_feedback(out: Path) -> None:
    """P0：一键整理部分失败时首页的详细错误反馈（原因/解决方法/历史）。"""
    service = FakeService()
    service.organize_qq_failed = 1
    service.organize_qq_authorized = 2
    service.organize_qq_sessions_scanned = 2
    service.organize_qq_sessions_failed = 1
    service.organize_qq_failure_hint = (
        "无法将 QQ 主面板置前（可能最小化到托盘/被遮挡）。"
    )
    service.organize_mail_new = 0
    service.organize_history_total = 248
    service.organize_latest_day = service._now_dt.date().replace(day=2)
    root, window = _new_window(service)
    try:
        window.organize_all()
        root.update()
        _capture(root, out / "error-feedback.png")
    finally:
        _close(root)


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    render_main_pages(out)
    render_inbox_pages(out)
    render_import_page(out)
    render_error_feedback(out)
    print(f"rendered screenshots to {out}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: screenshot_renderer.py <out_dir>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
