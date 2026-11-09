#!/usr/bin/env python
"""QQ 邮箱完整流程真机验收工具（Tkinter，本机运行；P0-2）。

用途
----
一次性验证 v0.1.1 的「真实 QQ 邮箱授权 → 只读同步 → 增量去重 → 摘要生成」完整产品链路：

1. 用户本人在本机输入 QQ 邮箱地址 + 16 位授权码（授权码输入框 ``show="*"``，仅内存，
   提交后立即清空）；调用产品 ``ServiceAPI.set_mail_credential`` 写入 **Windows 凭据管理器**
   （key 与产品一致：``mail.auth_code.<email 小写>``，复用 ``security.credential_store``）；
2. 用产品 ``ServiceAPI``（``add_mail_account`` → ``set_mail_credential`` → ``test_mail_credential``
   → ``run_scan``）连接 ``imap.qq.com:993``，**EXAMINE INBOX 只读**同步最近 N 封（默认 20，可改）；
   采集器经产品注入点限制单次读取 N 封并优先读取最近邮件（跳过更早积压）；
3. 立即再同步一次，验证新邮件为 0、入库条数不变、内容哈希无重复（增量去重）；
4. 用真实入库的消息调用产品 ``generate_daily_summary()``（本地规则），展示计数/分类/待办；
5. 负向按钮/开关：在**隔离的临时数据库**中用错误授权码走同一产品链路，必须明确失败、
   0 条消息入库（证明不误报成功）；真实邮箱/真实凭据不受影响；
6. 输出脱敏 JSON 到 ``evidence/mail/real_acceptance_<ts>.json``：只含邮箱域名、消息数、
   UID 范围、增量结果、摘要计数、耗时、错误码、正文长度/哈希；**不含授权码/正文原文/完整邮箱地址**。

安全边界
--------
- 只读：只走产品 MailCollector 的 ``EXAMINE + BODY.PEEK[]`` 路径；不发送/删除/标记邮件；
- 授权码只在内存与本机 Windows 凭据管理器；不打印、不写日志、不写证据；
- 只操作用户本人邮箱；不访问其它文件夹/他人账号；窗口内不显示邮件正文与主题原文；
- 截图只截取「结果区」（不含输入框、不含真实正文）。

运行::

    .venv\\Scripts\\python.exe tools\\mail_acceptance.py                 # 图形界面（真实邮箱）
    .venv\\Scripts\\python.exe tools\\mail_acceptance.py --self-test     # Tkinter 窗口自检（无网络）
    .venv\\Scripts\\python.exe tools\\mail_acceptance.py --mock-self-test # mock IMAP 全流程自检（无真实凭据）

双击入口：仓库根目录 ``邮箱真机验收.cmd``（GBK）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_secretary.collectors.mail import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    MailCollector,
    USER_MESSAGES,
    mask_email,
)
from social_secretary.collectors.mail.errors import MailErrorCode  # noqa: E402
from social_secretary.config import APP_NAME, APP_VERSION, AppPaths  # noqa: E402
from social_secretary.contracts import (  # noqa: E402
    AccountKind,
    Category,
    ConsentScope,
    ScanStatus,
)
from social_secretary.core.service import (  # noqa: E402
    NotFoundError,
    ServiceError,
    SocialSecretaryService,
)
from social_secretary.security.credential_store import (  # noqa: E402
    InMemoryCredentialStore,
)
from social_secretary.security.log_redaction import redact_text, safe_error  # noqa: E402

DEFAULT_COUNT = 20
MAX_COUNT = 200
#: 负向测试用的"错误授权码"：16 位、明显错误，不会与真实授权码相同。
DEFAULT_WRONG_AUTH_CODE = "wrong-auth-code-00"
EVIDENCE_PREFIX_REAL = "real_acceptance"
EVIDENCE_PREFIX_MOCK = "real_acceptance_mock"
MODE_REAL = "real"
MODE_MOCK = "mock"
_SCAN_OK_STATUSES = {ScanStatus.OK}
_UID_RE = re.compile(r"UID\s+(\d+)", re.IGNORECASE)
_MAIL_CODE_RE = re.compile(r"MAIL_[A-Z_]+")


class AcceptanceInputError(ValueError):
    """输入不合法（邮箱格式/条数）；GUI 会在开始前拦截并给出中文提示。"""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _domain_of(email: str) -> str:
    _local, sep, domain = (email or "").strip().partition("@")
    return domain.strip().lower() if sep else ""


def _extract_mail_code(text: str | None) -> str | None:
    match = _MAIL_CODE_RE.search(text or "")
    return match.group(0) if match else None


def _account_messages(service: Any, account_id: str, *, limit: int = 5000) -> list[Any]:
    """返回该账号已入库消息（不含演示数据）。"""
    try:
        messages = service.list_messages(limit=limit)
    except Exception:  # noqa: BLE001 - 只读统计失败不应中断验收
        return []
    return [message for message in messages if message.account_id == account_id]


def _uid_range(messages: list[Any]) -> dict[str, Any] | None:
    uids = sorted(
        int(match.group(1))
        for message in messages
        for match in [_UID_RE.search(message.source_locator or "")]
        if match
    )
    if not uids:
        return None
    return {"min": uids[0], "max": uids[-1], "count": len(uids)}


def _category_counts(messages: list[Any]) -> dict[str, int]:
    counts = {"important": 0, "normal": 0, "fyi": 0}
    for message in messages:
        category = getattr(message, "category", None)
        key = getattr(category, "value", str(category))
        if key in counts:
            counts[key] += 1
    return counts


def _duplicate_hash_count(messages: list[Any]) -> int:
    hashes = [getattr(message, "content_hash", "") for message in messages]
    return len(hashes) - len(set(hashes))


def _scan_record(results: list[Any]) -> dict[str, Any]:
    """把产品 CollectionResult 列表转成脱敏记录（不含正文/会话标题）。"""
    statuses = [getattr(result.status, "value", str(result.status)) for result in results]
    errors = [redact_text(str(result.error), max_len=160) for result in results if result.error]
    warnings = [
        redact_text(str(warning), max_len=160)
        for result in results
        for warning in (result.warnings or ())
    ]
    return {
        "statuses": statuses,
        "all_ok": bool(results) and all(status == ScanStatus.OK.value for status in statuses),
        "messages_seen": sum(int(result.messages_seen) for result in results),
        "messages_new": sum(int(result.messages_new) for result in results),
        "conversations_scanned": sum(int(result.conversations_scanned) for result in results),
        "duration_ms": sum(int(result.duration_ms) for result in results),
        "errors": errors[:3],
        "error_code": _extract_mail_code(" ".join(errors)),
        "warnings_count": len(warnings),
        "warnings": warnings[:5],
    }


def _summary_record(summary: Any) -> dict[str, Any]:
    counts = dict(getattr(summary, "counts", {}) or {})
    todos = tuple(getattr(summary, "todos", ()) or ())
    open_todos = sum(
        1 for todo in todos if getattr(getattr(todo, "status", None), "value", "") == "open"
    )
    return {
        "day": getattr(summary, "day", None).isoformat() if getattr(summary, "day", None) else None,
        "counts": {
            "total": int(counts.get("total", 0)),
            "important": int(counts.get("important", 0)),
            "normal": int(counts.get("normal", 0)),
            "fyi": int(counts.get("fyi", 0)),
            "todos": int(counts.get("todos", 0)),
            "conversations": int(counts.get("conversations", 0)),
        },
        "todos_total": len(todos),
        "open_todos": open_todos,
        "scan_status": getattr(getattr(summary, "scan_status", None), "value", None),
        "is_demo": bool(getattr(summary, "is_demo", False)),
        "caveats_count": len(getattr(summary, "caveats", ()) or ()),
    }


def _latest_message_day(messages: list[Any]) -> date | None:
    try:
        from social_secretary.core.storage import local_day_of
    except Exception:  # noqa: BLE001 - 产品内部工具不可用时退化为本地日期
        local_day_of = lambda value: value.astimezone().date().isoformat()  # noqa: E731

    days: set[str] = set()
    for message in messages:
        value = getattr(message, "timestamp", None) or getattr(message, "collected_at", None)
        if value is None:
            continue
        try:
            days.add(local_day_of(value))
        except Exception:  # noqa: BLE001
            continue
    if not days:
        return None
    try:
        return date.fromisoformat(max(days))
    except ValueError:
        return None


def _temp_workspace_dir(prefix: str) -> Path:
    """临时目录：优先工作区 .tmp（DSH 沙箱下系统 tempfile 的 0o700 目录不可用），否则系统临时目录。"""
    base = PROJECT_ROOT / ".tmp"
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{prefix}{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        path.mkdir()
        return path
    except OSError:
        return Path(tempfile.mkdtemp(prefix=prefix))


# ---------------------------------------------------------------------------
# 产品采集器接线（source_factory 注入：限制单次 N 封、首次优先最近邮件）
# ---------------------------------------------------------------------------

def _make_mail_factory(
    holder: dict[str, Any],
    *,
    count: int,
    endpoint: dict[str, Any] | None,
) -> Callable[[Any], Any]:
    """构造 source_factory：用产品 MailCollector 实现，仅加"单次封顶/最近优先"参数。"""
    options = dict(endpoint or {})
    host = str(options.get("host") or DEFAULT_HOST)
    port = int(options.get("port") or DEFAULT_PORT)
    insecure = bool(options.get("insecure_plaintext", False))
    timeout = float(options.get("timeout") or 20.0)

    def factory(account: Any) -> Any:
        service = holder.get("service")
        if service is None:
            return None
        return MailCollector(
            account=account,
            credential_store=service.credential_store,
            sync_state=service.mail_sync_state_store,
            host=host,
            port=port,
            insecure_plaintext=insecure,
            timeout=timeout,
            fetch_limit_cap=count,
            initial_sync_newest=True,
        )

    return factory


# ---------------------------------------------------------------------------
# 负向测试（隔离数据库 + 内存凭据；不碰真实凭据/真实数据库）
# ---------------------------------------------------------------------------

def run_negative_check(
    *,
    email: str,
    wrong_auth_code: str = DEFAULT_WRONG_AUTH_CODE,
    data_dir: Path | None = None,
    mail_endpoint: dict[str, Any] | None = None,
    count: int = 5,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """用错误授权码跑一次产品链路，断言"明确失败且 0 条入库"。

    使用独立的临时数据目录与 ``InMemoryCredentialStore``：
    - 不会覆盖真实 Windows 凭据管理器中的授权码；
    - 不会向产品真实数据库写入任何消息。
    """
    emit = progress or (lambda _message: None)
    started = time.perf_counter()
    temp_dir = Path(data_dir) if data_dir is not None else _temp_workspace_dir("mail-acceptance-negative-")
    record: dict[str, Any] = {
        "attempted": True,
        "isolation": {
            "temporary_data_dir": data_dir is None,
            "in_memory_credentials": True,
            "product_data_untouched": True,
        },
        "credential_store": "in-memory-test",
        "auth_check_ok": None,
        "scan": None,
        "messages_stored": None,
        "misreported_success": None,
        "error_code": None,
        "error": None,
        "duration_ms": None,
    }
    service = None
    holder: dict[str, Any] = {}
    try:
        paths = AppPaths.create(temp_dir)
        service = SocialSecretaryService(
            paths,
            credential_store=InMemoryCredentialStore(),
            source_factory=_make_mail_factory(holder, count=max(1, min(count, MAX_COUNT)), endpoint=mail_endpoint),
        )
        holder["service"] = service
        emit("负向测试：使用错误授权码登录（只尝试一次，不读取邮件）…")
        account = service.add_mail_account(email, display_name="验收负向测试（临时）", folder="INBOX")
        # 先确认错误码确实被写入隔离凭据库（不触碰真实凭据管理器）。
        service.set_mail_credential(email, wrong_auth_code)
        service.grant_consent(
            ConsentScope.MAIL_READ,
            confirmation_text="邮箱真机验收负向测试：仅用于验证错误授权码不会误报成功",
        )
        service.resume_collection()
        check = service.test_mail_credential(email)
        record["auth_check_ok"] = bool(check.ok)
        record["error_code"] = check.discovered.get("code") if getattr(check, "discovered", None) else None
        if not check.ok:
            record["error"] = redact_text(str(check.detail), max_len=160)
        emit(f"负向测试：连接检查 ok={check.ok}（期望 False），继续验证 0 条入库…")
        results = service.run_scan(account.id, trigger="acceptance-negative")
        record["scan"] = _scan_record(results)
        stats = service.get_storage_stats()
        record["messages_stored"] = int(stats.get("message_count", 0))
        scan_success = any(
            getattr(result.status, "value", "") == ScanStatus.OK.value for result in results
        )
        record["misreported_success"] = bool(check.ok or scan_success or record["messages_stored"] > 0)
        emit(
            "负向测试完成："
            f"误报成功={record['misreported_success']}，入库={record['messages_stored']} 条。"
        )
    except Exception as exc:  # noqa: BLE001 - 负向测试失败也要如实记录，不崩溃
        record["error"] = safe_error(exc, max_len=160)
        record["misreported_success"] = None
        emit(f"负向测试异常（已记录）：{type(exc).__name__}")
    finally:
        if service is not None:
            try:
                service.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if data_dir is None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        record["duration_ms"] = _elapsed_ms(started)
    record["ok"] = bool(
        record["auth_check_ok"] is False
        and record["misreported_success"] is False
        and (record["messages_stored"] or 0) == 0
        and record["scan"] is not None
        and not record["scan"]["all_ok"]
    )
    return record


# ---------------------------------------------------------------------------
# 主验收流程
# ---------------------------------------------------------------------------

#: QQ 个人邮箱常见域名（本验收工具固定连接 imap.qq.com，仅支持个人邮箱）。
ALLOWED_QQ_DOMAINS: tuple[str, ...] = ("qq.com", "vip.qq.com", "foxmail.com")


def validate_acceptance_input(email: str, auth_code: str, count: int) -> None:
    """校验验收输入；不合法时抛 ``AcceptanceInputError``（中文、可展示给用户）。

    邮箱域名校验用于拦截 ``qq.cm`` 这类少字母的输入错误（曾导致用户真实验收失败）。
    """
    normalized_email = (email or "").strip()
    if "@" not in normalized_email or normalized_email.startswith("@") or normalized_email.endswith("@"):
        raise AcceptanceInputError("邮箱地址格式不正确，请输入例如 123456789@qq.com。")
    domain = _domain_of(normalized_email)
    if domain not in ALLOWED_QQ_DOMAINS:
        raise AcceptanceInputError(
            f"邮箱域名「{domain or '（空）'}」不像 QQ 个人邮箱（支持 qq.com / vip.qq.com / foxmail.com）。"
            "请检查地址是否输错（例如 qq.cm 少了一个字母 o）。"
        )
    if not (auth_code or "").strip():
        raise AcceptanceInputError("请先输入 16 位 QQ 邮箱授权码（不是 QQ 密码）。")
    if not isinstance(count, int) or isinstance(count, bool) or not (1 <= count <= MAX_COUNT):
        raise AcceptanceInputError(f"同步条数 N 必须是 1~{MAX_COUNT} 的整数。")


def run_mail_acceptance(
    *,
    email: str,
    auth_code: str,
    count: int = DEFAULT_COUNT,
    data_dir: str | Path | None = None,
    credential_store: Any | None = None,
    mail_endpoint: dict[str, Any] | None = None,
    run_negative: bool = True,
    negative_auth_code: str = DEFAULT_WRONG_AUTH_CODE,
    evidence_dir: str | Path | None = None,
    evidence_prefix: str = EVIDENCE_PREFIX_REAL,
    mode: str = MODE_REAL,
    progress: Callable[[str], None] | None = None,
    readonly_probe: Callable[[], dict[str, Any]] | None = None,
    write_evidence_file: bool = True,
) -> dict[str, Any]:
    """执行完整验收并返回脱敏结果 dict（同时写 evidence/mail/real_acceptance_<ts>.json）。"""
    emit = progress or (lambda _message: None)
    normalized_email = (email or "").strip()
    normalized_code = (auth_code or "").strip()
    validate_acceptance_input(normalized_email, normalized_code, count)

    started = time.perf_counter()
    timings: dict[str, int] = {}
    result: dict[str, Any] = {
        "schema_version": 1,
        "tool": "tools/mail_acceptance.py",
        "mode": mode,
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "ok": False,
        "started_at": _now_iso(),
        "finished_at": None,
        "elapsed_ms": None,
        "email_masked": mask_email(normalized_email),
        "email_domain": _domain_of(normalized_email),
        "requested_count": count,
        "data_dir_kind": "custom" if data_dir is not None else "product-default",
        "credential_store": type(credential_store).__name__ if credential_store is not None else "keyring",
        "checks": {},
        "connection": None,
        "account": None,
        "first_sync": None,
        "incremental_sync": None,
        "summary": None,
        "negative": None,
        "readonly": {
            "mechanism": "EXAMINE + BODY.PEEK[] + IMAP 命令白名单；不发送/删除/标记邮件",
            "server_verified": readonly_probe is not None,
        },
        "warnings": [],
        "errors": [],
        "privacy_note": (
            "本文件不含授权码、邮件正文原文、完整邮箱地址；仅记录域名、计数、UID 范围、"
            "摘要计数与耗时。"
        ),
    }
    service = None
    holder: dict[str, Any] = {}
    try:
        paths = AppPaths.create(Path(data_dir) if data_dir is not None else None)
        factory = _make_mail_factory(holder, count=count, endpoint=mail_endpoint)
        service = SocialSecretaryService(
            paths, credential_store=credential_store, source_factory=factory
        )
        holder["service"] = service

        # 1) 账号与凭据（产品 API：写入系统凭据管理器）
        emit("步骤 1/6：准备产品账号并保存授权码到 Windows 凭据管理器…")
        step_started = time.perf_counter()
        existing = next(
            (
                account
                for account in service.list_accounts()
                if account.kind == AccountKind.EMAIL
                and (account.identifier or "").strip().lower() == normalized_email.lower()
            ),
            None,
        )
        account_reused = existing is not None
        account = existing or service.add_mail_account(
            normalized_email, display_name=f"QQ邮箱（{result['email_domain']}）", folder="INBOX"
        )
        service.set_mail_credential(normalized_email, normalized_code)
        credential_saved = bool(service.has_mail_credential(normalized_email))
        result["account"] = {
            "kind": AccountKind.EMAIL.value,
            "domain": result["email_domain"],
            "account_reused": account_reused,
        }
        timings["prepare_ms"] = _elapsed_ms(step_started)
        result["credential"] = {
            "saved": credential_saved,
            "key_pattern": "mail.auth_code.<email 小写>",
            "store": "windows-credential-manager" if credential_store is None else "in-memory-test",
        }
        result["checks"]["credential_saved"] = credential_saved
        if not credential_saved:
            result["errors"].append("授权码未能写入 Windows 凭据管理器")
            emit("授权码保存失败，验收终止。")
            raise ServiceError("授权码未能写入 Windows 凭据管理器")

        # 2) 只读同意 + 恢复采集（产品同意门控/暂停开关）
        emit("步骤 2/6：确认只读同意并恢复采集（产品开关）…")
        settings = service.get_settings()
        consent_granted = bool(getattr(settings, "mail_read_consent", False))
        if not consent_granted:
            service.grant_consent(
                ConsentScope.MAIL_READ,
                confirmation_text="邮箱真机验收：仅只读采集我本人的邮箱（EXAMINE/BODY.PEEK）",
            )
            consent_granted = bool(getattr(service.get_settings(), "mail_read_consent", False))
        if bool(getattr(service.get_settings(), "collection_paused", True)):
            service.resume_collection()
        result["consent_granted"] = consent_granted
        result["checks"]["consent_granted"] = consent_granted

        # 3) 连接/认证自检（产品 test_mail_credential → MailCollector.test_connection）
        emit("步骤 3/6：连接 imap.qq.com:993 并做只读 EXAMINE INBOX 自检…")
        step_started = time.perf_counter()
        check = service.test_mail_credential(normalized_email)
        timings["connection_ms"] = _elapsed_ms(step_started)
        discovered = dict(getattr(check, "discovered", None) or {})
        connection = {
            "ok": bool(check.ok),
            "latency_ms": getattr(check, "latency_ms", None),
            "uidvalidity": discovered.get("uidvalidity"),
            "server_message_count": discovered.get("message_count"),
            "capabilities_count": len(discovered.get("capabilities") or ()),
            "tls": discovered.get("tls"),
            "readonly": discovered.get("readonly"),
            "code": discovered.get("code"),
            "detail": None if check.ok else redact_text(str(check.detail), max_len=200),
        }
        result["connection"] = connection
        result["checks"]["connection_ok"] = bool(check.ok)
        if not check.ok:
            result["errors"].append(
                f"邮箱连接/认证失败（{connection['code'] or '未知错误'}）：{connection['detail'] or ''}".strip()
            )
            emit(f"连接自检失败：{connection['code'] or ''} {connection['detail'] or ''}")
            raise ServiceError(connection["detail"] or "邮箱连接/认证失败")

        # 只读性证据（真实运行）：产品 test_connection 以 EXAMINE 只读方式打开 INBOX 并断言
        # imaplib 的 is_readonly；该结果就是"EXAMINE INBOX 只读"的实际证据。
        examine_readonly = bool(discovered.get("readonly"))
        result["readonly"].update(
            {
                "examine_readonly": examine_readonly,
                "verified_by": (
                    "产品 test_mail_credential → MailCollector.test_connection："
                    "EXAMINE INBOX 只读打开并断言 is_readonly"
                ),
            }
        )
        result["checks"]["readonly_ok"] = examine_readonly

        # 4) 首次只读同步（产品 run_scan 全链路：采集→去重→落库→分类→待办）
        emit(f"步骤 4/6：产品 run_scan 只读同步最近 {count} 封邮件…")
        step_started = time.perf_counter()
        messages_before = _account_messages(service, account.id)
        stored_before = len(messages_before)
        first_results = service.run_scan(account.id, trigger="acceptance")
        first_record = _scan_record(first_results)
        messages_after_first = _account_messages(service, account.id)
        first_record["stored_before"] = stored_before
        first_record["stored_after"] = len(messages_after_first)
        first_record["uid_range"] = _uid_range(messages_after_first)
        first_record["category_counts"] = _category_counts(messages_after_first)
        timings["first_sync_ms"] = _elapsed_ms(step_started)
        result["first_sync"] = first_record
        result["checks"]["sync_ok"] = first_record["all_ok"]
        result["checks"]["data_present"] = len(messages_after_first) > 0
        result["warnings"].extend(first_record["warnings"])
        if not first_record["all_ok"]:
            result["errors"].append(
                f"首次同步状态异常：{', '.join(first_record['statuses'])}"
                + (f"（{first_record['errors'][0]}）" if first_record["errors"] else "")
            )
        if len(messages_after_first) == 0:
            result["errors"].append("收件箱没有可读取的邮件：无法验收摘要/分类/待办，请稍后重试")
            emit("警告：本次没有同步到任何邮件。")

        # 5) 第二次同步：验证增量 0 新增 / 不重复入库
        emit("步骤 5/6：再次 run_scan 验证增量（应 0 新增、入库条数不变）…")
        step_started = time.perf_counter()
        second_results = service.run_scan(account.id, trigger="acceptance-incremental")
        second_record = _scan_record(second_results)
        messages_after_second = _account_messages(service, account.id)
        stored_after_second = len(messages_after_second)
        duplicate_hashes = _duplicate_hash_count(messages_after_second)
        second_record.update(
            {
                "stored_before": len(messages_after_first),
                "stored_after": stored_after_second,
                "duplicate_content_hashes": duplicate_hashes,
                "uid_range": _uid_range(messages_after_second),
            }
        )
        incremental_ok = bool(
            second_record["all_ok"]
            and second_record["messages_new"] == 0
            and stored_after_second == len(messages_after_first)
        )
        second_record["incremental_ok"] = incremental_ok
        timings["incremental_sync_ms"] = _elapsed_ms(step_started)
        result["incremental_sync"] = second_record
        result["checks"]["incremental_ok"] = incremental_ok
        result["checks"]["no_duplicate_ok"] = duplicate_hashes == 0
        result["warnings"].extend(second_record["warnings"])
        if not incremental_ok:
            result["errors"].append(
                "增量校验失败：第二次同步仍新增消息或入库条数变化（可能重复入库）"
            )
        emit(
            f"增量结果：新增 {second_record['messages_new']} 条，"
            f"入库 {len(messages_after_first)}→{stored_after_second} 条。"
        )

        # 6) 摘要（产品 generate_daily_summary；真实邮件日期可能不在今天，补一次最新邮件日）
        emit("步骤 6/6：调用产品 generate_daily_summary() 生成摘要（本地规则，不联网）…")
        step_started = time.perf_counter()
        summary_ok = False
        summary_payload: dict[str, Any] = {}
        try:
            today_summary = service.generate_daily_summary()
            summary_payload["today"] = _summary_record(today_summary)
            latest_day = _latest_message_day(messages_after_second)
            if latest_day is not None and latest_day != today_summary.day:
                latest_summary = service.generate_daily_summary(day=latest_day)
                summary_payload["latest_message_day"] = _summary_record(latest_summary)
            summary_ok = bool(
                summary_payload.get("today", {}).get("counts", {}).get("total", 0) > 0
                or summary_payload.get("latest_message_day", {})
                .get("counts", {})
                .get("total", 0)
                > 0
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败如实记录
            summary_payload["error"] = safe_error(exc, max_len=160)
            result["errors"].append(f"摘要生成失败：{safe_error(exc, max_len=160)}")
        timings["summary_ms"] = _elapsed_ms(step_started)
        result["summary"] = summary_payload
        result["checks"]["summary_ok"] = summary_ok
        if summary_ok:
            shown = summary_payload.get("today") or summary_payload.get("latest_message_day") or {}
            emit(
                "摘要已生成："
                f"日期 {shown.get('day')}，收到 {shown.get('counts', {}).get('total', 0)} 条，"
                f"待办 {shown.get('open_todos', 0)} 条。"
            )

        # 7) 负向测试（隔离数据库 + 内存凭据）
        if run_negative:
            emit("附加步骤：负向测试（错误授权码 → 必须明确失败且 0 条入库）…")
            negative = run_negative_check(
                email=normalized_email,
                wrong_auth_code=negative_auth_code,
                mail_endpoint=mail_endpoint,
                count=min(count, 10),
                progress=emit,
            )
            result["negative"] = negative
            result["checks"]["negative_ok"] = bool(negative.get("ok"))
            if not negative.get("ok"):
                result["errors"].append(
                    "负向测试未通过：错误授权码路径可能误报成功或写入了消息"
                )
        else:
            result["negative"] = {"attempted": False, "reason": "用户关闭了负向测试"}
            result["checks"]["negative_ok"] = None

        # 8) 只读性加强证据：mock 自检可观测服务器命令日志（真实运行保留 EXAMINE 只读证据）
        if readonly_probe is not None:
            try:
                observed = readonly_probe() or {}
                write_attempts = list(observed.get("write_attempts") or [])
                non_peek = list(observed.get("non_peek_fetch_uids") or [])
                result["readonly"].update(
                    {
                        "examine_readonly": True,
                        "write_commands_seen": write_attempts,
                        "non_peek_fetch_uids": non_peek,
                        "observed_verbs": list(observed.get("observed_verbs") or []),
                        "verified_by": "EXAMINE 只读结果 + mock IMAP 服务器实际命令日志",
                    }
                )
                result["checks"]["readonly_ok"] = bool(
                    examine_readonly and not write_attempts and not non_peek
                )
            except Exception:  # noqa: BLE001 - 命令日志观测失败时保留 EXAMINE 只读证据
                result["readonly"]["probe_error"] = True
    except Exception as exc:  # noqa: BLE001 - 验收工具必须返回结构化结果而不是崩溃
        if not isinstance(exc, (AcceptanceInputError,)):
            result["errors"].append(safe_error(exc, max_len=200))
        emit(f"验收中止：{type(exc).__name__}")
    finally:
        if service is not None:
            try:
                service.shutdown()
            except Exception:  # noqa: BLE001
                pass
        result["timings_ms"] = timings
        result["finished_at"] = _now_iso()
        result["elapsed_ms"] = _elapsed_ms(started)
        # 汇总检查项 → 总 ok（readonly_ok 允许 None：真实运行无法观测服务器命令）
        required = ("credential_saved", "connection_ok", "sync_ok", "data_present",
                    "incremental_ok", "no_duplicate_ok", "summary_ok")
        checks = result.get("checks", {})
        required_ok = all(checks.get(key) is True for key in required)
        if run_negative:
            required_ok = required_ok and checks.get("negative_ok") is True
        result["ok"] = bool(required_ok and not result["errors"])
        if write_evidence_file:
            try:
                path = write_evidence(result, evidence_dir, prefix=evidence_prefix)
                result["evidence_path"] = str(path)
            except OSError as exc:
                result["evidence_write_error"] = f"无法写入证据文件（{type(exc).__name__}）"
        emit("验收结果：" + ("通过 PASS" if result["ok"] else "未通过 FAIL"))
    return result


def write_evidence(
    result: dict[str, Any],
    evidence_dir: str | Path | None = None,
    *,
    prefix: str = EVIDENCE_PREFIX_REAL,
) -> Path:
    """把脱敏验收结果写入 evidence/mail/<prefix>_<ts>.json。"""
    target_dir = Path(evidence_dir) if evidence_dir is not None else PROJECT_ROOT / "evidence" / "mail"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = target_dir / f"{prefix}_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 结果文本（GUI 展示；只显示计数/分类，不显示正文/主题/完整邮箱）
# ---------------------------------------------------------------------------

def _format_result_lines(result: dict[str, Any]) -> list[str]:
    """把脱敏验收结果格式化为界面文本（纯函数，便于自检与测试）。"""
    lines: list[str] = []
    if result.get("ok"):
        lines.append("✅ 验收通过：真实邮箱授权 / 只读同步 / 增量去重 / 摘要 全流程正常。")
    else:
        lines.append("❌ 验收未通过：请查看下面的分项结果与错误提示。")
    lines.append(f"账号（脱敏）：{result.get('email_masked') or '—'}（域名 {result.get('email_domain') or '—'}）")
    lines.append(f"应用版本：{result.get('app_version')}｜模式：{result.get('mode')}")
    connection = result.get("connection") or {}
    if connection:
        lines.append(
            f"连接：ok={connection.get('ok')}｜延迟 {connection.get('latency_ms')} ms｜"
            f"TLS {(connection.get('tls') or {}).get('version') or '—'}｜只读={connection.get('readonly')}"
        )
    first = result.get("first_sync") or {}
    if first:
        lines.append(
            f"首次同步：状态 {first.get('statuses')}｜读取 {first.get('messages_seen')} 封｜"
            f"新增入库 {first.get('messages_new')} 封｜UID 范围 {first.get('uid_range')}"
        )
    incremental = result.get("incremental_sync") or {}
    if incremental:
        lines.append(
            f"增量同步：状态 {incremental.get('statuses')}｜新增 {incremental.get('messages_new')} 封｜"
            f"入库 {incremental.get('stored_before')}→{incremental.get('stored_after')} 封｜"
            f"重复哈希 {incremental.get('duplicate_content_hashes')} 个"
        )
    summary = result.get("summary") or {}
    shown = summary.get("today") or summary.get("latest_message_day") or {}
    if shown:
        counts = shown.get("counts") or {}
        lines.append(
            f"摘要（{shown.get('day')}）：收到 {counts.get('total', 0)} 条｜"
            f"重要 {counts.get('important', 0)} / 普通 {counts.get('normal', 0)} / "
            f"仅供了解 {counts.get('fyi', 0)}｜待办 {shown.get('open_todos', 0)} 条"
        )
        if summary.get("latest_message_day"):
            latest = summary["latest_message_day"]
            lines.append(f"  · 另按最新邮件日期 {latest.get('day')} 生成摘要补充展示（真实邮件日期可能不在今天）。")
    negative = result.get("negative") or {}
    if negative.get("attempted"):
        lines.append(
            f"负向测试（错误授权码）：明确失败={not negative.get('auth_check_ok')}｜"
            f"入库 {negative.get('messages_stored')} 条｜误报成功={negative.get('misreported_success')}"
        )
    else:
        lines.append("负向测试：未执行（本次关闭）。")
    lines.append("")
    lines.append("分项检查：")
    for key, value in (result.get("checks") or {}).items():
        label = {
            "credential_saved": "授权码写入凭据管理器",
            "connection_ok": "只读连接/认证",
            "sync_ok": "首次只读同步",
            "data_present": "确有邮件入库",
            "incremental_ok": "增量 0 新增",
            "no_duplicate_ok": "无重复入库",
            "summary_ok": "摘要生成",
            "negative_ok": "错误授权码不误报",
            "readonly_ok": "只读性（EXAMINE 只读 / 命令日志）",
        }.get(key, key)
        mark = "✅" if value is True else ("—" if value is None else "❌")
        lines.append(f"  {mark} {label}")
    errors = result.get("errors") or []
    if errors:
        lines.append("")
        lines.append("错误/提示：")
        lines.extend(f"  - {redact_text(str(error), max_len=200)}" for error in errors[:5])
    evidence_path = result.get("evidence_path")
    if evidence_path:
        lines.append("")
        lines.append(f"脱敏证据 JSON 已写入：{evidence_path}")
    if result.get("evidence_write_error"):
        lines.append(f"注意：{result['evidence_write_error']}")
    lines.append("")
    lines.append("说明：界面与证据均不含授权码、邮件正文或完整邮箱地址；本工具不会发送/删除/标记邮件。")
    return lines


# ---------------------------------------------------------------------------
# GUI（Tkinter）
# ---------------------------------------------------------------------------

def create_acceptance_app(
    *,
    default_count: int = DEFAULT_COUNT,
    out_dir: str | Path | None = None,
    evidence_dir: str | Path | None = None,
    root_factory: Callable[[], Any] | None = None,
):
    """创建验收 Tk 窗口，返回 ``(root, app)``；不进入 mainloop。"""
    import queue as _queue
    import tkinter as tk
    from tkinter import messagebox

    class AcceptanceApp:
        def __init__(self, root: Any) -> None:
            self.root = root
            self._out_dir = Path(out_dir) if out_dir is not None else None
            self._evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
            self._queue: _queue.Queue[tuple[str, Any]] = _queue.Queue()
            self._worker: threading.Thread | None = None
            self._closing = False
            self._poll_after_id: str | None = None
            self._last_result: dict[str, Any] | None = None

            root.title("QQ 邮箱完整流程验收（Tkinter，本机运行）")
            root.geometry("940x780")
            root.minsize(820, 640)

            main = tk.Frame(root, padx=14, pady=12)
            main.pack(fill="both", expand=True)
            tk.Label(
                main,
                text="QQ 邮箱完整流程真机验收（授权 → 只读同步 → 增量去重 → 摘要）",
                font=("Microsoft YaHei UI", 15, "bold"),
            ).pack(anchor="w")
            tk.Label(
                main,
                text=(
                    "本工具只操作用户本人邮箱：只读 EXAMINE/BODY.PEEK，不发送/删除/标记邮件；"
                    "授权码仅在本机内存与 Windows 凭据管理器中使用，不打印/不写证据。\n"
                    "仅限本人邮箱，请勿使用朋友或他人的账号。"
                ),
                justify="left",
                anchor="w",
                fg="#8a4b00",
                wraplength=880,
            ).pack(fill="x", pady=(6, 10))

            form = tk.Frame(main)
            form.pack(fill="x")
            form.columnconfigure(1, weight=1)
            tk.Label(form, text="QQ 邮箱地址：").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=3)
            self.email_var = tk.StringVar()
            self.email_entry = tk.Entry(form, textvariable=self.email_var, width=40)
            self.email_entry.grid(row=0, column=1, sticky="we", pady=3)
            tk.Label(form, text="邮箱授权码：").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=3)
            self.code_var = tk.StringVar()
            self.code_entry = tk.Entry(form, textvariable=self.code_var, width=40, show="*")
            self.code_entry.grid(row=1, column=1, sticky="we", pady=3)
            tk.Label(form, text="同步条数 N：").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=3)
            self.count_var = tk.StringVar(value=str(max(1, min(default_count, MAX_COUNT))))
            self.count_spin = tk.Spinbox(form, from_=1, to=MAX_COUNT, textvariable=self.count_var, width=8)
            self.count_spin.grid(row=2, column=1, sticky="w", pady=3)

            self.consent_var = tk.BooleanVar(value=False)
            self.consent_check = tk.Checkbutton(
                main,
                text="我确认：这是本人的 QQ 邮箱，并同意本程序以只读方式采集该邮箱（不读取他人邮箱）",
                variable=self.consent_var,
                anchor="w",
                justify="left",
                wraplength=880,
            )
            self.consent_check.pack(fill="x", pady=(8, 2))
            self.negative_var = tk.BooleanVar(value=True)
            self.negative_check = tk.Checkbutton(
                main,
                text=(
                    "同时执行负向测试：用错误授权码验证“不会误报成功”（隔离临时数据库 + 内存凭据；"
                    "会向服务器发起一次注定失败的登录，不读取任何邮件）"
                ),
                variable=self.negative_var,
                anchor="w",
                justify="left",
                wraplength=880,
            )
            self.negative_check.pack(fill="x", pady=(0, 8))

            buttons = tk.Frame(main)
            buttons.pack(fill="x", pady=(0, 8))
            self.start_button = tk.Button(buttons, text=f"开始完整验收（同步最近 N 封，默认 {DEFAULT_COUNT}）", command=self._on_start)
            self.start_button.pack(side="left")
            self.screenshot_button = tk.Button(buttons, text="保存结果截图", command=self._on_screenshot, state="disabled")
            self.screenshot_button.pack(side="left", padx=(8, 0))
            self.close_button = tk.Button(buttons, text="关闭", command=self._on_close)
            self.close_button.pack(side="left", padx=(8, 0))

            self.status_label = tk.Label(main, text="等待输入…", anchor="w")
            self.status_label.pack(fill="x", pady=(0, 4))

            tk.Label(main, text="进度：", anchor="w").pack(fill="x")
            progress_frame = tk.Frame(main)
            progress_frame.pack(fill="x", pady=(2, 8))
            progress_scroll = tk.Scrollbar(progress_frame)
            progress_scroll.pack(side="right", fill="y")
            self.progress_text = tk.Text(progress_frame, height=7, wrap="word", state="disabled", yscrollcommand=progress_scroll.set)
            self.progress_text.pack(side="left", fill="both", expand=True)
            progress_scroll.config(command=self.progress_text.yview)

            tk.Label(main, text="结果（已脱敏，不显示邮件正文/主题）：", anchor="w").pack(fill="x")
            results_frame = tk.Frame(main)
            results_frame.pack(fill="both", expand=True, pady=(2, 0))
            results_scroll = tk.Scrollbar(results_frame)
            results_scroll.pack(side="right", fill="y")
            self.results_text = tk.Text(results_frame, height=20, wrap="word", state="disabled", yscrollcommand=results_scroll.set)
            self.results_text.pack(side="left", fill="both", expand=True)
            results_scroll.config(command=self.results_text.yview)

            root.protocol("WM_DELETE_WINDOW", self._on_close)
            self._poll_after_id = root.after(150, self._poll_queue)

        # -- 事件 ------------------------------------------------------
        def _on_start(self) -> None:
            email = self.email_var.get().strip()
            code = self.code_var.get().strip()
            try:
                count = int(self.count_var.get().strip())
            except ValueError:
                messagebox.showwarning("提示", "同步条数 N 必须是 1~200 的整数。", parent=self.root)
                return
            try:
                validate_acceptance_input(email, code, count)
            except AcceptanceInputError as exc:
                messagebox.showwarning("提示", str(exc), parent=self.root)
                return
            if not self.consent_var.get():
                messagebox.showwarning(
                    "需要确认",
                    "请先勾选“这是本人的 QQ 邮箱，并同意只读采集”后再开始验收。",
                    parent=self.root,
                )
                return

            self._set_busy(True)
            self._clear_text(self.progress_text)
            self._clear_text(self.results_text)
            self._last_result = None
            self.status_label.config(text="验收进行中…请勿关闭窗口", fg="#333333")
            self._append_progress("开始完整验收（不会发送/删除/标记任何邮件）。")
            # 授权码先交给 worker 参数，随后立即清空输入框与变量。
            self.code_var.set("")
            try:
                self.code_entry.delete(0, tk.END)
            except tk.TclError:  # pragma: no cover
                pass
            self._worker = threading.Thread(
                target=self._run_worker,
                args=(email, code, count, self.negative_var.get()),
                name="mail-acceptance-worker",
                daemon=True,
            )
            self._worker.start()

        def _on_screenshot(self) -> None:
            if self._last_result is None:
                messagebox.showinfo("提示", "还没有验收结果，请先运行验收。", parent=self.root)
                return
            try:
                from PIL import ImageGrab
            except Exception:  # noqa: BLE001
                messagebox.showwarning("无法截图", "缺少 Pillow/ImageGrab，无法保存截图。", parent=self.root)
                return
            self.root.update_idletasks()
            try:
                x = self.results_text.winfo_rootx()
                y = self.results_text.winfo_rooty()
                width = self.results_text.winfo_width()
                height = self.results_text.winfo_height()
                image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
                target_dir = self._evidence_dir or (PROJECT_ROOT / "evidence" / "mail")
                target_dir.mkdir(parents=True, exist_ok=True)
                path = target_dir / f"mail_acceptance_result_{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}.png"
                image.save(path)
                messagebox.showinfo("截图已保存", f"结果区截图已保存：\n{path}", parent=self.root)
            except Exception as exc:  # noqa: BLE001
                messagebox.showwarning("截图失败", f"保存截图失败（{type(exc).__name__}）。", parent=self.root)

        def _on_close(self) -> None:
            if self._worker is not None and self._worker.is_alive():
                messagebox.showinfo("请稍候", "验收仍在进行中，请等待完成后再关闭。", parent=self.root)
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
        def _run_worker(self, email: str, code: str, count: int, run_negative: bool) -> None:
            try:
                result = run_mail_acceptance(
                    email=email,
                    auth_code=code,
                    count=count,
                    data_dir=self._out_dir,
                    run_negative=run_negative,
                    evidence_dir=self._evidence_dir,
                    mode=MODE_REAL,
                    progress=lambda message: self._queue.put(("progress", message)),
                )
            except AcceptanceInputError as exc:
                result = {
                    "ok": False,
                    "mode": MODE_REAL,
                    "errors": [str(exc)],
                    "checks": {},
                    "email_masked": mask_email(email),
                    "email_domain": _domain_of(email),
                }
            except Exception as exc:  # noqa: BLE001 - 兜底：不把异常原文暴露到 UI
                result = {
                    "ok": False,
                    "mode": MODE_REAL,
                    "errors": [f"验收工具内部错误（{type(exc).__name__}），请重试"],
                    "checks": {},
                    "email_masked": mask_email(email),
                    "email_domain": _domain_of(email),
                }
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
                        self._append_progress(str(payload))
                    else:
                        self._on_done(payload)
            except _queue.Empty:
                pass
            if not self._closing:
                self._poll_after_id = self.root.after(150, self._poll_queue)

        def _on_done(self, result: dict[str, Any]) -> None:
            self._worker = None
            self._set_busy(False)
            self._last_result = result
            self._render_result(result)
            self.screenshot_button.config(state="normal")
            if result.get("ok"):
                self.status_label.config(text="✅ 验收通过", fg="#1a7f37")
                messagebox.showinfo(
                    "验收通过",
                    "授权 / 只读同步 / 增量去重 / 摘要 全流程通过。\n\n"
                    "脱敏证据 JSON 已写入 evidence/mail/。",
                    parent=self.root,
                )
            else:
                self.status_label.config(text="❌ 验收未通过", fg="#b42318")
                errors = result.get("errors") or ["请查看结果区"]
                messagebox.showerror(
                    "验收未通过",
                    "本次验收未通过，请查看结果区的分项检查与错误提示。\n\n"
                    + redact_text(str(errors[0]), max_len=200),
                    parent=self.root,
                )

        # -- 界面状态 --------------------------------------------------
        def _set_busy(self, busy: bool) -> None:
            state = "disabled" if busy else "normal"
            for widget in (
                self.email_entry,
                self.code_entry,
                self.count_spin,
                self.consent_check,
                self.negative_check,
                self.start_button,
                self.close_button,
            ):
                try:
                    widget.config(state=state)
                except Exception:  # noqa: BLE001
                    pass

        def _clear_text(self, widget: Any) -> None:
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.config(state="disabled")

        def _append_progress(self, message: str) -> None:
            self.progress_text.config(state="normal")
            self.progress_text.insert("end", message + "\n")
            self.progress_text.see("end")
            self.progress_text.config(state="disabled")

        def _render_result(self, result: dict[str, Any]) -> list[str]:
            lines = _format_result_lines(result)
            self._clear_text(self.results_text)
            self.results_text.config(state="normal")
            self.results_text.insert("1.0", "\n".join(lines))
            self.results_text.config(state="disabled")
            return lines

    root = root_factory() if root_factory is not None else tk.Tk()
    app = AcceptanceApp(root)
    return root, app


def run_self_test() -> int:
    """无网络 GUI 自检：真实创建 Tkinter 窗口，验证授权码掩码/脱敏展示/失败提示。"""
    try:
        root, app = create_acceptance_app()
    except ImportError:
        print("未找到 Tkinter（Python 安装未包含 Tcl/Tk），无法运行 GUI 自检。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"无法创建 Tkinter 窗口（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2
    try:
        assert str(app.code_entry.cget("show")) == "*", "授权码输入框必须为密码模式"
        assert str(app.email_entry.cget("show")) == ""
        assert app.count_var.get() == str(DEFAULT_COUNT)
        assert app.code_var.get() == ""

        synthetic = {
            "ok": True,
            "mode": MODE_REAL,
            "app_version": APP_VERSION,
            "email_masked": "1***@qq.com",
            "email_domain": "qq.com",
            "connection": {
                "ok": True,
                "latency_ms": 180,
                "tls": {"version": "TLSv1.2"},
                "readonly": True,
            },
            "first_sync": {
                "statuses": ["ok"],
                "messages_seen": 20,
                "messages_new": 3,
                "uid_range": {"min": 101, "max": 120, "count": 20},
            },
            "incremental_sync": {
                "statuses": ["ok"],
                "messages_new": 0,
                "stored_before": 3,
                "stored_after": 3,
                "duplicate_content_hashes": 0,
            },
            "summary": {
                "today": {
                    "day": "2026-11-01",
                    "counts": {"total": 3, "important": 1, "normal": 2, "fyi": 0},
                    "open_todos": 1,
                }
            },
            "negative": {
                "attempted": True,
                "auth_check_ok": False,
                "messages_stored": 0,
                "misreported_success": False,
            },
            "checks": {
                "credential_saved": True,
                "connection_ok": True,
                "sync_ok": True,
                "data_present": True,
                "incremental_ok": True,
                "no_duplicate_ok": True,
                "summary_ok": True,
                "negative_ok": True,
                "readonly_ok": None,
            },
            "errors": [],
        }
        rendered = "\n".join(app._render_result(synthetic))
        assert "验收通过" in rendered
        assert "1***@qq.com" in rendered
        assert "123456789@qq.com" not in rendered
        assert "TLSv1.2" in rendered
        assert "增量同步" in rendered
        assert "负向测试" in rendered

        failure = {
            "ok": False,
            "mode": MODE_REAL,
            "app_version": APP_VERSION,
            "email_masked": "1***@qq.com",
            "email_domain": "qq.com",
            "checks": {"credential_saved": True, "connection_ok": False},
            "errors": ["邮箱连接/认证失败（MAIL_AUTH_FAILED）"],
        }
        failure_text = "\n".join(app._render_result(failure))
        assert "验收未通过" in failure_text
        assert "MAIL_AUTH_FAILED" in failure_text

        app.code_var.set("AUTHCODE-SECRET-SENTINEL")
        root.update_idletasks()
        root.update()
        assert "AUTHCODE-SECRET-SENTINEL" not in app.progress_text.get("1.0", "end-1c")
        app.code_var.set("")
        print(
            "SELF-TEST OK: Tkinter 窗口已实际创建；授权码密码框 / 脱敏结果展示 / "
            "失败提示 / 授权码不落界面 均通过（未联网、未使用真实授权码）。"
        )
        return 0
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# mock IMAP 全流程自检（无真实凭据、无真实邮箱）
# ---------------------------------------------------------------------------

def _load_mock_module() -> Any:
    import importlib.util

    path = PROJECT_ROOT / "tools" / "mock_imap_server.py"
    module_name = "ss_mock_imap_server_acceptance"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 tools/mock_imap_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _sample_email(*, index: int, date_value: datetime) -> bytes:
    from email.message import EmailMessage
    from email.utils import format_datetime

    message = EmailMessage()
    message["Subject"] = f"合成验收邮件 {index}"
    message["From"] = f"合成发件人 <sender{index}@example.com>"
    message["To"] = "test@qq.com"
    message["Message-ID"] = f"<acceptance-{index}@example.com>"
    message["Date"] = format_datetime(date_value)
    message.set_content(
        f"这是第 {index} 封合成邮件正文，仅用于验收工具自检。"
        "任务：请在明天 18:00 前确认安排。"
    )
    return message.as_bytes()


def run_mock_acceptance(
    *,
    data_dir: str | Path | None = None,
    evidence_dir: str | Path | None = None,
    count: int = 3,
) -> dict[str, Any]:
    """用本地 mock IMAP 服务器跑完整验收流程（无真实凭据、无真实邮箱）。"""
    mock = _load_mock_module()
    now = datetime.now().astimezone()
    mailbox = mock.MockMailbox(uidvalidity=100)
    for index in range(1, 7):
        mailbox.messages.append(
            mock.MockMessage(uid=index, raw=_sample_email(index=index, date_value=now))
        )
    server = mock.MockImapServer(mailbox=mailbox, username="test@qq.com", password="mock-auth-code").start()
    temp_data = Path(data_dir) if data_dir is not None else _temp_workspace_dir("mail-acceptance-mock-data-")
    try:
        return run_mail_acceptance(
            email="test@qq.com",
            auth_code="mock-auth-code",
            count=count,
            data_dir=temp_data,
            credential_store=InMemoryCredentialStore(),
            mail_endpoint={
                "host": "127.0.0.1",
                "port": server.port,
                "insecure_plaintext": True,
                "timeout": 3.0,
            },
            run_negative=True,
            negative_auth_code="wrong-code",
            evidence_dir=evidence_dir,
            evidence_prefix=EVIDENCE_PREFIX_MOCK,
            mode=MODE_MOCK,
            readonly_probe=lambda: {
                "write_attempts": [command.verb for command in server.write_attempts],
                "non_peek_fetch_uids": list(server.non_peek_fetch_uids),
                "observed_verbs": sorted(set(server.verbs)),
            },
        )
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass
        if data_dir is None:
            shutil.rmtree(temp_data, ignore_errors=True)


def run_mock_self_test(*, evidence_dir: str | Path | None = None) -> int:
    """CLI：mock 全流程自检，结果写证据并打印摘要。"""
    result = run_mock_acceptance(evidence_dir=evidence_dir)
    summary = {
        "ok": result.get("ok"),
        "mode": result.get("mode"),
        "checks": result.get("checks"),
        "first_sync": result.get("first_sync"),
        "incremental_sync": result.get("incremental_sync"),
        "negative_ok": (result.get("negative") or {}).get("ok"),
        "evidence_path": result.get("evidence_path"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QQ 邮箱完整流程真机验收工具（Tkinter；只读、仅本人邮箱）"
    )
    parser.add_argument("--self-test", action="store_true", help="无网络 GUI 自检（真实创建 Tkinter 窗口）后退出")
    parser.add_argument(
        "--mock-self-test",
        action="store_true",
        help="mock IMAP 全流程自检（无真实凭据/真实邮箱），写证据后退出",
    )
    parser.add_argument("--data-dir", default=None, help="产品数据目录（默认使用产品默认目录；测试可覆盖）")
    parser.add_argument("--evidence-dir", default=None, help="证据输出目录（默认 evidence/mail）")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"同步条数 N（默认 {DEFAULT_COUNT}）")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if args.mock_self_test:
        return run_mock_self_test(evidence_dir=args.evidence_dir)

    try:
        root, _app = create_acceptance_app(
            default_count=args.count,
            out_dir=args.data_dir,
            evidence_dir=args.evidence_dir,
        )
    except ImportError:
        print(
            "未找到 Tkinter（Python 安装未包含 Tcl/Tk），无法启动图形界面；"
            "可运行 --mock-self-test 做无界面自检。",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"无法创建 Tkinter 窗口（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
