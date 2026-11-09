#!/usr/bin/env python
"""导入聊天文本（实验性）—— Tkinter 本机工具。

用途
----
让用户**主动选择自己**已经导出的聊天文本（.txt/.json/.csv），在本地完成：
选择文件 → 解析预览（默认脱敏，只显示长度/SHA-256）→ dry-run 或
“导入到独立测试数据目录并生成摘要” → 输出脱敏证据到
``evidence/import/import_<ts>.json``。

安全边界
--------
- 只读取用户在文件对话框里主动选择的文件；不联网、不访问 QQ、不读取 QQ 本地数据库；
- 永不写入用户真实产品数据库：导入数据写到 ``evidence/import/data/import_<ts>/``
  的**独立测试数据目录**（默认只保存长度+哈希，不保存正文）；
- 证据 JSON 不含消息/发送者/文件名原文，只含长度、SHA-256、统计与警告码；
- 不做任何“自动读取 QQ 聊天记录”；本工具是实验性导入原型。

运行
----
::

    .venv\\Scripts\\python.exe tools\\import_chat_text.py                # 打开图形界面
    .venv\\Scripts\\python.exe tools\\import_chat_text.py --selftest     # 真实创建窗口自检（合成样例）
    .venv\\Scripts\\python.exe tools\\import_chat_text.py --selftest --keep-open-ms 2500

双击入口：``tools\\导入聊天文本.cmd``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import tkinter as tk
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_secretary.contracts import Priority  # noqa: E402
from social_secretary.importing import (  # noqa: E402
    EXPERIMENTAL_NOTICE,
    ImportOptions,
    ImportResult,
    ImportRunReport,
    ImportSummary,
    JsonlMessageSink,
    parse_chat_bytes,
    run_import,
    text_fingerprint,
)

TOOL_NAME = "import_chat_text"
TOOL_VERSION = "0.1.0"
EXPERIMENTAL = True

EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "import"
DATA_DIR = EVIDENCE_DIR / "data"
SELFTEST_DIR = EVIDENCE_DIR / "selftest"

#: 单文件大小上限（超过直接拒绝，避免误选 SQLite/视频/压缩包等大文件）。
MAX_FILE_BYTES = 20 * 1024 * 1024
#: 预览最多显示多少条消息。
DEFAULT_PREVIEW_LIMIT = 20

FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("自动识别（推荐）", "auto"),
    ("通用行格式", "generic"),
    ("QQ 复制/导出文本（实验性启发式）", "qq_copy"),
    ("JSON", "json"),
    ("CSV", "csv"),
)
_FORMAT_TO_HINT = {label: hint for label, hint in FORMAT_CHOICES}

CATEGORY_LABELS = {"important": "重要", "normal": "普通", "fyi": "仅供了解"}

#: 本地规则层（可选）：可用时用产品同一套规则生成实验性摘要；不可用时退化为纯统计。
try:  # pragma: no cover - 依赖 intel 是否可用
    from social_secretary.intel.rules import RuleClassifier, RuleTodoExtractor

    _INTEL_AVAILABLE = True
except Exception:  # noqa: BLE001
    RuleClassifier = None  # type: ignore[assignment]
    RuleTodoExtractor = None  # type: ignore[assignment]
    _INTEL_AVAILABLE = False

#: 自检用合成文本（非真实聊天；覆盖多行、重复、跳过行、日期分隔）。
SELFTEST_TEXT = """消息记录（与 测试联系人 的聊天记录）
2024-01-01 星期一
这是一行无法识别的杂项（用于验证跳过行统计）
2024-01-01 12:30:00 测试联系人
你好，这是第一条合成消息
2024-01-01 12:31:00 我
收到，这是第二条
2024-01-01 12:31:00 我
收到，这是第二条
2024-01-01 12:32:00 测试联系人: 只有一行的消息
2024-01-01 12:33:00 测试联系人: 多行消息第一行
这是多行消息第二行
"""


# ---------------------------------------------------------------------------
# 纯函数（可被测试直接调用）
# ---------------------------------------------------------------------------

def _stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return moment.strftime("%Y%m%d_%H%M%S")


def _sha256_16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:  # pragma: no cover - 工作区外路径
        return path.name


def read_chat_file(path: str | Path) -> tuple[bytes, dict[str, Any]]:
    """读取用户选择的文件（含大小/哈希/扩展名信息；文件名只给脱敏指纹）。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{target}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"文件过大（{size} 字节 > {MAX_FILE_BYTES} 字节）。本工具只处理用户导出的聊天文本，"
            "不支持数据库/压缩包等大文件。"
        )
    data = target.read_bytes()
    info = {
        "extension": target.suffix.lower(),
        "size_bytes": size,
        "content_sha256_16": _sha256_16(data),
        "name_fingerprint": text_fingerprint(target.name),
    }
    return data, info


