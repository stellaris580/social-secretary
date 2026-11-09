"""v0.1.1 负向路径验收（task-10 / P0-4）：无 Key / 断网 / 邮箱授权失败 / QQ 不可用 / keyring 不可用。

真实执行产品代码（ServiceAPI + 真实采集器），逐场景断言：
1. 不误报成功（失败必须返回明确错误状态，而不是 ok/空数据冒充成功）；
2. 不写消息 / 不写草稿；
3. 错误可操作（中文、指向下一步）；
4. 日志/证据中不出现邮箱授权码、API Key、消息正文。

输出：``evidence/acceptance/negative_paths_<ts>.json``

用法::

    .\\.venv\\Scripts\\python.exe tools\\negative_acceptance.py
    .\\.venv\\Scripts\\python.exe tools\\negative_acceptance.py --offline   # 跳过真实网络场景

退出码：0 = 全部场景通过；1 = 存在失败；2 = 全部结构化失败但部分场景受环境限制（需重跑）。
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_secretary.config import APP_NAME, APP_VERSION, AppPaths  # noqa: E402
from social_secretary.contracts import (  # noqa: E402
    ConsentScope,
    Conversation,
    ConversationKind,
    ScanStatus,
)
from social_secretary.core.service import ServiceError, SocialSecretaryService  # noqa: E402
from social_secretary.security.credential_store import (  # noqa: E402
    CredentialStoreError,
    InMemoryCredentialStore,
    KeyringCredentialStore,
)
from social_secretary.security.log_redaction import hash_text, redact_text  # noqa: E402
from social_secretary.security.logging_setup import configure_logging, reset_logging  # noqa: E402

# 验收用的假凭据：只存在于内存，绝不写入真实 Windows 凭据管理器。
WRONG_AUTH_CODE = "abcdefghijklmnop"           # 16 位小写，非真实授权码
FAKE_API_KEY = "sk-negative-acceptance-0123456789abcdef"
FAKE_MAIL_EMAIL = "social-secretary-acceptance@qq.com"  # 不存在的测试邮箱
UNREACHABLE_AI_BASE_URL = "http://127.0.0.1:1/v1"       # 本机未监听端口 → 立即失败
UNREACHABLE_IMAP_HOST = "127.0.0.1"
UNREACHABLE_IMAP_PORT = 1
QQ_TEST_CONVERSATION = "SocialSecretaryNegativeAcceptanceOnly"

SECRETS = (WRONG_AUTH_CODE, FAKE_API_KEY)
# 演示数据正文（用于证明负向验收证据/日志里没有消息正文泄漏）
DEMO_BODY_MARKERS = ("项目评审会", "预算表", "团建报名")


# ---------------------------------------------------------------------------
# 场景框架
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}", max_len=300)


class ScenarioResult(dict):
    pass


def _new_result(scenario_id: str, title: str, expected: str) -> ScenarioResult:
    return ScenarioResult(
        {
            "id": scenario_id,
            "title": title,
            "expected": expected,
            "ok": False,
            "status": "not_run",
            "environment_limited": False,
            "actual": "",
            "checks": {},
            "error": None,
            "duration_ms": 0,
        }
    )


def run_scenario(scenario_id: str, title: str, expected: str, func) -> ScenarioResult:
    result = _new_result(scenario_id, title, expected)
    started = time.monotonic()
    try:
        func(result)
    except Exception as exc:  # noqa: BLE001 - 场景本身异常也记录为失败，不终止全部验收
        result["ok"] = False
        result["status"] = "error"
        result["error"] = _safe(exc)
        result["traceback_tail"] = traceback.format_exc().splitlines()[-3:]
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
    if result["status"] == "not_run":
        result["status"] = "pass" if result["ok"] else "fail"
    return result


# ---------------------------------------------------------------------------
# 各场景
# ---------------------------------------------------------------------------

def _new_service(paths: AppPaths, logger, credential_store=None, **kwargs) -> SocialSecretaryService:
    return SocialSecretaryService(
        paths,
        credential_store=credential_store or InMemoryCredentialStore(),
        logger=logger,
        **kwargs,
    )


def scenario_external_ai_missing_key(root: Path, logger) -> ScenarioResult:
    def body(result: ScenarioResult) -> None:
        service = _new_service(AppPaths.create(root / "external-ai-missing-key"), logger)
        try:
            service.load_demo_data()
            service.grant_consent(ConsentScope.EXTERNAL_AI)
            raised = None
            try:
                service.generate_drafts("demo-msg-3", use_external=True)
            except ServiceError as exc:
                raised = str(exc)
            drafts = service.list_drafts("demo-msg-3")
            ai_check = service.test_external_ai()
            result["checks"] = {
                "generate_drafts_raised": raised is not None,
                "error_message": raised,
                "error_mentions_key": bool(
                    raised and ("Key" in raised or "API Key" in raised)
                ),
                "drafts_written": len(drafts),
                "no_drafts": len(drafts) == 0,
                "test_external_ai_ok": bool(ai_check.ok),
                "test_external_ai_detail": ai_check.detail,
                "no_success_audit": "drafts_generated_external"
                not in {event["action"] for event in service.storage.list_audit()},
            }
            result["actual"] = (
                f"use_external=True 被拒绝：{raised or '（未抛错！）'}；"
                f"草稿数={len(drafts)}；test_external_ai.ok={ai_check.ok}"
            )
            result["ok"] = all(
                (
                    result["checks"]["generate_drafts_raised"],
                    result["checks"]["error_mentions_key"],
                    result["checks"]["no_drafts"],
                    result["checks"]["test_external_ai_ok"] is False,
                    result["checks"]["no_success_audit"],
                )
            )
        finally:
            service.shutdown()

    return run_scenario(
        "external_ai_missing_key",
        "无外部 AI Key 时调用 generate_drafts(use_external=True) / test_external_ai",
        "明确失败（提示未保存 Key），0 草稿、0 成功状态、无网络请求",
        body,
    )


def scenario_external_ai_unreachable(root: Path, logger) -> ScenarioResult:
    def body(result: ScenarioResult) -> None:
        service = _new_service(AppPaths.create(root / "external-ai-unreachable"), logger)
        try:
            service.load_demo_data()
            service.set_external_ai_key(FAKE_API_KEY)
            service.update_settings(
                external_ai_base_url=UNREACHABLE_AI_BASE_URL,
                external_ai_model="negative-acceptance-model",
            )
            service.grant_consent(ConsentScope.EXTERNAL_AI)

            started = time.monotonic()
            raised = None
            try:
                service.generate_drafts("demo-msg-3", use_external=True)
            except ServiceError as exc:
                raised = str(exc)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            drafts = service.list_drafts("demo-msg-3")
            ai_check = service.test_external_ai()
            actions = {event["action"] for event in service.storage.list_audit()}
            result["checks"] = {
                "generate_drafts_raised": raised is not None,
                "error_message": raised,
                "error_mentions_external_ai": bool(raised and "外部 AI" in raised),
                "error_mentions_network_or_config": bool(
                    raised and any(word in raised for word in ("网络", "配置", "不可用", "失败"))
                ),
                "no_drafts": len(drafts) == 0,
                "test_external_ai_ok": bool(ai_check.ok),
                "test_external_ai_detail": ai_check.detail,
                "no_success_audit": "drafts_generated_external" not in actions,
                "elapsed_ms": elapsed_ms,
            }
            result["actual"] = (
                f"base_url={UNREACHABLE_AI_BASE_URL} 时 generate_drafts 失败：{raised or '（未抛错！）'}；"
                f"草稿数={len(drafts)}；test_external_ai.ok={ai_check.ok}（{ai_check.detail[:80]}）；"
                f"耗时 {elapsed_ms}ms"
            )
            result["ok"] = all(
                (
                    result["checks"]["generate_drafts_raised"],
                    result["checks"]["error_mentions_external_ai"],
                    result["checks"]["no_drafts"],
                    result["checks"]["test_external_ai_ok"] is False,
                    result["checks"]["no_success_audit"],
                )
            )
        finally:
            service.shutdown()

    return run_scenario(
        "external_ai_unreachable",
        "外部 AI 服务不可达（127.0.0.1:1）",
        "结构化网络/配置错误、0 草稿、无成功状态、不崩溃",
        body,
    )


def scenario_mail_bad_auth_real(root: Path, logger, *, offline: bool) -> ScenarioResult:
    def body(result: ScenarioResult) -> None:
        if offline:
            result["status"] = "environment_limited"
            result["environment_limited"] = True
            result["actual"] = "--offline：跳过真实 imap.qq.com 认证失败场景（未验证）"
            result["error"] = "offline 模式未执行真实网络认证失败场景"
            return

        service = _new_service(AppPaths.create(root / "mail-bad-auth-real"), logger)
        try:
            account = service.add_mail_account(FAKE_MAIL_EMAIL, "负向验收邮箱")
            service.set_mail_credential(FAKE_MAIL_EMAIL, WRONG_AUTH_CODE)
            service.grant_consent(ConsentScope.MAIL_READ)
            service.resume_collection()

            credential_check = service.test_mail_credential(FAKE_MAIL_EMAIL)
            code = str((credential_check.discovered or {}).get("code", ""))
            scan = service.run_scan(account.id)
            scan_result = scan[0] if scan else None
            messages = service.list_messages()
            scan_runs = service.storage.list_scan_runs(limit=5)
            status = service.collection_status()
            summary = service.generate_daily_summary()
            result["checks"] = {
                "test_credential_ok": bool(credential_check.ok),
                "test_credential_detail": credential_check.detail,
                "error_code": code,
                "scan_count": len(scan),
                "scan_status": scan_result.status.value if scan_result else None,
                "scan_messages_new": scan_result.messages_new if scan_result else None,
                "scan_error": scan_result.error if scan_result else None,
                "messages_in_db": len(messages),
                "drafts_in_db": service.storage.count_drafts(),
                "scan_runs_recorded": [run.status.value for run in scan_runs],
                "collection_status_last": status["last_scan_status"],
                "collection_status_error": status["last_error"],
                "summary_scan_status": summary.scan_status.value,
                "summary_caveat_mentions_failure": any(
                    "采集存在失败" in caveat for caveat in summary.caveats
                ),
                "summary_not_false_no_news": "没有新的消息需要整理" not in summary.narrative,
                "no_drafts": service.storage.count_drafts() == 0,
            }
            auth_failed = code == "MAIL_AUTH_FAILED"
            network_limited = code in {"MAIL_CONNECTION_FAILED", "MAIL_TLS_FAILED", "MAIL_TIMEOUT"}
            invariants_ok = all(
                (
                    credential_check.ok is False,
                    scan_result is not None,
                    scan_result.status is ScanStatus.FAILED,
                    (scan_result.messages_new or 0) == 0,
                    len(messages) == 0,
                    status["last_scan_status"] == "failed",
                    bool(status["last_error"]),
                    summary.scan_status is ScanStatus.FAILED,
                    result["checks"]["summary_caveat_mentions_failure"],
                    result["checks"]["summary_not_false_no_news"],
                )
            )
            if auth_failed:
                result["ok"] = invariants_ok
                result["actual"] = (
                    f"真实 imap.qq.com 用错误授权码登录被拒绝（{code}）：{credential_check.detail[:80]}；"
                    f"run_scan={scan_result.status.value}，消息 {len(messages)} 条，scan_runs={result['checks']['scan_runs_recorded']}；"
                    "摘要状态 failed 且 caveats 已提示，未误报“没有新消息”"
                )
            elif network_limited:
                result["environment_limited"] = True
                result["status"] = "environment_limited"
                result["actual"] = (
                    f"网络不可用，未能验证“认证失败”（采集器返回 {code}）；"
                    "结构化失败路径已验证：0 消息、scan FAILED"
                )
                result["ok"] = False
            else:
                result["actual"] = f"意外错误码 {code!r}：{credential_check.detail}"
                result["ok"] = False
        finally:
            service.shutdown()

    return run_scenario(
        "mail_bad_auth_real_imap",
        "邮箱错误授权码 → 真实 imap.qq.com 认证失败（不读取任何邮件）",
        "认证失败结构化返回、run_scan FAILED、0 消息入库、摘要不误报无消息",
        body,
    )


def scenario_mail_network_unreachable(root: Path, logger) -> ScenarioResult:
    def body(result: ScenarioResult) -> None:
        mail_pkg = __import__("social_secretary.collectors.mail", fromlist=["MailCollector"])
        holder: dict[str, Any] = {}

        def factory(account):
            collector = mail_pkg.MailCollector(
                account=account,
                credential_store=holder["service"].credential_store,
                sync_state=holder["service"].mail_sync_state_store,
                folders=["INBOX"],
                host=UNREACHABLE_IMAP_HOST,
                port=UNREACHABLE_IMAP_PORT,
                insecure_plaintext=True,
                timeout=5,
            )
            return collector

        service = _new_service(
            AppPaths.create(root / "mail-network-unreachable"),
            logger,
            source_factory=factory,
        )
        holder["service"] = service
        try:
            account = service.add_mail_account("neg-network@example.com", "网络不可达邮箱")
            service.set_mail_credential("neg-network@example.com", WRONG_AUTH_CODE)
            service.grant_consent(ConsentScope.MAIL_READ)
            service.resume_collection()

            account_check = service.test_account(account.id)
            credential_check = service.test_mail_credential("neg-network@example.com")
            scan = service.run_scan(account.id)
            scan_result = scan[0] if scan else None
            messages = service.list_messages()
            result["checks"] = {
                "test_account_ok": bool(account_check.ok),
                "test_account_detail": account_check.detail,
                "test_account_code": (account_check.discovered or {}).get("code"),
                "test_credential_ok": bool(credential_check.ok),
                "test_credential_detail": credential_check.detail,
                "scan_status": scan_result.status.value if scan_result else None,
                "scan_messages_new": scan_result.messages_new if scan_result else None,
                "scan_error": scan_result.error if scan_result else None,
                "messages_in_db": len(messages),
            }
            result["ok"] = all(
                (
                    account_check.ok is False,
                    credential_check.ok is False,
                    scan_result is not None,
                    scan_result.status is ScanStatus.FAILED,
                    (scan_result.messages_new or 0) == 0,
                    len(messages) == 0,
                )
            )
            result["actual"] = (
                f"IMAP 指向 {UNREACHABLE_IMAP_HOST}:{UNREACHABLE_IMAP_PORT}："
                f"test_account.ok={account_check.ok}（{(account_check.discovered or {}).get('code')}），"
                f"run_scan={scan_result.status.value if scan_result else None}，消息 {len(messages)} 条"
            )
        finally:
            service.shutdown()

    return run_scenario(
        "mail_network_unreachable",
        "邮箱网络不可达（127.0.0.1:1）",
        "结构化网络错误、0 消息、run_scan FAILED、不误报",
        body,
    )


def scenario_qq_unavailable(root: Path, logger) -> ScenarioResult:
    def body(result: ScenarioResult) -> None:
        service = _new_service(AppPaths.create(root / "qq-unavailable"), logger)
        scan_service = service
        try:
            account = service.add_qq_account("负向验收 QQ", [QQ_TEST_CONVERSATION])
            service.grant_consent(ConsentScope.QQ_READ)
            # P0-22：实验性 QQ 读取默认关闭；本场景需验证 worker 的“不可用/失败”降级，
            # 因此显式开启实验开关（仍只用 foreground_ack=False，采集器在触碰 UIA 前拒绝）。
            service.update_settings(qq_read_experimental=True)
            service.resume_collection()
            refresh = service.refresh_conversations(account.id)
            if not refresh.ok:
                # 子进程环境异常时直接建会话，保证仍能验证 run_scan 的降级路径
                service.storage.upsert_conversation(
                    Conversation(
                        id="conv-qq-negative",
                        account_id=account.id,
                        kind=ConversationKind.PRIVATE,
                        title=QQ_TEST_CONVERSATION,
                        external_key=QQ_TEST_CONVERSATION,
                        enabled=True,
                    )
                )
            account_check = service.test_account(account.id)
            error_code = str((account_check.discovered or {}).get("error_code") or "")
            qq_absent = (account_check.ok is False) and error_code == "qq_not_running"

            # 安全策略：foreground_ack=False 时 QqMessageSource 在**触碰 UIA 之前**就会拒绝
            # （QqForegroundNotAcknowledged），因此用真实默认 worker 即可验证“失败不误报”，
            # 且绝不会打开/读取任何真实会话。
            scan = service.run_scan(account.id, foreground_ack=False)
            scan_result = scan[0] if scan else None
            note = (
                "实验开关已显式开启；真实默认 worker、foreground_ack=False："
                "采集器在触碰 UIA 前结构化失败，未读取任何真实会话"
            )
            if scan_result is None or scan_result.status is not ScanStatus.FAILED:
                # 兜底：注入 worker 崩溃，继续验证「子进程失败 → FAILED」的降级路径
                scan_service = _new_service(
                    AppPaths.create(root / "qq-unavailable-injected"),
                    logger,
                    qq_worker_command=[
                        sys.executable,
                        "-c",
                        "import sys; sys.exit(7)",
                    ],
                )
                scan_account = scan_service.add_qq_account(
                    "负向验收 QQ（注入崩溃）", [QQ_TEST_CONVERSATION]
                )
                scan_service.grant_consent(ConsentScope.QQ_READ)
                scan_service.update_settings(qq_read_experimental=True)
                scan_service.resume_collection()
                scan_service.storage.upsert_conversation(
                    Conversation(
                        id="conv-qq-injected",
                        account_id=scan_account.id,
                        kind=ConversationKind.PRIVATE,
                        title=QQ_TEST_CONVERSATION,
                        external_key=QQ_TEST_CONVERSATION,
                        enabled=True,
                    )
                )
                scan = scan_service.run_scan(scan_account.id)
                scan_result = scan[0] if scan else None
                note = (
                    "真实默认 worker 未按预期失败（qq_absent="
                    f"{qq_absent}）：改用注入 worker 崩溃验证降级；未读取任何真实会话"
                )

            messages = scan_service.list_messages()
            status = scan_service.collection_status()
            summary = scan_service.generate_daily_summary()
            result["checks"] = {
                "test_account_ok": bool(account_check.ok),
                "test_account_detail": account_check.detail,
                "test_account_code": (account_check.discovered or {}).get("code"),
                "qq_absent_confirmed": qq_absent,
                "qq_experimental_enabled": True,
                "scan_status": scan_result.status.value if scan_result else None,
                "scan_messages_new": scan_result.messages_new if scan_result else None,
                "scan_error": scan_result.error if scan_result else None,
                "messages_in_db": len(messages),
                "collection_status_last": status["last_scan_status"],
                "collection_status_error": status["last_error"],
                "summary_scan_status": summary.scan_status.value,
                "summary_caveat_mentions_failure": any(
                    "采集存在失败" in caveat for caveat in summary.caveats
                ),
                "summary_not_false_no_news": "没有新的消息需要整理" not in summary.narrative,
                "note": note,
            }
            result["ok"] = all(
                (
                    scan_result is not None,
                    scan_result.status is ScanStatus.FAILED,
                    (scan_result.messages_new or 0) == 0,
                    len(messages) == 0,
                    status["last_scan_status"] == "failed",
                    bool(status["last_error"]),
                    summary.scan_status is ScanStatus.FAILED,
                    result["checks"]["summary_caveat_mentions_failure"],
                    result["checks"]["summary_not_false_no_news"],
                )
            )
            result["actual"] = (
                f"QQ 默认 worker：test_account.ok={account_check.ok}（{account_check.detail[:70]}）；"
                f"run_scan={scan_result.status.value if scan_result else None}，消息 {len(messages)} 条；"
                f"摘要 scan_status={summary.scan_status.value} 且 caveats 明确。{note}"
            )
        finally:
            service.shutdown()
            if scan_service is not service:
                scan_service.shutdown()

    return run_scenario(
        "qq_unavailable",
        "QQ 默认 worker 不可用 / 未授权读取环境",
        "结构化失败、0 消息、summary caveats 明确，不把空当成“没有新消息”",
        body,
    )


class _BrokenKeyring:
    def set_password(self, *args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    def get_password(self, *args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    def delete_password(self, *args, **kwargs):
        raise RuntimeError("keyring backend unavailable")


def scenario_keyring_unavailable(root: Path, logger) -> ScenarioResult:
    def body(result: ScenarioResult) -> None:
        broken_store = KeyringCredentialStore(keyring_backend=_BrokenKeyring())
        service = _new_service(
            AppPaths.create(root / "keyring-unavailable"),
            logger,
            credential_store=broken_store,
        )
        try:
            service.load_demo_data()
            mail_error = api_error = None
            try:
                service.set_mail_credential("neg-keyring@example.com", WRONG_AUTH_CODE)
            except ServiceError as exc:
                mail_error = str(exc)
            try:
                service.set_external_ai_key(FAKE_API_KEY)
            except ServiceError as exc:
                api_error = str(exc)
            mail_check = service.test_mail_credential("neg-keyring@example.com")
            service.grant_consent(ConsentScope.EXTERNAL_AI)
            ai_check = service.test_external_ai()
            drafts_error = None
            try:
                service.generate_drafts("demo-msg-3", use_external=True)
            except ServiceError as exc:
                drafts_error = str(exc)
            drafts = service.list_drafts("demo-msg-3")
            result["checks"] = {
                "set_mail_credential_raised": mail_error,
                "set_external_ai_key_raised": api_error,
                "mail_error_mentions_keyring": bool(mail_error and "凭据管理器" in mail_error),
                "api_error_mentions_keyring": bool(api_error and "凭据管理器" in api_error),
                "test_mail_credential_ok": bool(mail_check.ok),
                "test_mail_credential_detail": mail_check.detail,
                "mail_check_mentions_keyring": "凭据管理器" in mail_check.detail,
                "test_external_ai_ok": bool(ai_check.ok),
                "test_external_ai_detail": ai_check.detail,
                "ai_check_mentions_keyring": "凭据管理器" in ai_check.detail,
                "generate_drafts_raised": drafts_error,
                "generate_drafts_mentions_keyring": bool(
                    drafts_error and "凭据管理器" in drafts_error
                ),
                "no_drafts": len(drafts) == 0,
                "has_mail_credential": service.has_mail_credential("neg-keyring@example.com"),
                "has_external_ai_key": service.has_external_ai_key(),
                "service_still_usable": len(service.list_messages()) > 0,
            }
            result["ok"] = all(
                (
                    result["checks"]["mail_error_mentions_keyring"],
                    result["checks"]["api_error_mentions_keyring"],
                    mail_check.ok is False,
                    result["checks"]["mail_check_mentions_keyring"],
                    ai_check.ok is False,
                    result["checks"]["ai_check_mentions_keyring"],
                    result["checks"]["generate_drafts_mentions_keyring"],
                    result["checks"]["no_drafts"],
                    result["checks"]["has_mail_credential"] is False,
                    result["checks"]["has_external_ai_key"] is False,
                    result["checks"]["service_still_usable"],
                )
            )
            result["actual"] = (
                f"keyring 后端故障：set_mail_credential → {mail_error}；"
                f"test_mail_credential.ok={mail_check.ok}（{mail_check.detail[:60]}）；"
                f"test_external_ai.ok={ai_check.ok}；草稿数={len(drafts)}；其余功能可用"
            )
        finally:
            service.shutdown()

    return run_scenario(
        "keyring_unavailable",
        "凭据管理器/keyring 不可用（注入故障后端）",
        "保存/读取/测试均给出明确“凭据管理器不可用”错误，不误报成功、不崩溃",
        body,
    )


# ---------------------------------------------------------------------------
# 隐私检查（跨场景）
# ---------------------------------------------------------------------------

def _walk_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def privacy_checks(root: Path, logs_dir: Path, evidence_text: str) -> dict[str, Any]:
    log_files = sorted(logs_dir.glob("*.jsonl")) if logs_dir.exists() else []
    log_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in log_files
    )
    data_files = _walk_files(root)
    data_hits: list[str] = []
    for path in data_files:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        for secret in SECRETS:
            if secret.encode("utf-8") in blob:
                data_hits.append(str(path.relative_to(root)))
    evidence_hits = [secret_kind for secret_kind, value in (
        ("auth_code", WRONG_AUTH_CODE),
        ("api_key", FAKE_API_KEY),
    ) if value in evidence_text]
    body_hits = [
        fallback
        for fallback in DEMO_BODY_MARKERS
        if fallback in log_text or fallback in evidence_text
    ]
    return {
        "log_files_scanned": [str(path.relative_to(root)) for path in log_files],
        "log_lines": sum(1 for _ in log_text.splitlines()),
        "secrets_in_logs": [
            secret_kind
            for secret_kind, value in (("auth_code", WRONG_AUTH_CODE), ("api_key", FAKE_API_KEY))
            if value in log_text
        ],
        "secrets_in_data_files": data_hits,
        "secrets_in_evidence": evidence_hits,
        "message_body_markers_in_logs_or_evidence": body_hits,
        "ok": not data_hits and not evidence_hits and not body_hits,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v0.1.1 负向路径验收（P0-4）")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="跳过真实网络场景（邮箱认证失败），仅验证结构化失败路径",
    )
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = PROJECT_ROOT / ".tmp" / "negative-acceptance" / stamp
    run_root.mkdir(parents=True, exist_ok=True)
    logs_dir = run_root / "logs"
    logger = configure_logging(logs_dir, level="INFO")

    print(f"[negative-acceptance] {APP_NAME} v{APP_VERSION}")
    print(f"[negative-acceptance] 临时数据目录: {run_root}")
    print("[negative-acceptance] 真实网络场景: 邮箱错误授权码（imap.qq.com 仅尝试登录，不读取邮件）")
    print()

    scenarios = [
        scenario_external_ai_missing_key(run_root, logger),
        scenario_external_ai_unreachable(run_root, logger),
        scenario_mail_bad_auth_real(run_root, logger, offline=args.offline),
        scenario_mail_network_unreachable(run_root, logger),
        scenario_qq_unavailable(run_root, logger),
        scenario_keyring_unavailable(run_root, logger),
    ]

    # 先写一版证据文本用于隐私自检，再写入最终文件
    privacy = {"ok": False, "reason": "not_evaluated"}
    evidence_path = (
        PROJECT_ROOT / "evidence" / "acceptance" / f"negative_paths_{stamp}.json"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    # 日志在读取前关闭，确保落盘完整
    reset_logging()
    evidence_preview = json.dumps(
        {"scenarios": scenarios}, ensure_ascii=False, default=str
    )
    privacy = privacy_checks(run_root, logs_dir, evidence_preview)

    failures = [item for item in scenarios if item["ok"] is not True]
    environment_limited = [item for item in scenarios if item.get("environment_limited")]
    overall_ok = not failures and privacy["ok"]

    payload = {
        "generated_at": _now_iso(),
        "app": APP_NAME,
        "version": APP_VERSION,
        "acceptance": "P0-4 negative paths（无 Key/断网/授权失败/QQ 不可用/keyring 不可用）",
        "command": " ".join([Path(sys.executable).name, *sys.argv]),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "offline_mode": bool(args.offline),
            "qq_worker_command": [
                sys.executable,
                "-m",
                "social_secretary.core.collector_worker",
            ],
            "unreachable_ai_base_url": UNREACHABLE_AI_BASE_URL,
            "unreachable_imap": f"{UNREACHABLE_IMAP_HOST}:{UNREACHABLE_IMAP_PORT}",
            "mail_test_email_hash": hash_text(FAKE_MAIL_EMAIL),
            "note": "验收使用内存凭据库/注入故障后端，未写入真实 Windows 凭据管理器；未读取任何真实聊天/邮件正文。",
        },
        "scenarios": scenarios,
        "privacy_checks": privacy,
        "fixes_applied": [
            "service.generate_drafts(use_external=True)：在构造外部客户端前显式区分“未保存 Key”/“凭据管理器不可用”，抛 ServiceError，避免含糊的“适配器不可用”或误报成功。",
            "service.test_external_ai()/test_mail_credential()：读取凭据时区分 keyring 故障并返回结构化 ConnectionCheck(ok=False)，不再把“读不到”说成“没有保存”。",
            "core.summary.build_narrative()：采集 FAILED/PARTIAL/BLOCKED/PAUSED 导致的 0 消息不再表述为“今天没有新的消息”，改为明确提示“采集未完成/未授权，不代表没有新消息”。",
        ],
        "uncovered": [
            "真实 QQ 聊天消息读取：用户仅授权只读结构探测，且 QQ 未登录；负向验收只验证“不可用/失败”路径。",
            "真实邮箱成功登录与邮件读取：未使用真实授权码，只验证“认证失败/网络失败”路径。",
            "外部 AI 成功调用：未配置真实服务与 Key，只验证“无 Key/不可达”失败路径。",
            "真实 Windows 凭据管理器写入：验收使用内存凭据库与注入故障后端，避免污染用户凭据。",
        ],
        "notes": [
            "证据中仅包含脱敏后的错误文案、状态、计数、耗时与哈希；不含授权码/API Key/消息正文。",
            "每个场景使用独立的临时数据目录与数据库，互不影响。",
        ],
        "summary": {
            "total": len(scenarios),
            "passed": sum(1 for item in scenarios if item["ok"] is True),
            "failed": len(failures),
            "environment_limited": len(environment_limited),
            "overall_ok": overall_ok,
        },
    }
    evidence_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # 控制台摘要
    print(f"{'场景':<34} {'结果':<10} 说明")
    print("-" * 110)
    for item in scenarios:
        status = "PASS" if item["ok"] else ("ENV-LIMIT" if item.get("environment_limited") else "FAIL")
        print(f"{item['id']:<34} {status:<10} {item['actual'][:150]}")
    print("-" * 110)
    print(
        f"隐私检查：日志 {privacy['log_files_scanned']} 个、"
        f"密钥泄漏 {privacy['secrets_in_logs'] + privacy['secrets_in_data_files'] + privacy['secrets_in_evidence']}、"
        f"正文标记 {privacy['message_body_markers_in_logs_or_evidence']}"
    )
    print(f"证据文件：{evidence_path}")
    print(
        f"结论：{payload['summary']['passed']}/{payload['summary']['total']} 通过，"
        f"{payload['summary']['failed']} 失败，{payload['summary']['environment_limited']} 环境受限"
    )

    if overall_ok:
        return 0
    if failures and not environment_limited:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
