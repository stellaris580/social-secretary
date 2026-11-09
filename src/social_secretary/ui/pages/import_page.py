"""聊天导入（实验）页：用户主动提供/粘贴聊天文本，先预览、再确认导入本机。

实验性边界（界面必须明确）：
- 不自动读取 QQ、不读 QQ 数据库、不抓取登录凭据；
- 只解析用户手动选择/粘贴的文本，只存本机、不联网；
- 不保证兼容任意 QQ 导出格式；预览只展示计数与元信息，不展示正文。
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from .. import notify
from ..formatting import format_datetime
from ..widgets import Banner, Card, big_button, hint, label

__all__ = ["ImportPage"]

_SUPPORTED_SUFFIXES = (".txt", ".md", ".json", ".jsonl", ".csv")

_EXPERIMENT_NOTICE = (
    "实验性功能：不会自动读取 QQ，也不会读取 QQ 数据库或登录凭据；只解析你手动选择/粘贴的聊天文本，"
    "只保存在本机、不联网。不保证兼容任意 QQ 导出格式；请先「解析预览」再「确认导入」。"
)


class ImportPage(tk.Frame):
    """聊天导入（实验）页面。"""

    def __init__(self, parent: tk.Misc, window) -> None:
        theme = window.theme
        super().__init__(parent, bg=theme.color("bg"))
        self.window = window
        self.service = window.service
        self.theme = theme
        self._last_preview: dict[str, Any] | None = None
        self._preview_ok = False

        header = tk.Frame(self, bg=theme.color("bg"))
        header.pack(fill="x", padx=18, pady=(14, 6))
        label(header, theme, "聊天导入（实验）", kind="h1", bg=theme.color("bg")).pack(side="left")
        hint(
            header,
            theme,
            "用户主动提供聊天文本，不是自动读取 QQ。",
            bg=theme.color("bg"),
        ).pack(side="right")

        Banner(self, theme, _EXPERIMENT_NOTICE, kind="warning", wrap=980).pack(
            fill="x", padx=18, pady=(0, 8)
        )

        input_card = Card(self, theme, "第一步：选择文件或粘贴文本")
        input_card.pack(fill="x", padx=18, pady=(0, 8))

        file_row = tk.Frame(input_card.body, bg=theme.color("card"))
        file_row.pack(fill="x")
        self.load_button = big_button(file_row, theme, "选择文件…", self.choose_file)
        self.load_button.pack(side="left")
        hint(
            file_row,
            theme,
            "支持 .txt / .md / .json / .jsonl / .csv；文件只在本机读取。",
            bg=theme.color("card"),
        ).pack(side="left", padx=(10, 0))
        self.source_label = hint(
            input_card.body,
            theme,
            "当前来源：粘贴的聊天文本",
            wrap=960,
            bg=theme.color("card"),
        )
        self.source_label.pack(fill="x", pady=(6, 0))

        self.text_box = tk.Text(
            input_card.body,
            height=8,
            wrap="word",
            font=theme.font("base"),
            bg="#FFFFFF",
            fg=theme.color("text"),
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.color("border"),
            padx=10,
            pady=8,
        )
        self.text_box.pack(fill="both", expand=True, pady=(8, 0))
        hint(
            input_card.body,
            theme,
            "示例格式：「张三: 你好」或「[2026-11-01 10:00] 张三: 你好」；识别不了的行会跳过并提示。",
            wrap=960,
            bg=theme.color("card"),
        ).pack(fill="x", pady=(4, 0))

        action_row = tk.Frame(input_card.body, bg=theme.color("card"))
        action_row.pack(fill="x", pady=(10, 0))
        self.parse_button = big_button(
            action_row, theme, "解析预览（不保存）", self.parse_preview, kind="primary"
        )
        self.parse_button.pack(side="left")
        self.import_button = big_button(
            action_row, theme, "确认导入本机", self.confirm_import, kind="danger", state="disabled"
        )
        self.import_button.pack(side="left", padx=(8, 0))
        big_button(action_row, theme, "清空", self.clear).pack(side="left", padx=(8, 0))
        hint(
            action_row,
            theme,
            "导入前必须先预览；确认导入后可在「消息来源」「今日摘要」中查看。",
            bg=theme.color("card"),
        ).pack(side="left", padx=(10, 0))

        self.status_label = hint(input_card.body, theme, "", wrap=960, bg=theme.color("card"))
        self.status_label.pack(fill="x", pady=(8, 0))

        result_card = Card(self, theme, "第二步：预览统计与将导入的消息（不含正文）")
        result_card.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.stats_label = label(result_card.body, theme, "还没有解析预览。", kind="base")
        self.stats_label.pack(fill="x")

        columns = ("conversation", "sender", "time", "length", "status")
        self.tree = ttk.Treeview(
            result_card.body, columns=columns, show="headings", height=7, selectmode="browse"
        )
        for key, (text, width) in {
            "conversation": ("会话", 260),
            "sender": ("发送者", 160),
            "time": ("时间", 160),
            "length": ("字数", 80),
            "status": ("状态", 110),
        }.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "conversation"))
        scrollbar = ttk.Scrollbar(result_card.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True, pady=(8, 0))

        hint(
            self,
            theme,
            "实验性功能：未验证真实 QQ 导出格式；程序不联网、不读取 QQ 数据库、不抓取凭据。",
            bg=theme.color("bg"),
        ).pack(fill="x", padx=18, pady=(0, 10))

    # ------------------------------------------------------------------
    # 固定测试钩子
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """页面刷新：不重复解析、不写库；只确保实验边界提示存在。"""
        if self._last_preview is None and not self.text_box.get("1.0", "end-1c").strip():
            self._set_status(
                "实验性功能：请粘贴聊天文本或选择文件，先「解析预览（不保存）」再「确认导入本机」。"
            )

    def set_text(self, text: str) -> None:
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text or "")
        self._reset_preview()
        self.source_label.configure(text="当前来源：粘贴的聊天文本")
        self._set_status("已填入文本：请先点「解析预览（不保存）」确认后，再点「确认导入本机」。")

    def load_file(self, path: str | Path) -> bool:
        file_path = Path(path)
        if file_path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            self._set_status(
                f"不支持的文件类型：{file_path.suffix or '（无扩展名）'}。"
                "请选择 .txt / .md / .json / .jsonl / .csv 文本文件。",
                error=True,
            )
            return False
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            self._set_status(f"读取文件失败：{exc}。请确认文件存在且有读取权限。", error=True)
            return False
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text and data:
            self._set_status("读取文件失败：无法识别文本编码（请另存为 UTF-8 后重试）。", error=True)
            return False
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self._reset_preview()
        self.source_label.configure(text=f"当前来源：{file_path.name}（本机文件，不上传）")
        self._set_status(
            f"已读取文件：{file_path.name}。请先点「解析预览（不保存）」，确认统计后再导入。"
        )
        return True

    def parse_preview(self) -> dict[str, Any]:
        text = self.text_box.get("1.0", "end-1c")
        if not text.strip():
            self._set_status("请先在文本框粘贴聊天文本，或点「选择文件…」。", error=True)
            return {"ok": False, "error": "聊天文本为空"}
        if not hasattr(self.service, "import_chat_text"):
            self._set_status("聊天导入接口还没有就绪（等待核心层实现）。", error=True)
            return {"ok": False, "error": "聊天导入接口不可用"}
        source = self._source_name()
        try:
            result = self.service.import_chat_text(
                text, source_name=source, options=None, dry_run=True
            )
        except Exception as exc:  # noqa: BLE001
            self._last_preview = None
            self._preview_ok = False
            self.import_button.configure(state="disabled")
            self._set_status(f"解析预览失败：{exc}。可以检查文本格式后再试。", error=True)
            return {"ok": False, "error": str(exc)}
        result = dict(result or {})
        self._last_preview = result
        if not result.get("ok"):
            self._preview_ok = False
            self.import_button.configure(state="disabled")
            detail = result.get("error") or "没有识别到可导入的聊天内容。"
            self._set_status(f"解析预览失败：{detail}", error=True)
            self._render_stats(result)
            self._fill_tree(result)
            return result
        self._preview_ok = True
        self.import_button.configure(state="normal")
        self._render_stats(result)
        self._fill_tree(result)
        warnings = int(result.get("warning_count", len(result.get("warnings") or [])) or 0)
        skipped = int(result.get("skipped_count", 0) or 0)
        duplicate = int(result.get("duplicate_count", 0) or 0)
        extra = ""
        if skipped:
            extra += f"，{skipped} 行无法识别将跳过"
        if duplicate:
            extra += f"，{duplicate} 条重复将跳过"
        if warnings:
            extra += f"，{warnings} 类警告"
        self._set_status(
            f"预览完成：识别到 {result.get('conversation_count', 0)} 个会话、"
            f"{result.get('message_count', 0)} 条消息{extra}。预览不会写入本机；确认后请点「确认导入本机」。"
        )
        return result

    def confirm_import(self) -> dict[str, Any]:
        if not self._preview_ok or self._last_preview is None:
            self._set_status(
                "请先点「解析预览（不保存）」确认统计后，再点「确认导入本机」。", error=True
            )
            return {"ok": False, "error": "还没有完成解析预览"}
        text = self.text_box.get("1.0", "end-1c")
        if not text.strip():
            self._set_status("文本为空：请先粘贴聊天文本或选择文件。", error=True)
            return {"ok": False, "error": "聊天文本为空"}
        self._set_status("正在导入本机（只写本地数据库，不联网）…")
        try:
            result = self.service.import_chat_text(
                text, source_name=self._source_name(), options=None, dry_run=False
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"导入失败：{exc}。可以稍后重试；如果一直失败，请导出诊断信息。", error=True)
            notify.show_error(
                self.window.root,
                "导入聊天文本失败",
                f"这次导入失败：{exc}\n\n可以检查文本格式后重试；如果一直失败，请导出诊断信息。",
            )
            return {"ok": False, "error": str(exc)}
        result = dict(result or {})
        self._last_preview = result
        if not result.get("ok"):
            detail = result.get("error") or "没有可导入的内容。"
            self._set_status(f"导入失败：{detail}", error=True)
            self._render_stats(result)
            self._fill_tree(result)
            return result
        imported = int(result.get("imported_count", 0) or 0)
        duplicate = int(result.get("duplicate_count", 0) or 0)
        skipped = int(result.get("skipped_count", 0) or 0)
        self._preview_ok = False
        self.import_button.configure(state="disabled")
        self._render_stats(result)
        self._fill_tree(result)
        detail = f"重复跳过 {duplicate} 条" if duplicate else ""
        if skipped:
            detail = (detail + "，" if detail else "") + f"无法识别跳过 {skipped} 行"
        suffix = f"（{detail}）" if detail else ""
        self._set_status(
            f"已导入 {imported} 条消息{suffix}。数据只保存在本机；不会联网、不会自动读取 QQ。"
            "可到「消息来源」「今日摘要」查看。"
        )
        for key in ("sources", "summary"):
            try:
                self.window.pages[key].refresh()
            except Exception:  # noqa: BLE001
                pass
        self.window.refresh_status()
        return result

    def status_text(self) -> str:
        return self.status_label.cget("text")

    def preview_stats(self) -> dict[str, Any]:
        if self._last_preview is None:
            return {}
        result = self._last_preview
        warnings = result.get("warnings") or []
        return {
            "ok": bool(result.get("ok")),
            "dry_run": bool(result.get("dry_run", True)),
            "format": str(result.get("format") or ""),
            "source_name": str(result.get("source_name") or ""),
            "conversation_count": int(result.get("conversation_count", 0) or 0),
            "message_count": int(result.get("message_count", 0) or 0),
            "imported_count": int(result.get("imported_count", 0) or 0),
            "duplicate_count": int(result.get("duplicate_count", 0) or 0),
            "skipped_count": int(result.get("skipped_count", 0) or 0),
            "warning_count": len(warnings),
            "warnings": [
                {"code": str(item.get("code") or ""), "count": int(item.get("count", 0) or 0)}
                for item in warnings
                if isinstance(item, dict)
            ],
            "error": result.get("error"),
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _source_name(self) -> str:
        text = self.source_label.cget("text")
        prefix = "当前来源："
        if text.startswith(prefix):
            return text[len(prefix) :].split("（", 1)[0].strip() or "粘贴的聊天文本"
        return "粘贴的聊天文本"

    def clear(self) -> None:
        self.text_box.delete("1.0", "end")
        self._reset_preview()
        self.source_label.configure(text="当前来源：粘贴的聊天文本")
        self.stats_label.configure(text="还没有解析预览。")
        self._set_status("已清空。请粘贴聊天文本或点「选择文件…」。")

    def choose_file(self) -> bool:
        path = filedialog.askopenfilename(
            parent=self.window.root,
            title="选择聊天文本文件（只读，本机）",
            filetypes=[
                ("聊天文本", "*.txt *.md *.json *.jsonl *.csv"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return False
        return self.load_file(path)

    def _reset_preview(self) -> None:
        self._last_preview = None
        self._preview_ok = False
        self.import_button.configure(state="disabled")
        self.stats_label.configure(text="还没有解析预览。")
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, fg=self.theme.color("danger" if error else "muted")
        )

    def _render_stats(self, result: dict[str, Any]) -> None:
        warnings = result.get("warnings") or []
        warning_codes = "、".join(
            str(item.get("code") or "警告")
            for item in warnings
            if isinstance(item, dict)
        )
        lines = [
            f"格式：{result.get('format') or '未识别'}　·　"
            f"会话数：{result.get('conversation_count', 0)}　·　"
            f"消息数：{result.get('message_count', 0)}",
            f"将导入/已导入：{result.get('imported_count', 0)}　·　"
            f"重复跳过：{result.get('duplicate_count', 0)}　·　"
            f"无法识别跳过：{result.get('skipped_count', 0)}　·　"
            f"警告：{len(warnings)} 类" + (f"（{warning_codes}）" if warning_codes else ""),
        ]
        if result.get("error"):
            lines.append(f"原因：{result.get('error')}")
        self.stats_label.configure(text="\n".join(lines))

    def _fill_tree(self, result: dict[str, Any]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        preview = result.get("preview") or result.get("messages") or []
        if not isinstance(preview, list) or not preview:
            return
        for index, row in enumerate(preview):
            if not isinstance(row, dict):
                continue
            stamp = row.get("timestamp") or row.get("time") or ""
            when = "时间未知"
            if stamp:
                try:
                    when = format_datetime(datetime.fromisoformat(str(stamp)))
                except (TypeError, ValueError):
                    when = str(stamp)
            length = row.get("length")
            if length is None:
                length = len(str(row.get("text") or ""))
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    str(row.get("conversation") or row.get("conversation_title") or ""),
                    str(row.get("sender") or ""),
                    when,
                    f"{length} 字",
                    str(row.get("status") or ""),
                ),
            )