def build_import_options(
    *,
    file_name: str,
    conversation_title: str = "",
    self_names_text: str = "我",
    format_hint: str = "auto",
    now: datetime | None = None,
) -> ImportOptions:
    names = tuple(
        part.strip()
        for part in self_names_text.replace("，", ",").replace("、", ",").replace(";", ",").split(",")
        if part.strip()
    )
    return ImportOptions(
        format_hint=format_hint,
        conversation_title=conversation_title.strip(),
        source_name=file_name,
        self_sender_names=names or ("我",),
        now=now,
    )


def format_stats_text(result: ImportResult | None) -> str:
    if result is None:
        return "尚未解析：请选择文件后点击「解析预览」。"
    stats = result.stats
    lines = [
        f"识别格式：{result.format.label_zh}（{result.format.value}）",
        f"字符数：{stats.input_chars}    行数：{stats.input_lines}    编码：{result.encoding or '未知'}",
        f"解析消息：{stats.parsed_messages}    跳过行：{stats.skipped_lines}    重复移除：{stats.duplicate_messages}",
        f"有时间的消息：{stats.timestamped_messages}    无时间（COLLECTED）：{stats.untimed_messages}",
        f"发送者数：{stats.sender_count}    己方消息：{stats.self_messages}",
        f"解析成功率：{stats.parse_success_rate:.1%}    耗时：{stats.elapsed_ms} ms",
    ]
    if stats.truncated_input or stats.truncated_messages or stats.truncated_texts:
        lines.append(
            f"截断：输入={'是' if stats.truncated_input else '否'}，"
            f"消息上限={'是' if stats.truncated_messages else '否'}，"
            f"单条文本截断={stats.truncated_texts} 次"
        )
    if result.warnings:
        lines.append("")
        lines.append("警告（证据只记录码/数量，不含正文）：")
        for warning in result.warnings:
            lines.append(f"  [{warning.code.value}] {warning.message}（×{warning.count}）")
    if not result.ok:
        lines.append("")
        lines.append("未解析出消息：请检查文件是否为聊天文本，或手动选择格式后重试。")
    return "\n".join(lines)


def masked_preview(result: ImportResult | None, *, reveal: bool = False, limit: int = DEFAULT_PREVIEW_LIMIT) -> str:
    """消息预览。默认脱敏：只显示长度 + SHA-256 前缀；``reveal=True`` 仅本机显示原文。"""
    if result is None:
        return "尚未解析。"
    lines: list[str] = []
    for index, message in enumerate(result.messages[: max(0, limit)], start=1):
        raw = message.raw
        when = (
            raw.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if raw.timestamp is not None
            else "无时间"
        )
        if reveal:
            sender = raw.sender
            body = raw.text.replace("\r", "").replace("\n", "\\n")
        else:
            sender_fp = text_fingerprint(raw.sender)
            text_fp = text_fingerprint(raw.text)
            sender = f"[已脱敏 len={sender_fp['length']} sha={sender_fp['sha256_16'][:8]}]"
            body = f"[已脱敏 len={text_fp['length']} sha={text_fp['sha256_16'][:8]}]"
        lines.append(
            f"#{index:03d} 行{message.line_no:<5} {when} ({raw.timestamp_source.value})  "
            f"发送者 {sender}  内容 {body}"
        )
    if len(result.messages) > limit:
        lines.append(f"… 其余 {len(result.messages) - limit} 条消息未显示（仅预览前 {limit} 条）")
    if not lines:
        lines.append("（没有可预览的消息）")
    return "\n".join(lines)


def build_summary(result: ImportResult) -> ImportSummary:
    """实验性本地摘要：统计 + 可选地复用产品本地规则分类/待办（不联网）。"""
    raws = result.raw_messages
    summary = ImportSummary(
        message_count=len(raws),
        sender_count=len({raw.sender for raw in raws}),
        self_messages=sum(1 for raw in raws if raw.is_self),
        notes=[
            "实验性本地摘要：仅统计与本地规则，不调用外部 AI、不联网。",
            "摘要只用于本机验证，不是产品的「今日摘要」。",
        ],
    )
    day_counts: Counter[str] = Counter()
    stamps = [raw.timestamp for raw in raws if raw.timestamp is not None]
    for stamp in stamps:
        day_counts[stamp.astimezone().date().isoformat()] += 1
    summary.day_counts = dict(sorted(day_counts.items()))
    if stamps:
        summary.first_timestamp = min(stamps).isoformat()
        summary.last_timestamp = max(stamps).isoformat()

    if not _INTEL_AVAILABLE:
        summary.notes.append("本地规则层不可用，本摘要仅包含统计信息。")
        return summary

    try:  # pragma: no cover - 依赖 intel 实现细节
        classifier = RuleClassifier()  # type: ignore[misc]
        extractor = RuleTodoExtractor()  # type: ignore[misc]
        category_counts: Counter[str] = Counter()
        todo_count = 0
        for raw in raws:
            decision = classifier.classify(
                raw,
                conversation_title=result.conversation.title,
                conversation_kind=result.conversation.kind,
                conversation_priority=Priority.NORMAL,
            )
            category_counts[decision.category.value] += 1
            if not raw.is_self:
                todo_count += len(extractor.extract(raw))
        summary.category_counts = dict(category_counts)
        summary.todo_count = todo_count
    except Exception as exc:  # noqa: BLE001 - 摘要失败不影响导入结果
        summary.notes.append(f"本地规则摘要不可用（{type(exc).__name__}），已退化为统计。")
    return summary


