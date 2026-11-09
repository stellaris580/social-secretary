#!/usr/bin/env python
"""QQ 邮箱 IMAP 只读真机验证工具（Tkinter GUI，由用户**本人在本机**运行）。

用途
----
主程序需要用户在「设置 → 邮箱」里输入 QQ 邮箱授权码后才能真机同步。本工具让用户
先在本机做一次"只读验证"：输入 QQ 邮箱地址 + 16 位授权码 → 连接 ``imap.qq.com:993``
（IMAPS）→ ``EXAMINE INBOX`` 只读 → 读取最近 5 封邮件的头与正文摘要 → 在
``evidence/mail/mail_real_check_<ts>.json`` 写出**脱敏**结果。

为什么用 Tkinter 而不是 Qt
--------------------------
本机（Windows 11）上任何 Qt 进程创建 ``QApplication`` 都会在平台插件初始化阶段崩溃
（``0xC0000005``；NahimicOSD.dll / Alienware Sound Center 注入导致，PySide6 6.8/6.12、
PyQt5 均复现，与沙箱无关）。Tkinter 实测可用，故本工具使用 Python 标准库 Tkinter。

安全边界
--------
- 授权码只在本机进程内存中使用：不打印、不写日志、不写证据文件、不通过网络传输；
  只用于一次 IMAP ``LOGIN``（本工具没有 SMTP/发送/删除/标记能力）。
- 授权码输入框为 ``show="*"``；提交后立刻清空输入框与变量，worker 结束清空局部引用。
- 证据 JSON 只含：是否成功、服务器能力、邮件数、UID 范围、每封邮件的主题/正文
  **长度与 SHA-256 前 16 位**、发件人**域名**、附件数量/大小、耗时、错误码；
  不含授权码、邮件正文原文、完整邮箱地址。

运行::

    .venv\\Scripts\\python.exe tools\\mail_real_check.py                # 打开 GUI
    .venv\\Scripts\\python.exe tools\\mail_real_check.py --self-test    # 真实创建窗口自检（无网络）
    .venv\\Scripts\\python.exe tools\\mail_real_check.py --core-self-test  # 无 GUI 核心自检

本工具**不集成进主程序**，仅供第一阶段真机验证；主程序凭据入口由 UI 设置页实现。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_secretary.collectors.mail import (  # noqa: E402
    DEFAULT_TIMEOUT,
    MailAuthError,
    MailCollectorError,
    MailCredentialMissingError,
    MailErrorCode,
    MailProtocolError,
    ReadOnlyIMAP4SSL,
    ReadOnlyViolation,
    USER_MESSAGES,
    mask_email,
    parse_email,
    quietly_logout,
    translate_imap_exception,
)

DEFAULT_HOST = "imap.qq.com"
DEFAULT_PORT = 993
DEFAULT_COUNT = 5
#: 单封邮件最多下载 2 MB 正文做摘要；超过则只记录大小并跳过（避免大附件邮件）。
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
#: 摘要最多解析 4000 字符正文（只用于统计长度/哈希，不落盘）。
SUMMARY_BODY_CHARS = 4000

#: 工具内部/输入错误码（不属于主程序 MailErrorCode 枚举）。
TOOL_INPUT_INVALID = "MAIL_INPUT_INVALID"

_ACTION_HINTS: dict[MailErrorCode, str] = {
    MailErrorCode.CREDENTIAL_MISSING: "请先输入 16 位授权码；授权码不是 QQ 密码。",
    MailErrorCode.AUTH_FAILED: (
        "1) 确认输入的是 16 位授权码，不是 QQ 密码；"
        "2) 到网页版 mail.qq.com → 设置 → 账号 → POP3/IMAP/SMTP 服务，开启 IMAP 服务；"
        "3) 若近期改过 QQ 密码，旧授权码会失效，请重新生成授权码后重试。"
    ),
    MailErrorCode.CONNECTION_FAILED: (
        "无法连接 imap.qq.com:993。请检查网络；公司/校园网络可能封禁 993 端口，"
        "可尝试关闭代理或换手机热点后重试。"
    ),
    MailErrorCode.TIMEOUT: (
        "连接超时。请检查网络稳定性和代理设置；公司/校园网络可能限制 IMAP，可换网络重试。"
    ),
    MailErrorCode.TLS_FAILED: (
        "TLS/证书校验失败。请检查系统时间是否准确、是否处于需要认证的代理网络；"
        "不要为此关闭证书校验。"
    ),
    MailErrorCode.PROTOCOL_ERROR: (
        "服务器返回了无法识别的响应，可能被网络中间设备拦截；请换网络后重试。"
    ),
    MailErrorCode.FOLDER_NOT_FOUND: "收件箱（INBOX）不存在或不可访问，请确认邮箱状态。",
    MailErrorCode.READ_ONLY_VIOLATION: "只读安全护栏被触发，请把该错误码反馈给开发者。",
    MailErrorCode.UNEXPECTED: "请重试；若持续失败，请把错误码与时间反馈给开发者。",
    MailErrorCode.COLLECTOR_CLOSED: "验证会话已关闭，请重新开始。",
}


# ---------------------------------------------------------------------------
# 只读验证核心（无 GUI 依赖，便于自检/复用）
# ---------------------------------------------------------------------------

def check_qq_mail_readonly(
    email: str,
    auth_code: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    count: int = DEFAULT_COUNT,
    timeout: float = DEFAULT_TIMEOUT,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """连接真实 QQ 邮箱做一次只读验证，返回脱敏结果 dict（不抛异常）。

    只发送：CAPABILITY（连接阶段）、LOGIN、EXAMINE、UID SEARCH、UID FETCH(BODY.PEEK)、LOGOUT。
    """
    emit = progress or (lambda _message: None)
    started = time.perf_counter()
    result: dict[str, Any] = {
        "schema_version": 1,
        "tool": "tools/mail_real_check.py",
        "ok": False,
        "started_at": _now_iso(),
        "finished_at": None,
        "elapsed_ms": None,
        "account_masked": "",
        "target": {"host": host, "port": int(port), "mailbox": "INBOX"},
        "login_attempted": False,
        "read_only": True,
        "commands": [],
        "tls": None,
        "capabilities": [],
        "mailbox": {"exists": None, "uidvalidity": None, "uidnext": None},
        "requested_count": int(count),
        "fetched_count": 0,
        "uid_range": None,
        "emails": [],
        "errors": [],
        "privacy_note": (
            "本文件不含授权码、邮件正文原文、完整邮箱地址；主题/正文只记录长度与 SHA-256 前 16 位。"
        ),
    }
    conn: Any = None
    normalized_email = (email or "").strip()
    normalized_code = (auth_code or "").strip()
    try:
        if "@" not in normalized_email or normalized_email.startswith("@") or normalized_email.endswith("@"):
            return _finish(
                result,
                started,
                errors=[_input_error("邮箱地址格式不正确，请输入例如 123456789@qq.com。")],
            )
        result["account_masked"] = mask_email(normalized_email)
        if not normalized_code:
            raise MailCredentialMissingError()
        if count < 1:
            raise ValueError("count 必须 >= 1")

        emit(f"正在连接 {host}:{int(port)}（TLS，证书校验开启）…")
        conn = ReadOnlyIMAP4SSL(host, int(port), timeout=timeout)
        # imaplib 在构造连接时会自动发送 CAPABILITY。
        result["commands"].append("CAPABILITY")
        result["tls"] = _tls_summary(conn)
        result["capabilities"] = list(getattr(conn, "capabilities", ()))
        emit("TLS 握手成功，正在登录（只做 IMAP LOGIN，不发送任何邮件）…")

        result["login_attempted"] = True
        result["commands"].append("LOGIN")
        typ, _data = conn.login(normalized_email, normalized_code)
        if typ != "OK":
            raise MailAuthError()

        emit("登录成功，正在以只读方式打开收件箱（EXAMINE，不修改任何标志）…")
        result["commands"].append("EXAMINE")
        typ, _data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise MailProtocolError(f"EXAMINE INBOX 返回 {typ}")
        if not getattr(conn, "is_readonly", False):
            raise ReadOnlyViolation("邮箱未以只读方式打开")

        result["mailbox"]["exists"] = _exists_count(conn)
        result["mailbox"]["uidvalidity"] = _response_int(conn, "UIDVALIDITY")
        result["mailbox"]["uidnext"] = _response_int(conn, "UIDNEXT")
        emit(
            f"收件箱共 {result['mailbox']['exists']} 封邮件，"
            f"正在只读读取最近 {count} 封的头与正文摘要（BODY.PEEK）…"
        )

        result["commands"].append("UID SEARCH")
        all_uids = _search_uids(conn)
        selected_uids = all_uids[-count:] if count > 0 else []
        if selected_uids:
            result["uid_range"] = {"min": selected_uids[0], "max": selected_uids[-1]}

        for uid in selected_uids:
            record = _summarize_one_mail(conn, uid, emit=emit)
            if record is not None:
                result["emails"].append(record)

        result["fetched_count"] = len([item for item in result["emails"] if not item.get("skipped")])
        result["commands"].append("LOGOUT")
        result["ok"] = True
        emit("只读验证完成：未发送/删除/标记任何邮件。")
        return _finish(result, started)
    except MailCollectorError as exc:
        emit(f"验证失败：{exc.user_message}")
        return _finish(result, started, errors=[_error_entry(exc)])
    except Exception as exc:  # noqa: BLE001 - 工具必须给出可操作的失败信息而不是崩溃
        translated = translate_imap_exception(exc)
        emit(f"验证失败：{translated.user_message}")
        return _finish(result, started, errors=[_error_entry(translated)])
    finally:
        quietly_logout(conn)
        if conn is not None and "LOGOUT" not in result["commands"]:
            result["commands"].append("LOGOUT")
        # 尽力清理授权码引用（Python 字符串无法可靠擦除，这里只减少驻留）。
        normalized_code = ""


def _summarize_one_mail(
    conn: Any, uid: int, *, emit: Callable[[str], None]
) -> dict[str, Any] | None:
    """读取单封邮件并生成脱敏摘要；失败/超大时返回 skipped 记录。"""
    size = _fetch_size(conn, uid)
    if size is not None and size > MAX_MESSAGE_BYTES:
        emit(f"UID {uid} 超过 2 MB，已跳过正文下载。")
        return {"uid": uid, "size": size, "skipped": True, "reason": "超过大小上限"}
    raw = _fetch_body(conn, uid)
    if raw is None:
        return {"uid": uid, "size": size, "skipped": True, "reason": "服务器未返回正文"}
    if len(raw) > MAX_MESSAGE_BYTES:
        return {"uid": uid, "size": len(raw), "skipped": True, "reason": "超过大小上限"}

    parsed = parse_email(raw, max_body_chars=SUMMARY_BODY_CHARS)
    record: dict[str, Any] = {
        "uid": uid,
        "size": size if size is not None else len(raw),
        "subject_len": len(parsed.subject),
        "subject_sha256_16": _sha256_16(parsed.subject),
        "from_domain": _domain_of(parsed.from_address),
        "has_message_id": bool(parsed.message_id),
        "date": parsed.date.isoformat(timespec="seconds") if parsed.date else None,
        "content_type": parsed.content_type,
        "body_len": len(parsed.body_text),
        "body_sha256_16": _sha256_16(parsed.body_text),
        "attachment_count": parsed.attachment_count,
        "attachment_bytes": parsed.attachment_bytes,
        "parse_warnings_count": len(parsed.warnings),
    }
    emit(
        f"已读取 UID {uid}：主题 {record['subject_len']} 字、正文 {record['body_len']} 字、"
        f"附件 {record['attachment_count']} 个。"
    )
    return record


# ---------------------------------------------------------------------------
# IMAP 只读小工具（与 collector 内的实现保持同样的只读语义）
# ---------------------------------------------------------------------------

def _fetch_size(conn: Any, uid: int) -> int | None:
    typ, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
    if typ != "OK":
        raise MailProtocolError(f"UID FETCH RFC822.SIZE 返回 {typ}")
    for item in data or []:
        chunk = item[0] if isinstance(item, tuple) and item else item
        if not isinstance(chunk, (bytes, bytearray)):
            continue
        text = bytes(chunk)
        index = text.upper().find(b"RFC822.SIZE")
        if index < 0:
            continue
        tail = text[index + len(b"RFC822.SIZE") :].strip().split(b")")[0]
        try:
            return int(tail.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _fetch_body(conn: Any, uid: int) -> bytes | None:
    typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise MailProtocolError(f"UID FETCH BODY.PEEK[] 返回 {typ}")
    for item in data or []:
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        prefix, literal = item
        prefix_bytes = bytes(prefix) if isinstance(prefix, (bytes, bytearray)) else b""
        if isinstance(literal, (bytes, bytearray)) and b"BODY[]" in prefix_bytes.upper():
            return bytes(literal)
    return None


def _search_uids(conn: Any) -> list[int]:
    typ, data = conn.uid("SEARCH", "UID", "1:*")
    if typ != "OK":
        raise MailProtocolError(f"UID SEARCH 返回 {typ}")
    uids: set[int] = set()
    for item in data or []:
        if item is None:
            continue
        text = bytes(item).decode("ascii", "ignore") if isinstance(item, (bytes, bytearray)) else str(item)
        for token in text.split():
            if token.isdigit():
                uids.add(int(token))
    return sorted(uids)


def _exists_count(conn: Any) -> int | None:
    responses = getattr(conn, "untagged_responses", None) or {}
    values = responses.get("EXISTS") or []
    if not values:
        return None
    last = values[-1]
    if isinstance(last, (bytes, bytearray)):
        try:
            return int(bytes(last).strip())
        except ValueError:
            return None
    return last if isinstance(last, int) else None


def _response_int(conn: Any, code: str) -> int | None:
    try:
        _typ, data = conn.response(code)
    except Exception:  # noqa: BLE001
        return None
    for item in data or []:
        if isinstance(item, (bytes, bytearray)):
            try:
                return int(bytes(item).strip())
            except ValueError:
                continue
    return None


def _tls_summary(conn: Any) -> dict[str, Any] | None:
    sock = getattr(conn, "sock", None)
    if sock is None or not hasattr(sock, "version"):
        return None
    try:
        cipher = sock.cipher()
        return {"version": sock.version(), "cipher": cipher[0] if cipher else None}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 结果 / 证据（脱敏）
# ---------------------------------------------------------------------------

def _finish(result: dict[str, Any], started: float, *, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result["finished_at"] = _now_iso()
    result["elapsed_ms"] = max(0, int((time.perf_counter() - started) * 1000))
    if errors:
        result["errors"] = errors
        result["ok"] = False
    return result


def _error_entry(exc: MailCollectorError) -> dict[str, str]:
    code = exc.code
    return {
        "code": code.value,
        "message": USER_MESSAGES.get(code, USER_MESSAGES[MailErrorCode.UNEXPECTED]),
        "action": _ACTION_HINTS.get(code, ""),
    }


def _input_error(message: str) -> dict[str, str]:
    return {
        "code": TOOL_INPUT_INVALID,
        "message": message,
        "action": _ACTION_HINTS[MailErrorCode.CREDENTIAL_MISSING],
    }


def _unexpected_result() -> dict[str, Any]:
    """worker 内部异常兜底结果（绝不把异常原文暴露到 UI/证据）。"""
    result = _finish(
        {
            "schema_version": 1,
            "tool": "tools/mail_real_check.py",
            "ok": False,
            "started_at": _now_iso(),
            "finished_at": None,
            "elapsed_ms": None,
            "errors": [],
        },
        0.0,
        errors=[
            {
                "code": MailErrorCode.UNEXPECTED.value,
                "message": USER_MESSAGES[MailErrorCode.UNEXPECTED],
                "action": _ACTION_HINTS[MailErrorCode.UNEXPECTED],
            }
        ],
    )
    return result


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_16(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _domain_of(address: str) -> str:
    """只取发件人域名（不含本地部分），不构成完整邮箱地址。"""
    _local, sep, domain = (address or "").partition("@")
    return domain.strip().lower() if sep else ""


def default_evidence_dir() -> Path:
    return PROJECT_ROOT / "evidence" / "mail"


def write_evidence(result: dict[str, Any], out_dir: str | Path | None = None) -> Path:
    """把脱敏验证结果写到 evidence/mail/mail_real_check_<ts>.json。"""
    target_dir = Path(out_dir) if out_dir is not None else default_evidence_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = target_dir / f"mail_real_check_{stamp}.json"
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path


def _format_result_lines(result: dict[str, Any]) -> list[str]:
    """把脱敏结果格式化为界面/日志文本（纯函数，便于自检与测试）。"""
    lines: list[str] = []
    if result.get("ok"):
        lines.append("只读验证成功（未做任何写入）。")
        lines.append(f"账号：{result.get('account_masked', '')}")
        lines.append("")
        target = result.get("target") or {}
        lines.append(f"服务器：{target.get('host')}:{target.get('port')}（{target.get('mailbox')}，只读）")
        tls = result.get("tls") or {}
        if tls:
            lines.append(f"TLS：{tls.get('version')} / {tls.get('cipher')}")
        capabilities = result.get("capabilities") or []
        shown = ", ".join(capabilities[:10])
        lines.append(f"服务器能力：{shown}{' …' if len(capabilities) > 10 else ''}")
        mailbox = result.get("mailbox") or {}
        lines.append(
            f"收件箱：共 {mailbox.get('exists')} 封；UIDVALIDITY={mailbox.get('uidvalidity')}；"
            f"UIDNEXT={mailbox.get('uidnext')}"
        )
        lines.append(f"本次只读读取最近 {result.get('fetched_count')} 封；UID 范围：{result.get('uid_range')}")
        for record in result.get("emails", []):
            if record.get("skipped"):
                lines.append(f"  · UID {record.get('uid')}：跳过（{record.get('reason', '过大')}）")
                continue
            lines.append(
                f"  · UID {record.get('uid')}：主题 {record.get('subject_len')} 字、"
                f"正文 {record.get('body_len')} 字、发件域 {record.get('from_domain') or '—'}、"
                f"附件 {record.get('attachment_count')} 个"
            )
        lines.append("")
        lines.append("提示：本工具不显示邮件正文与授权码；以上均为脱敏统计。")
    else:
        for error in result.get("errors", []):
            lines.append(f"错误码：{error.get('code')}")
            lines.append(f"说明：{error.get('message')}")
        actions = [error.get("action") for error in result.get("errors", []) if error.get("action")]
        if actions:
            lines.append("")
            lines.append("处理建议：")
            lines.extend(f"  - {action}" for action in actions)
    evidence_path = result.get("evidence_path")
    if evidence_path:
        lines.append("")
        lines.append(f"脱敏证据已写入：{evidence_path}")
    if result.get("evidence_write_error"):
        lines.append(f"注意：{result['evidence_write_error']}")
    return lines


# ---------------------------------------------------------------------------
# GUI（Tkinter，标准库；本机 Qt 不可用，见模块 docstring）
# ---------------------------------------------------------------------------

def create_real_check_app(
    out_dir: str | Path | None = None,
    *,
    root_factory: Callable[[], Any] | None = None,
):
    """创建 Tk 根窗口与验证面板，返回 ``(root, app)``；不进入 mainloop。

    ``root_factory`` 仅测试/自检用（默认 ``tkinter.Tk``）。
    延迟 import tkinter：``--core-self-test`` 无需图形环境。
    """
    import tkinter as tk
    from tkinter import messagebox

    class RealCheckApp:
        def __init__(self, root: Any, evidence_dir: Path | None) -> None:
            self.root = root
            self._out_dir = evidence_dir
            self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
            self._worker: threading.Thread | None = None
            self._closing = False
            self._poll_after_id: str | None = None

            root.title("QQ 邮箱 IMAP 只读验证（本机工具，Tkinter）")
            root.geometry("800x640")
            root.minsize(700, 540)
            try:
                root.option_add("*Font", ("Microsoft YaHei UI", 10))
            except Exception:  # noqa: BLE001 - 字体不可用时用系统默认
                pass

            main = tk.Frame(root, padx=14, pady=12)
            main.pack(fill="both", expand=True)

            tk.Label(main, text="QQ 邮箱 IMAP 只读验证（本机运行）", font=("Microsoft YaHei UI", 15, "bold")).pack(
                anchor="w"
            )
            note = tk.Label(
                main,
                text=(
                    "本工具仅做只读验证：只发送 CAPABILITY / LOGIN / EXAMINE / SEARCH / FETCH(BODY.PEEK) / LOGOUT，"
                    "不会发送、删除、标记任何邮件。\n"
                    "授权码只在本机内存中使用，不打印、不写日志、不写证据文件、不上传网络。\n"
                    "证据写入 evidence/mail/，只保存脱敏统计（长度/哈希/发件人域名）。"
                ),
                justify="left",
                anchor="w",
                fg="#555555",
            )
            note.pack(fill="x", pady=(6, 10))

            form = tk.Frame(main)
            form.pack(fill="x")
            form.columnconfigure(1, weight=1)
            tk.Label(form, text="QQ 邮箱地址：").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
            self.email_var = tk.StringVar()
            self.email_entry = tk.Entry(form, textvariable=self.email_var, width=44)
            self.email_entry.grid(row=0, column=1, sticky="we", pady=4)
            tk.Label(form, text="邮箱授权码：").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
            self.code_var = tk.StringVar()
            self.code_entry = tk.Entry(form, textvariable=self.code_var, width=44, show="*")
            self.code_entry.grid(row=1, column=1, sticky="we", pady=4)

            hint = tk.Label(
                main,
                text=(
                    "不知道授权码？请见 docs/user-guide-mail-auth-code.md：网页版 mail.qq.com → 设置 → 账号 → "
                    "POP3/IMAP/SMTP 服务 → 开启 IMAP → 生成授权码（需要短信验证）。"
                ),
                justify="left",
                anchor="w",
                fg="#555555",
            )
            hint.pack(fill="x", pady=(8, 10))

            buttons = tk.Frame(main)
            buttons.pack(fill="x", pady=(0, 8))
            self.start_button = tk.Button(buttons, text="开始只读验证（最近 5 封）", command=self._on_start)
            self.start_button.pack(side="left")
            self.close_button = tk.Button(buttons, text="关闭", command=self._on_close)
            self.close_button.pack(side="left", padx=(8, 0))

            self.status_label = tk.Label(main, text="等待输入…", anchor="w", fg="#333333")
            self.status_label.pack(fill="x", pady=(0, 6))

            log_frame = tk.Frame(main)
            log_frame.pack(fill="both", expand=True)
            scrollbar = tk.Scrollbar(log_frame)
            scrollbar.pack(side="right", fill="y")
            self.log = tk.Text(log_frame, height=17, wrap="word", state="disabled", yscrollcommand=scrollbar.set)
            self.log.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=self.log.yview)

            root.protocol("WM_DELETE_WINDOW", self._on_close)
            self._poll_after_id = root.after(150, self._poll_queue)

        # -- 事件 ------------------------------------------------------
        def _on_start(self) -> None:
            email = self.email_var.get().strip()
            code = self.code_var.get().strip()
            if "@" not in email:
                messagebox.showwarning("提示", "请输入完整的 QQ 邮箱地址（例如 123456789@qq.com）。", parent=self.root)
                return
            if not code:
                messagebox.showwarning("提示", "请输入 16 位邮箱授权码（不是 QQ 密码）。", parent=self.root)
                return

            self._set_busy(True)
            self._clear_log()
            self.status_label.config(text="验证进行中…", fg="#333333")
            self._append("开始只读验证（不会发送/删除/标记任何邮件）。")
            # 先把授权码交给 worker 参数，再立刻清空输入框与变量（减少肩窥/驻留）。
            self.code_var.set("")
            try:
                self.code_entry.delete(0, tk.END)
            except tk.TclError:  # pragma: no cover - 窗口已销毁等极端情况
                pass
            self._worker = threading.Thread(
                target=self._run_worker,
                args=(email, code, self._out_dir),
                name="mail-real-check-worker",
                daemon=True,
            )
            self._worker.start()

        def _on_close(self) -> None:
            if self._worker is not None and self._worker.is_alive():
                messagebox.showinfo("请稍候", "只读验证仍在进行中，请等待完成后再关闭。", parent=self.root)
                return
            self._closing = True
            if self._poll_after_id is not None:
                try:
                    self.root.after_cancel(self._poll_after_id)
                except Exception:  # noqa: BLE001
                    pass
                self._poll_after_id = None
            self.root.destroy()

        # -- worker / 队列 ---------------------------------------------
        def _run_worker(self, email: str, code: str, evidence_dir: Path | None) -> None:
            try:
                result = check_qq_mail_readonly(
                    email,
                    code,
                    timeout=DEFAULT_TIMEOUT,
                    progress=lambda message: self._queue.put(("progress", message)),
                )
                try:
                    path = write_evidence(result, evidence_dir)
                    result["evidence_path"] = str(path)
                except OSError:
                    result["evidence_write_error"] = "无法写入 evidence/mail（请检查目录权限）"
            except Exception as exc:  # noqa: BLE001 - 兜底，不把异常原文暴露到 UI/证据
                result = _unexpected_result()
                result["error_type"] = type(exc).__name__
            finally:
                code = ""  # 尽力清理授权码局部引用
            self._queue.put(("done", result))

        def _poll_queue(self) -> None:
            if self._closing:
                return
            try:
                while True:
                    kind, payload = self._queue.get_nowait()
                    if kind == "progress":
                        self._append(str(payload))
                    else:
                        self._on_done(payload)
            except queue.Empty:
                pass
            if not self._closing:
                self._poll_after_id = self.root.after(150, self._poll_queue)

        def _on_done(self, result: dict[str, Any]) -> None:
            self._worker = None
            self._set_busy(False)
            self._render_result(result)
            if result.get("ok"):
                messagebox.showinfo(
                    "验证成功",
                    "只读验证成功（未做任何写入）。\n\n脱敏证据已写入 evidence/mail/。",
                    parent=self.root,
                )
            else:
                errors = result.get("errors") or []
                first = errors[0] if errors else {}
                messagebox.showerror(
                    "验证失败",
                    f"{first.get('code', 'MAIL_UNEXPECTED')}\n{first.get('message', '')}\n\n"
                    f"{first.get('action', '')}",
                    parent=self.root,
                )

        # -- 界面状态 --------------------------------------------------
        def _set_busy(self, busy: bool) -> None:
            state = "disabled" if busy else "normal"
            for widget in (self.email_entry, self.code_entry, self.start_button, self.close_button):
                try:
                    widget.config(state=state)
                except Exception:  # noqa: BLE001
                    pass

        def _clear_log(self) -> None:
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")

        def _append(self, message: str) -> None:
            self.log.config(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

        def _get_log_text(self) -> str:
            return self.log.get("1.0", "end-1c")

        def _render_result(self, result: dict[str, Any]) -> str:
            if result.get("ok"):
                self.status_label.config(text="✅ 只读验证成功（未做任何写入）", fg="#1a7f37")
            else:
                self.status_label.config(text="❌ 验证失败", fg="#b42318")
            text = "\n".join(_format_result_lines(result))
            self._clear_log()
            if text:
                self._append(text)
            return text

    root = root_factory() if root_factory is not None else tk.Tk()
    app = RealCheckApp(root, Path(out_dir) if out_dir is not None else None)
    return root, app


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def run_core_self_test() -> int:
    """无 GUI、无网络自检：输入校验 / 错误码 / 脱敏证据写入（可在沙箱/CI 运行）。"""
    import tempfile

    # 1) 输入校验失败：不建立网络连接、不尝试登录。
    bad_email = check_qq_mail_readonly("not-an-email", "whatever")
    assert bad_email["ok"] is False
    assert bad_email["errors"][0]["code"] == TOOL_INPUT_INVALID
    assert bad_email["login_attempted"] is False
    assert bad_email["commands"] == []

    # 2) 缺少授权码 → MAIL_CREDENTIAL_MISSING（中文提示），且不联网。
    missing = check_qq_mail_readonly("123456789@qq.com", "")
    assert missing["ok"] is False
    assert missing["errors"][0]["code"] == MailErrorCode.CREDENTIAL_MISSING.value
    assert missing["login_attempted"] is False
    assert missing["commands"] == []
    assert "授权码" in missing["errors"][0]["message"]

    # 3) 错误码映射 + 可操作提示。
    auth_entry = _error_entry(MailAuthError())
    assert auth_entry["code"] == MailErrorCode.AUTH_FAILED.value
    assert "授权码" in auth_entry["action"] and "IMAP" in auth_entry["action"]
    assert "imap.qq.com" in _ACTION_HINTS[MailErrorCode.CONNECTION_FAILED]
    assert mask_email("123456789@qq.com") == "1***@qq.com"
    assert _domain_of("sender@example.com") == "example.com"
    assert len(_sha256_16("任意文本")) == 16

    # 4) 脱敏证据：写入后逐字检查，绝不出现授权码/正文原文/完整邮箱地址。
    import uuid

    auth_sentinel = "AUTHCODE-SECRET-SENTINEL"
    body_sentinel = "机密正文原文-SENTINEL"
    synthetic = {
        "schema_version": 1,
        "tool": "tools/mail_real_check.py",
        "ok": True,
        "account_masked": mask_email("123456789@qq.com"),
        "target": {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "mailbox": "INBOX"},
        "login_attempted": True,
        "read_only": True,
        "commands": ["CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"],
        "tls": {"version": "TLSv1.2", "cipher": "ECDHE-RSA-AES128-GCM-SHA256"},
        "capabilities": ["IMAP4REV1", "ID", "NAMESPACE"],
        "mailbox": {"exists": 2, "uidvalidity": 1, "uidnext": 3},
        "requested_count": 5,
        "fetched_count": 1,
        "uid_range": {"min": 2, "max": 2},
        "emails": [
            {
                "uid": 2,
                "size": 2048,
                "subject_len": 12,
                "subject_sha256_16": _sha256_16("合成主题"),
                "from_domain": "example.com",
                "has_message_id": True,
                "date": "2026-10-31T22:40:00+08:00",
                "content_type": "text/plain",
                "body_len": len(body_sentinel),
                "body_sha256_16": _sha256_16(body_sentinel),
                "attachment_count": 1,
                "attachment_bytes": 1024,
                "parse_warnings_count": 0,
            }
        ],
        "errors": [],
        "elapsed_ms": 1234,
        "privacy_note": "本文件不含授权码、邮件正文原文、完整邮箱地址。",
    }
    # 用工作区内普通 mkdir 的临时目录（DSH 沙箱下 tempfile 的 0o700 目录会 PermissionError）。
    test_dir = PROJECT_ROOT / ".tmp" / f"mail-real-check-selftest-{uuid.uuid4().hex[:8]}"
    try:
        test_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        test_dir = Path(tempfile.mkdtemp(prefix="mail-real-check-selftest-"))
    try:
        path = write_evidence(synthetic, test_dir)
        text = path.read_text(encoding="utf-8")
    finally:
        import shutil

        shutil.rmtree(test_dir, ignore_errors=True)
    assert auth_sentinel not in text
    assert body_sentinel not in text
    assert "123456789@qq.com" not in text
    assert "1***@qq.com" in text
    parsed_back = json.loads(text)
    assert parsed_back["ok"] is True
    assert len(parsed_back["emails"][0]["subject_sha256_16"]) == 16
    assert len(parsed_back["emails"][0]["body_sha256_16"]) == 16
    assert parsed_back["read_only"] is True
    assert "LOGIN" in parsed_back["commands"] and "UID FETCH" in parsed_back["commands"]

    print(
        "CORE SELF-TEST OK：输入校验 / 错误码中文提示 / 脱敏证据写入 均通过"
        "（未联网，未使用真实授权码）。"
    )
    return 0


def run_self_test() -> int:
    """无网络 GUI 自检：**真实创建 Tkinter 窗口**，验证密码输入框/脱敏渲染/中文错误提示。"""
    try:
        root, app = create_real_check_app()
    except ImportError:
        print("未找到 Tkinter（Python 安装未包含 Tcl/Tk），无法运行 GUI 自检。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 无图形环境（如纯 SSH）时如实报告
        print(f"无法创建 Tkinter 窗口（{type(exc).__name__}），请在有桌面的本机运行。", file=sys.stderr)
        return 2
    try:
        # 1) 授权码输入框必须是密码模式，默认无内容。
        assert str(app.code_entry.cget("show")) == "*", "授权码输入框必须为密码模式"
        assert app.code_var.get() == ""
        assert str(app.email_entry.cget("show")) == "", "邮箱地址输入框不应为密码模式"

        # 2) 成功结果渲染：中文摘要 + 掩码账号 + 不出现完整邮箱/授权码。
        sample = {
            "schema_version": 1,
            "tool": "tools/mail_real_check.py",
            "ok": True,
            "account_masked": "1***@qq.com",
            "target": {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "mailbox": "INBOX"},
            "tls": {"version": "TLSv1.2", "cipher": "ECDHE-RSA-AES128-GCM-SHA256"},
            "capabilities": ["IMAP4REV1", "ID", "NAMESPACE"],
            "mailbox": {"exists": 123, "uidvalidity": 456, "uidnext": 124},
            "requested_count": DEFAULT_COUNT,
            "fetched_count": 1,
            "uid_range": {"min": 123, "max": 123},
            "emails": [
                {
                    "uid": 123,
                    "size": 1024,
                    "subject_len": 8,
                    "subject_sha256_16": "0123456789abcdef",
                    "from_domain": "example.com",
                    "date": "2026-10-31T22:40:00+08:00",
                    "content_type": "text/plain",
                    "body_len": 42,
                    "body_sha256_16": "fedcba9876543210",
                    "attachment_count": 0,
                    "attachment_bytes": 0,
                    "parse_warnings_count": 0,
                }
            ],
            "errors": [],
            "elapsed_ms": 1234,
        }
        success_text = app._render_result(sample)
        assert "只读验证成功" in success_text
        assert "1***@qq.com" in success_text
        assert "TLSv1.2" in success_text
        assert "123456789@qq.com" not in success_text

        # 3) 失败结果渲染：必须出现错误码与中文可操作提示。
        failure = {
            "ok": False,
            "errors": [
                {
                    "code": MailErrorCode.AUTH_FAILED.value,
                    "message": USER_MESSAGES[MailErrorCode.AUTH_FAILED],
                    "action": _ACTION_HINTS[MailErrorCode.AUTH_FAILED],
                }
            ],
        }
        failure_text = app._render_result(failure)
        assert "邮箱登录失败" in failure_text
        assert "授权码" in failure_text
        assert MailErrorCode.AUTH_FAILED.value in failure_text

        # 4) 授权码输入后立即清空变量；日志文本中绝不出现授权码。
        app.code_var.set("AUTHCODE-SECRET-SENTINEL")
        assert app.code_var.get() == "AUTHCODE-SECRET-SENTINEL"
        app.code_var.set("")
        root.update_idletasks()
        root.update()  # 真正驱动一次 Tk 事件循环
        assert "AUTHCODE-SECRET-SENTINEL" not in app._get_log_text()

        print(
            "SELF-TEST OK: Tkinter 窗口已实际创建；授权码密码框 / 脱敏成功渲染 / "
            "中文错误提示 / 授权码不落日志 均通过（未联网、未使用真实授权码）。"
        )
        return 0
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QQ 邮箱 IMAP 只读真机验证工具（Tkinter GUI；授权码只在本机内存中使用）"
    )
    parser.add_argument("--self-test", action="store_true", help="无网络 GUI 自检（真实创建 Tkinter 窗口）后退出")
    parser.add_argument(
        "--core-self-test",
        action="store_true",
        help="无 GUI、无网络自检核心逻辑与脱敏证据写入后退出（可在沙箱/CI 运行）",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="证据输出目录；默认 <仓库根>/evidence/mail",
    )
    args = parser.parse_args(argv)

    if args.core_self_test:
        return run_core_self_test()
    if args.self_test:
        return run_self_test()

    try:
        root, _app = create_real_check_app(
            Path(args.out_dir) if args.out_dir else None
        )
    except ImportError:
        print(
            "未找到 Tkinter（Python 安装未包含 Tcl/Tk），无法启动图形界面；请改用 --core-self-test。",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - 无图形环境时给出可操作提示
        print(f"无法创建 Tkinter 窗口（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