def format_summary_text(summary: ImportSummary | None) -> str:
    if summary is None:
        return "尚未生成摘要：导入后自动生成。"
    lines = [
        f"消息数：{summary.message_count}    发送者数：{summary.sender_count}    己方消息：{summary.self_messages}",
    ]
    if summary.category_counts:
        parts = [f"{CATEGORY_LABELS.get(key, key)} {value}" for key, value in summary.category_counts.items()]
        lines.append("本地规则分类：" + "，".join(parts))
        lines.append(f"待办候选：{summary.todo_count} 条（本地规则，仅供参考）")
    if summary.day_counts:
        days = list(summary.day_counts.items())
        shown = "，".join(f"{day} {count} 条" for day, count in days[:7])
        if len(days) > 7:
            shown += f" … 共 {len(days)} 天"
        lines.append("按日期：" + shown)
    if summary.first_timestamp:
        lines.append(f"时间范围：{summary.first_timestamp} → {summary.last_timestamp}")
    lines.extend(f"说明：{note}" for note in summary.notes)
    return "\n".join(lines)


def build_evidence_payload(
    result: ImportResult,
    *,
    mode: str,
    file_info: dict[str, Any],
    report_dry_run: ImportRunReport | None = None,
    report_import: ImportRunReport | None = None,
    summary: ImportSummary | None = None,
    data_dir: Path | None = None,
    screenshot_paths: tuple[Path, ...] = (),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成**脱敏**证据字典（不含消息/发送者/文件名原文）。"""
    screenshots = [str(path) for path in screenshot_paths]
    payload: dict[str, Any] = {
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "experimental": EXPERIMENTAL,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "generated_at": datetime.now().astimezone().isoformat(),
            "mode": mode,
        },
        "notice": EXPERIMENTAL_NOTICE,
        "source_file": file_info,
        "parse": result.to_evidence_dict(max_entries=20),
        "dry_run": report_dry_run.to_dict() if report_dry_run is not None else None,
        "import": report_import.to_dict() if report_import is not None else None,
        "summary": summary.to_dict() if summary is not None else None,
        "test_data_dir": _relative(data_dir) if data_dir is not None else None,
        "screenshots": [_relative(Path(item)) for item in screenshots],
        "screenshot": _relative(Path(screenshots[0])) if screenshots else None,
        "privacy": {
            "evidence_contains_raw_text": False,
            "evidence_contains_raw_sender": False,
            "evidence_contains_source_file_name": False,
            "network_used": False,
            "qq_accessed": False,
            "note": "证据只保存长度、SHA-256、统计、警告码；原始聊天文本只在本机内存中处理。",
        },
        "limits": [
            "实验性：不保证兼容任意 QQ 导出/复制格式。",
            "只处理用户主动提供的文本文件，不自动读取 QQ。",
            "导入目标为独立测试数据目录，不是产品真实数据库，也不会自动同步。",
        ],
    }
    if extra:
        payload.update(extra)
    return payload


def write_evidence(payload: dict[str, Any], *, evidence_dir: Path | None = None) -> Path:
    target_dir = evidence_dir or EVIDENCE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    path = target_dir / f"import_{stamp}.json"
    counter = 2
    while path.exists():
        path = target_dir / f"import_{stamp}_{counter}.json"
        counter += 1
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _new_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    target = DATA_DIR / f"import_{stamp}"
    counter = 2
    while target.exists():
        target = DATA_DIR / f"import_{stamp}_{counter}"
        counter += 1
    return target


def clear_test_data(data_root: Path | None = None) -> int:
    """清空独立测试数据目录下的 import_* 子目录（绝不触碰其他目录）。"""
    root = (data_root or DATA_DIR).resolve()
    if not root.exists():
        return 0
    removed = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("import_"):
            continue
        # 双保险：必须是 data_root 的直接子目录
        if child.resolve().parent != root:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed


def _capture_with_printwindow(root: tk.Misc, path: Path) -> str | None:
    """用 Win32 PrintWindow 只截取本窗口（不受其他窗口遮挡/DPI 缩放影响）。"""
    try:  # pragma: no cover - 依赖真实桌面
        import ctypes
        from ctypes import wintypes

        from PIL import Image

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        class _BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        frame = root.wm_frame()
        hwnd = int(str(frame), 16)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 1 or height <= 1:
            return None
        window_dc = user32.GetWindowDC(hwnd)
        if not window_dc:
            return None
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        try:
            gdi32.SelectObject(memory_dc, bitmap)
            printed = user32.PrintWindow(hwnd, memory_dc, 0x00000002)  # PW_RENDERFULLCONTENT
            if not printed:
                printed = user32.PrintWindow(hwnd, memory_dc, 0)
            if not printed:
                return None
            header = _BitmapInfoHeader()
            header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            header.biWidth = width
            header.biHeight = -height
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            buffer = (ctypes.c_char * (width * height * 4))()
            gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
            image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)
            return str(path) if path.is_file() else None
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)
    except Exception:  # noqa: BLE001 - 截图失败不影响功能
        return None


def capture_window_image(root: tk.Misc, path: Path) -> str | None:
    """截图当前窗口（仅自检用；失败返回 None，不伪造）。

    优先用 Win32 ``PrintWindow``（只截本窗口，不受其他窗口遮挡影响）；
    失败时回退到屏幕抓取（按 DPI 缩放换算）。
    """
    root.update_idletasks()
    root.update()
    printed = _capture_with_printwindow(root, path)
    if printed:
        return printed
    try:  # pragma: no cover - 回退路径依赖真实桌面
        from PIL import ImageGrab

        try:
            scale = float(root.winfo_fpixels("1i")) / 96.0
        except (tk.TclError, ValueError):
            scale = 1.0
        scale = max(0.5, min(scale, 4.0))
        x = int(root.winfo_rootx() * scale)
        y = int(root.winfo_rooty() * scale)
        width = int(root.winfo_width() * scale)
        height = int(root.winfo_height() * scale)
        if width <= 1 or height <= 1:
            return None
        screen_width = int(root.winfo_screenwidth())
        screen_height = int(root.winfo_screenheight())
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(x + width, screen_width), min(y + height, screen_height)
        if x2 - x1 <= 1 or y2 - y1 <= 1:  # pragma: no cover - 窗口完全在屏幕外
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        image = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
        image.save(path)
        return str(path) if path.is_file() else None
    except Exception:  # noqa: BLE001 - 截图失败不影响功能，如实记录
        return None


# ---------------------------------------------------------------------------
# Tkinter 界面
# ---------------------------------------------------------------------------

class ImportChatTextApp:
    """实验性导入工具主界面。"""

    def __init__(self, root: tk.Tk, *, selftest: bool = False) -> None:
        self.root = root
        self.selftest = selftest
        self.file_path: Path | None = None
        self.file_info: dict[str, Any] | None = None
        self.result: ImportResult | None = None
        self.report_dry_run: ImportRunReport | None = None
        self.report_import: ImportRunReport | None = None
        self.summary: ImportSummary | None = None
        self.last_data_dir: Path | None = None
        self.last_evidence_path: Path | None = None
        self.last_screenshots: list[Path] = []
        self.last_import_ok = False

        self._build_ui()

    # -- UI 构建 ---------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title("导入聊天文本（实验性）— AI 社交秘书")
        # 本机典型 1536x864（DPI 缩放 1.33）下窗口需放得下，用户可自行最大化。
        self.root.geometry("1080x630+20+20")
        self.root.minsize(900, 560)

        style = ttk.Style(self.root)
        try:
            style.configure("Warn.TLabel", foreground="#a33")
            style.configure("Hint.TLabel", foreground="#555")
            style.configure("Title.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        except tk.TclError:  # pragma: no cover - 主题差异
            pass

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="导入聊天文本（实验性）", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=EXPERIMENTAL_NOTICE,
            style="Warn.TLabel",
            wraplength=960,
            justify="left",
        ).pack(anchor="w", pady=(4, 8))

        # 文件选择
        file_frame = ttk.LabelFrame(outer, text="1. 选择你自己导出的聊天文本文件（.txt / .json / .csv）", padding=8)
        file_frame.pack(fill="x", pady=4)
        self.file_var = tk.StringVar(value="（尚未选择文件）")
        self.file_meta_var = tk.StringVar(value="")
        ttk.Button(file_frame, text="选择文件…", command=self.on_choose_file).pack(side="right")
        ttk.Label(file_frame, textvariable=self.file_meta_var, style="Hint.TLabel").pack(
            side="right", padx=(8, 8)
        )
        ttk.Entry(file_frame, textvariable=self.file_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )

        # 选项
        opt_frame = ttk.LabelFrame(outer, text="2. 解析选项（可保持默认）", padding=8)
        opt_frame.pack(fill="x", pady=4)
        ttk.Label(opt_frame, text="会话名称：").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar(value="")
        ttk.Entry(opt_frame, textvariable=self.title_var, width=36).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Label(opt_frame, text="（留空则用默认名或文件里的聊天标题）", style="Hint.TLabel").grid(
            row=0, column=2, columnspan=2, sticky="w"
        )
        ttk.Label(opt_frame, text="我的昵称：").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.self_names_var = tk.StringVar(value="我")
        ttk.Entry(opt_frame, textvariable=self.self_names_var, width=36).grid(
            row=1, column=1, sticky="we", padx=4, pady=(4, 0)
        )
        ttk.Label(
            opt_frame,
            text="（多个用逗号分隔；无法可靠推断“我”，请按需填写）",
            style="Hint.TLabel",
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(opt_frame, text="文本格式：").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.format_var = tk.StringVar(value=FORMAT_CHOICES[0][0])
        ttk.Combobox(
            opt_frame,
            textvariable=self.format_var,
            values=[label for label, _hint in FORMAT_CHOICES],
            state="readonly",
            width=34,
        ).grid(row=2, column=1, sticky="we", padx=4, pady=(4, 0))
        ttk.Label(opt_frame, text="（默认自动识别；识别不准时可手动指定）", style="Hint.TLabel").grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(4, 0)
        )
        opt_frame.columnconfigure(1, weight=1)

        # 操作按钮
        action_frame = ttk.Frame(outer)
        action_frame.pack(fill="x", pady=8)
        ttk.Button(action_frame, text="解析预览（不写任何数据）", command=self.on_parse).pack(side="left")
        ttk.Button(action_frame, text="dry-run（只统计）", command=self.on_dry_run).pack(side="left", padx=6)
        ttk.Button(
            action_frame,
            text="导入到测试数据目录并生成摘要",
            command=self.on_import,
        ).pack(side="left", padx=6)
        ttk.Button(action_frame, text="打开测试数据目录", command=self.on_open_data_dir).pack(side="left", padx=6)
        ttk.Button(action_frame, text="清空我的测试数据", command=self.on_clear_data).pack(side="left", padx=6)
        ttk.Button(action_frame, text="退出（安全退出）", command=self.root.destroy).pack(side="right")

        # 结果区：用 Notebook 分页，小屏也能完整显示（默认打开“解析统计”页）
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, pady=4)

        stats_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(stats_frame, text="3. 解析统计（脱敏，不含正文）")
        self.stats_text = tk.Text(stats_frame, height=8, wrap="word", state="disabled")
        stats_scroll = ttk.Scrollbar(stats_frame, command=self.stats_text.yview)
        self.stats_text.configure(yscrollcommand=stats_scroll.set)
        self.stats_text.pack(side="left", fill="both", expand=True)
        stats_scroll.pack(side="right", fill="y")

        summary_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(summary_frame, text="4. 本地实验性摘要（导入后生成）")
        self.summary_text = tk.Text(summary_frame, height=8, wrap="word", state="disabled")
        summary_scroll = ttk.Scrollbar(summary_frame, command=self.summary_text.yview)
        self.summary_text.configure(yscrollcommand=summary_scroll.set)
        self.summary_text.pack(side="left", fill="both", expand=True)
        summary_scroll.pack(side="right", fill="y")

        preview_frame = ttk.Frame(self.notebook, padding=6)
        self.preview_tab_index = self.notebook.index("end")
        self.notebook.add(preview_frame, text="5. 消息预览（默认脱敏，只显示长度/哈希）")
        preview_header = ttk.Frame(preview_frame)
        preview_header.pack(fill="x")
        self.reveal_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            preview_header,
            text="在本机显示原文（仅预览，不写入证据；截图前请确认）",
            variable=self.reveal_var,
            command=self._refresh_preview,
        ).pack(side="left")
        self.preview_text = tk.Text(preview_frame, height=8, wrap="none", state="disabled")
        preview_scroll = ttk.Scrollbar(preview_frame, command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=preview_scroll.set)
        self.preview_text.pack(side="left", fill="both", expand=True)
        preview_scroll.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="就绪：请先选择文件，然后点击「解析预览」。")
        ttk.Label(outer, textvariable=self.status_var, style="Hint.TLabel", wraplength=960).pack(
            anchor="w", pady=(4, 0)
        )

    # -- 小工具 ---------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _info(self, title: str, message: str) -> None:
        if self.selftest:
            self._set_status(f"{title}: {message}")
            return
        messagebox.showinfo(title, message, parent=self.root)

    def _warn(self, title: str, message: str) -> None:
        if self.selftest:
            self._set_status(f"{title}: {message}")
            return
        messagebox.showwarning(title, message, parent=self.root)

    def _error(self, title: str, message: str) -> None:
        if self.selftest:
            self._set_status(f"{title}: {message}")
            return
        messagebox.showerror(title, message, parent=self.root)

    def _refresh_stats(self) -> None:
        self._set_text(self.stats_text, format_stats_text(self.result))

    def _refresh_summary(self) -> None:
        self._set_text(self.summary_text, format_summary_text(self.summary))

    def _refresh_preview(self) -> None:
        self._set_text(
            self.preview_text,
            masked_preview(
                self.result,
                reveal=bool(self.reveal_var.get()),
                limit=DEFAULT_PREVIEW_LIMIT,
            ),
        )

    # -- 文件 / 解析 -----------------------------------------------------
    def on_parse(self) -> None:
        self.parse_current()

    def on_choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择你自己导出的聊天文本（本工具不会自动读取 QQ）",
            filetypes=[
                ("聊天文本", "*.txt *.json *.csv *.log *.md"),
                ("所有文件", "*.*"),
            ],
        )
        if selected:
            self.load_path(selected, notify=True)

    def load_path(self, path: str | Path, *, notify: bool = False) -> None:
        target = Path(path)
        self.file_path = target
        self.result = None
        self.report_dry_run = None
        self.report_import = None
        self.summary = None
        self.last_import_ok = False
        try:
            data, info = read_chat_file(target)
        except (OSError, ValueError) as exc:
            self._error("无法读取文件", str(exc))
            return
        self.file_info = info
        if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            self._warn(
                "看起来不是文本文件",
                "所选文件包含二进制内容（可能是数据库、压缩包或媒体文件）。\n"
                "本工具只处理用户主动导出的聊天文本（.txt/.json/.csv），不会读取 QQ 本地数据库。",
            )
            self.file_path = None
            return
        # 只显示文件名（不显示完整路径）：隐私友好，也避免长路径把控件挤出窗口。
        self.file_var.set(target.name)
        self.file_meta_var.set(
            f"{info['size_bytes']} 字节  内容哈希 {info['content_sha256_16']}"
        )
        self._set_text(self.stats_text, "文件已选择：点击「解析预览」开始解析。")
        self._set_text(self.preview_text, "")
        self._set_text(self.summary_text, "")
        self._set_status("文件已选择：点击「解析预览」。")
        if notify:
            self.on_parse()

    def _current_format_hint(self) -> str:
        return _FORMAT_TO_HINT.get(self.format_var.get(), "auto")

    def parse_current(self) -> bool:
        if self.file_path is None:
            self._warn("请先选择文件", "请点击「选择文件…」选择你自己导出的聊天文本。")
            return False
        try:
            data, info = read_chat_file(self.file_path)
        except (OSError, ValueError) as exc:
            self._error("无法读取文件", str(exc))
            return False
        self.file_info = info
        options = build_import_options(
            file_name=self.file_path.name,
            conversation_title=self.title_var.get(),
            self_names_text=self.self_names_var.get(),
            format_hint=self._current_format_hint(),
        )
        try:
            result = parse_chat_bytes(data, format_hint=options.format_hint, options=options)
        except Exception as exc:  # noqa: BLE001 - 工具层兜底，不崩溃
            self._error("解析失败", f"解析器出现异常（{type(exc).__name__}），已停止。")
            return False
        self.result = result
        self._refresh_stats()
        self._refresh_preview()
        self._refresh_summary()
        if result.ok:
            self._set_status(
                f"解析完成：{len(result.messages)} 条消息（格式 {result.format.value}，"
                f"跳过 {result.stats.skipped_lines} 行，重复 {result.stats.duplicate_messages} 条）。"
            )
        else:
            self._set_status("解析完成：没有解析出消息，请查看统计中的警告。")
        return result.ok

    # -- dry-run / 导入 --------------------------------------------------
    def run_dry_run_internal(self) -> ImportRunReport | None:
        if self.result is None and not self.parse_current():
            return None
        if self.result is None:
            return None
        self.report_dry_run = run_import(self.result, None, dry_run=True)
        return self.report_dry_run

    def on_dry_run(self) -> None:
        report = self.run_dry_run_internal()
        if report is None:
            return
        evidence = self._write_evidence_safely(mode="dry-run")
        message = (
            f"dry-run 完成：\n可解析消息 {report.message_count} 条\n"
            f"写入数据：0 条（未写任何数据）\n\n"
            + (f"证据（脱敏）：{evidence}" if evidence else "证据写入失败（不影响 dry-run 结果）。")
        )
        self._info("dry-run 结果", message)
        self._set_status(f"dry-run 完成：{report.message_count} 条消息，未写入数据。")

    def do_import_internal(self, *, write_evidence: bool = True) -> bool:
        self.last_import_ok = False
        if self.result is None and not self.parse_current():
            return False
        if self.result is None:
            return False
        target = _new_data_dir()
        sink = JsonlMessageSink(target, include_text=False)  # 默认脱敏：不保存正文
        report = run_import(self.result, sink)
        self.report_import = report
        self.last_data_dir = target
        if not report.ok:
            self._error("导入失败", report.error or "未知错误，未写入任何数据。")
            return False
        self.summary = build_summary(self.result)
        self._refresh_summary()
        self.last_import_ok = True
        if write_evidence:
            self.last_evidence_path = self._write_evidence_safely(mode="gui-import")
        self._set_status(
            f"导入完成：写入 {report.written_count} 条到独立测试数据目录 "
            f"{_relative(target)}（不写产品数据库）。"
        )
        return True

    def on_import(self) -> None:
        if self.result is None and not self.parse_current():
            return
        target = _new_data_dir_preview()
        confirmed = messagebox.askyesno(
            "确认导入到独立测试数据目录",
            "将把解析结果写入本机独立测试数据目录：\n"
            f"{_relative(target)}\n\n"
            "• 这不是产品真实数据库，也不会自动同步；\n"
            "• 默认只保存长度+哈希，不保存聊天正文；\n"
            "• 证据文件会写到 evidence/import/。\n\n继续吗？",
            parent=self.root,
        )
        if not confirmed:
            self._set_status("已取消导入。")
            return
        if not self.do_import_internal():
            return
        evidence_text = str(self.last_evidence_path) if self.last_evidence_path else "（证据写入失败）"
        self._info(
            "导入完成",
            f"已写入 {self.report_import.written_count if self.report_import else 0} 条消息到：\n"
            f"{_relative(self.last_data_dir) if self.last_data_dir else ''}\n\n"
            f"本地摘要：{self.summary.message_count if self.summary else 0} 条消息，"
            f"待办候选 {self.summary.todo_count if self.summary else 0} 条。\n\n"
            f"脱敏证据：{evidence_text}",
        )

    def _write_evidence_safely(self, *, mode: str) -> Path | None:
        if self.result is None or self.file_info is None:
            return None
        try:
            payload = build_evidence_payload(
                self.result,
                mode=mode,
                file_info=self.file_info,
                report_dry_run=self.report_dry_run,
                report_import=self.report_import,
                summary=self.summary,
                data_dir=self.last_data_dir,
                screenshot_paths=tuple(self.last_screenshots),
            )
            path = write_evidence(payload)
            self.last_evidence_path = path
            return path
        except OSError as exc:
            self._warn("证据写入失败", f"无法写入证据文件：{exc}")
            return None

    # -- 数据目录 -------------------------------------------------------
    def on_open_data_dir(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(DATA_DIR))  # noqa: S606 - Windows 本地打开目录（非执行用户文件）
            self._set_status(f"已打开测试数据目录：{_relative(DATA_DIR)}")
        except OSError as exc:
            self._error("无法打开目录", str(exc))

    def on_clear_data(self) -> None:
        if not messagebox.askyesno(
            "确认清空测试数据",
            "将删除 evidence/import/data/ 下所有 import_* 测试数据目录。\n"
            "不会影响产品数据、原始聊天文件或证据 JSON。\n\n继续吗？",
            parent=self.root,
        ):
            return
        removed = clear_test_data()
        self._info("已清空", f"已删除 {removed} 个测试数据目录。")
        self._set_status(f"已清空 {removed} 个测试数据目录。")


def _new_data_dir_preview() -> Path:
    """仅用于确认对话框显示下一个导入目录（不创建目录）。"""
    return DATA_DIR / f"import_{_stamp()}"


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def run_selftest(*, show_window: bool = True, keep_open_ms: int = 1200) -> int:
    """真实创建 Tk 窗口 + 合成样例，完整跑一遍解析/dry-run/导入/摘要/证据/截图。"""
    checks: dict[str, Any] = {}
    evidence_path: Path | None = None
    failure: str | None = None
    root: tk.Tk | None = None
    app: ImportChatTextApp | None = None

    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        SELFTEST_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _stamp()
        sample_path = SELFTEST_DIR / f"sample_{stamp}.txt"
        sample_path.write_text(SELFTEST_TEXT, encoding="utf-8")
        checks["sample_written"] = sample_path.is_file()
        checks["sample_is_synthetic"] = "合成消息" in SELFTEST_TEXT

        root = tk.Tk()
        checks["tk_available"] = True
        app = ImportChatTextApp(root, selftest=True)
        try:  # pragma: no cover - 真实桌面
            root.state("zoomed")  # 自检截图用最大化窗口，保证内容完整
        except tk.TclError:
            pass
        root.update_idletasks()
        root.update()
        checks["window_created"] = bool(root.winfo_exists())

        app.load_path(sample_path)
        checks["file_loaded"] = app.file_path is not None
        checks["parse_ok"] = app.parse_current()
        result = app.result
        checks["parsed_messages"] = len(result.messages) if result else 0
        checks["format"] = result.format.value if result else ""
        checks["duplicates_removed"] = result.stats.duplicate_messages if result else 0
        checks["skipped_lines"] = result.stats.skipped_lines if result else 0
        checks["preview_masked"] = (
            "已脱敏" in masked_preview(result) if result is not None else False
        )

        dry = app.run_dry_run_internal()
        checks["dry_run_ok"] = bool(dry and dry.ok)
        checks["dry_run_written"] = dry.written_count if dry else -1

        app.do_import_internal(write_evidence=False)
        checks["import_ok"] = bool(app.last_import_ok and app.report_import and app.report_import.ok)
        checks["written_count"] = app.report_import.written_count if app.report_import else -1
        checks["summary_ok"] = bool(
            app.summary is not None
            and result is not None
            and app.summary.message_count == len(result.messages)
        )
        checks["data_files_written"] = bool(
            app.last_data_dir
            and (app.last_data_dir / "messages.jsonl").is_file()
            and (app.last_data_dir / "conversation.json").is_file()
        )
        checks["redacted_data_dir"] = bool(
            app.last_data_dir
            and (app.last_data_dir / "messages.jsonl").is_file()
            and "这是第一条合成消息" not in (app.last_data_dir / "messages.jsonl").read_text(encoding="utf-8")
        )

        root.update_idletasks()
        root.update()
        if show_window:
            try:  # pragma: no cover - 真实桌面
                root.attributes("-topmost", True)
                root.update()
            except tk.TclError:
                pass
            time.sleep(max(0.0, min(keep_open_ms, 10000) / 1000.0))
            root.update()

        screenshots: list[Path] = []
        # 截图 1：解析统计页（默认页）
        app.notebook.select(0)
        root.update_idletasks()
        root.update()
        first = capture_window_image(root, EVIDENCE_DIR / f"import_{stamp}.png")
        if first:
            screenshots.append(Path(first))
        # 截图 2：消息预览页（仍是脱敏模式）
        app.notebook.select(app.preview_tab_index)
        root.update_idletasks()
        root.update()
        second = capture_window_image(root, EVIDENCE_DIR / f"import_{stamp}_preview.png")
        if second:
            screenshots.append(Path(second))
        app.last_screenshots = screenshots
        checks["screenshot_ok"] = len(screenshots) >= 1
        checks["screenshot_count"] = len(screenshots)

        if result is not None and app.file_info is not None:
            payload = build_evidence_payload(
                result,
                mode="selftest",
                file_info=app.file_info,
                report_dry_run=app.report_dry_run,
                report_import=app.report_import,
                summary=app.summary,
                data_dir=app.last_data_dir,
                screenshot_paths=tuple(screenshots),
                extra={"selftest_checks": checks},
            )
            evidence_path = write_evidence(payload)
            checks["evidence_written"] = evidence_path.is_file()
        else:  # pragma: no cover - 自检失败路径
            checks["evidence_written"] = False
    except Exception as exc:  # noqa: BLE001 - 自检必须如实报告失败，不崩溃
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:  # pragma: no cover
                pass

    critical = (
        "tk_available",
        "window_created",
        "file_loaded",
        "parse_ok",
        "dry_run_ok",
        "import_ok",
        "summary_ok",
        "data_files_written",
        "evidence_written",
    )
    ok = failure is None and all(bool(checks.get(key)) for key in critical)
    report = {
        "ok": ok,
        "failure": failure,
        "checks": checks,
        "evidence": _relative(evidence_path) if evidence_path is not None else None,
    }
    print(json.dumps(report, ensure_ascii=True))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入聊天文本（实验性，Tkinter 本机工具）")
    parser.add_argument("--selftest", action="store_true", help="真实创建窗口自检（合成样例，无网络）")
    parser.add_argument("--no-window", action="store_true", help="自检时不显示窗口（仅创建 Tk 对象）")
    parser.add_argument("--keep-open-ms", type=int, default=1200, help="自检窗口停留毫秒数（默认 1200）")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest(show_window=not args.no_window, keep_open_ms=args.keep_open_ms)

    root = tk.Tk()
    ImportChatTextApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
