"""ServiceAPI 实现：UI 唯一依赖的业务门面。

职责
----
- 账号 / 会话白名单 / 采集编排 / 摘要 / 消息 / 待办 / 草稿 / 设置 / 同意 / 凭据 / 演示数据。
- **绝不提供发送能力**：本模块不存在 SMTP、QQ 输入框输入、点击发送等任何代码路径。
- 外部 AI 门控：``generate_drafts(use_external=True)`` 与 ``test_external_ai()`` 必须先
  通过 ``ConsentScope.EXTERNAL_AI`` 授权，未授权在**创建适配器之前**就抛
  :class:`ConsentRequired`；默认草稿使用本地模板，零联网。
- 凭据只进凭据管理器（``CredentialStore``）；日志只出现 id/计数/哈希。

接线方式
--------
- 采集器：构造时注入 ``source_factory(account) -> MessageSource | None``；
  未注入时按账号类型做**内置接线**：
  * 邮件账号 → ``collectors.mail.MailCollector``（凭据来自 ``CredentialStore``，
    增量状态来自内置 SQLite ``StorageSyncStateStore``，文件夹取会话白名单；
    imaplib 纯 Python，允许留在主进程）；
  * QQ 账号 → ``core.collector_worker.WorkerQQMessageSource``：**UIA/comtypes 只在实际读取的
    子进程中 import**（``python -m social_secretary.core.collector_worker``；冻结版
    ``sys.executable --collector-worker``），主进程只收结构化 JSON，避免 COM 初始化把 UI 拖崩；
    子进程崩溃/超时/坏回传由流水线降级为 PARTIAL/FAILED。
  采集器模块不可用时退回惰性发现/返回 ``ConnectionCheck(ok=False)``（不崩溃）。
  也可用 :func:`set_default_source_factory` 全局注册自定义工厂（测试/高级接线）。
- intel：构造参数未注入时直接使用 ``RuleClassifier`` / ``RuleTodoExtractor`` /
  ``LocalDraftProvider``；intel 缺失时回退到核心层的安全空实现（不联网）。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import APP_ID, APP_NAME, APP_VERSION, AppPaths
from ..contracts import (
    Account,
    AccountKind,
    AppSettings,
    Category,
    Classifier,
    CollectionResult,
    ConnectionCheck,
    ConsentScope,
    Conversation,
    ConversationKind,
    DailySummary,
    DraftContext,
    DraftKind,
    DraftProvider,
    ExternalAIConfig,
    Message,
    MessageWorkStatus,
    Priority,
    ReplyDraft,
    ScanStatus,
    SourceDetail,
    TodoExtractor,
    TodoItem,
    TodoStatus,
)
from ..security.audit import AuditLogger
from ..security.consent import ConsentManager, ConsentRequired
from ..security.credential_store import (
    EXTERNAL_AI_KEY,
    CredentialStoreError,
    build_default_credential_store,
    mail_credential_key,
)
from ..security.log_redaction import hash_text, looks_like_secret, redact_text, safe_error
from .demo import (
    DEMO_ACCOUNT_IDS,
    clear_demo_data as _clear_demo_data,
    is_demo_active as _is_demo_active,
    load_demo_data as _load_demo_data,
)
from .collector_worker import WorkerQQMessageSource
from .dedupe import compute_content_hash
from .drafts import ExternalDraftAdapter, TemplateDraftProvider
from .mail_state import StorageSyncStateStore
from .pipeline import NullClassifier, NullTodoExtractor, ScanPipeline
from .settings_store import SettingsStore
from .storage import Storage, is_demo_account_id, now_local, to_iso
from .summary import generate_daily_summary as _generate_summary
from .summary import get_cached_summary as _get_cached_summary

SourceFactory = Callable[[Account], Any | None]

# 默认值/限制
DEFAULT_DRAFT_STYLES = ("简洁", "礼貌")
_MAX_DIAGNOSTIC_ITEMS = 50

# 实验性聊天文本导入（P0-17）：只处理用户主动提供的文本，不联网、不读 QQ 数据库。
IMPORT_ACCOUNT_ID = "import-account"
IMPORT_ACCOUNT_IDENTIFIER = "imported-text"
IMPORT_ACCOUNT_DISPLAY_NAME = "导入聊天文本（实验性）"
_IMPORT_NOTICE_FALLBACK = (
    "实验性功能：聊天文本导入不保证兼容任意导出格式，需用户手动提供文本；"
    "只在本机解析与保存，不会自动同步，也不等于“自动读取 QQ 聊天记录”。"
)

# 实验性 QQ 客户端只读（P0-22）：默认关闭；需用户在设置中显式开启后才允许启动 worker/UIA。
QQ_EXPERIMENTAL_DISABLED_RUN_SCAN = (
    "实验性 QQ 客户端读取未启用（默认关闭）。请到设置与隐私→QQ 读取（实验）开启后再试。"
)
QQ_EXPERIMENTAL_DISABLED_TEST = "实验性 QQ 客户端读取未启用（默认关闭）"


class ServiceError(RuntimeError):
    """服务层可展示给用户的错误（中文、不含隐私内容）。"""


class NotFoundError(ServiceError):
    """请求的资源不存在。"""


# ---------------------------------------------------------------------------
# 默认依赖发现（intel / collectors 并行开发时保持核心层可独立测试）
# ---------------------------------------------------------------------------

def _load_default_classifier() -> Classifier:
    try:
        from ..intel.rules import RuleClassifier  # type: ignore[import-not-found]

        return RuleClassifier()
    except Exception:  # noqa: BLE001 - intel 未就绪时回退
        return NullClassifier()


def _load_default_todo_extractor(clock: Callable[[], datetime] | None = None) -> TodoExtractor:
    try:
        from ..intel.rules import RuleTodoExtractor  # type: ignore[import-not-found]

        return RuleTodoExtractor(now_provider=clock)
    except Exception:  # noqa: BLE001
        return NullTodoExtractor()


def _load_default_draft_provider(clock: Callable[[], datetime] | None = None) -> DraftProvider:
    try:
        from ..intel.drafts import LocalDraftProvider  # type: ignore[import-not-found]

        return LocalDraftProvider(now_provider=clock)
    except Exception:  # noqa: BLE001
        return TemplateDraftProvider()


_MAIL_SOURCE_MODULES = (
    "social_secretary.collectors.mail.collector",
    "social_secretary.collectors.mail",
)
_MAIL_SOURCE_CLASSES = (
    "MailCollector",
    "MailMessageSource",
    "IMAPMessageSource",
    "MailSource",
    "ImapMailSource",
)

_default_source_factory_override: SourceFactory | None = None


def set_default_source_factory(factory: SourceFactory | None) -> None:
    """全局注册采集器工厂（app.py 组装阶段调用；传 None 恢复默认接线）。"""
    global _default_source_factory_override
    _default_source_factory_override = factory


def default_source_factory(account: Account) -> Any | None:
    """通用自动发现：仅用于邮件等非 COM 采集器；**QQ UIA 必须走 collector_worker 子进程**。

    - 订阅 QQ 账号：返回 ``None``（服务层内置接线的 ``WorkerQQMessageSource`` 才符合
      子进程隔离要求；这里绝不 import collectors.qq，避免主进程加载 uiautomation/comtypes）。
    - 邮件账号：尝试发现 ``MailCollector`` 等实现（服务层通常已内置接线，此为兜底）。
    """
    if _default_source_factory_override is not None:
        return _default_source_factory_override(account)
    if account.kind == AccountKind.QQ:
        return None
    modules, class_names = _MAIL_SOURCE_MODULES, _MAIL_SOURCE_CLASSES
    for module_path in modules:
        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - 采集器可能依赖 imaplib 等
            continue
        for class_name in class_names:
            candidate = getattr(module, class_name, None)
            if not isinstance(candidate, type):
                continue
            for args, kwargs in (((account,), {}), ((), {"account": account}), ((), {})):
                try:
                    return candidate(*args, **kwargs)
                except Exception:  # noqa: BLE001 - 构造参数不匹配则尝试下一种
                    continue
    return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SocialSecretaryService:
    """实现 :class:`social_secretary.contracts.ServiceAPI`。"""

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        storage: Storage | None = None,
        settings_store: SettingsStore | None = None,
        credential_store: Any | None = None,
        sync_state_store: Any | None = None,
        source_factory: SourceFactory | None = None,
        qq_worker_command: Sequence[str] | None = None,
        qq_worker_timeout: float | None = None,
        qq_worker_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        qq_worker_env: Mapping[str, str] | None = None,
        qq_worker_output_dir: Path | str | None = None,
        classifier: Classifier | None = None,
        todo_extractor: TodoExtractor | None = None,
        draft_provider: DraftProvider | None = None,
        external_provider_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
        auto_configure_logging: bool = False,
    ) -> None:
        self._paths = paths or AppPaths.create()
        self._clock = clock or now_local
        self._storage = storage or Storage(self._paths.db_path)
        self._storage.initialize()
        self._settings_store = settings_store or SettingsStore(self._paths.config_path)
        self._credentials = credential_store or build_default_credential_store()
        # 邮件 IMAP 增量同步状态（UIDVALIDITY + next_uid）：SQLite 持久化，掉电不重复拉取
        self._sync_state_store = sync_state_store or StorageSyncStateStore(self._storage)
        # 邮箱地址 -> 采集器实际使用的凭据 key（如 mail.auth_code.<email>）
        self._mail_credential_keys: dict[str, str] = {}
        self._source_factory = source_factory
        # QQ UIA 子进程隔离（见 core/collector_worker.py）；测试可注入 runner/command
        self._qq_worker_command = list(qq_worker_command) if qq_worker_command else None
        self._qq_worker_timeout = qq_worker_timeout
        self._qq_worker_runner = qq_worker_runner
        self._qq_worker_env = dict(qq_worker_env) if qq_worker_env else None
        self._qq_worker_output_dir = (
            Path(qq_worker_output_dir)
            if qq_worker_output_dir is not None
            else self._paths.data_dir / "ipc"
        )
        self._classifier = classifier
        self._todo_extractor = todo_extractor
        self._draft_provider = draft_provider
        self._external_provider_factory = external_provider_factory
        self._consent = ConsentManager(self._storage)
        self._audit = AuditLogger(self._storage)
        self._logger = logger or logging.getLogger("social_secretary")
        self._shutdown = False
        self._scanning = False
        self._last_scan_error: str | None = None
        if auto_configure_logging:
            from ..security.logging_setup import configure_logging

            self._logger = configure_logging(
                self._paths.logs_dir, level=self._settings_store.load().log_level
            )

    # -- 只读属性（测试 / app.py 组装使用）--------------------------------

    @property
    def paths(self) -> AppPaths:
        return self._paths

    @property
    def storage(self) -> Storage:
        return self._storage

    @property
    def credential_store(self) -> Any:
        return self._credentials

    @property
    def consent_manager(self) -> ConsentManager:
        return self._consent

    @property
    def mail_sync_state_store(self) -> Any:
        return self._sync_state_store

    # -- 账号 ---------------------------------------------------------------

    def list_accounts(self) -> list[Account]:
        self._ensure_open()
        return self._storage.list_accounts()

    def add_qq_account(self, display_name: str, allowlist: list[str]) -> Account:
        self._ensure_open()
        name = (display_name or "").strip()
        if not name:
            raise ServiceError("账号显示名称不能为空")
        account = Account(
            id=f"qq-{uuid.uuid4().hex[:12]}",
            kind=AccountKind.QQ,
            display_name=name,
            enabled=True,
            created_at=self._now(),
            identifier="",
        )
        self._storage.upsert_account(account)
        entries = [str(item).strip() for item in (allowlist or []) if str(item).strip()]
        self._set_allowlist(account.id, entries)
        self._audit.log(
            "account_added",
            target_kind="account",
            target_id=account.id,
            detail={"kind": AccountKind.QQ.value, "allowlist_count": len(entries)},
        )
        return account

    def add_mail_account(
        self, email: str, display_name: str, folder: str = "INBOX"
    ) -> Account:
        self._ensure_open()
        normalized = (email or "").strip().lower()
        if "@" not in normalized or len(normalized) < 3:
            raise ServiceError("邮箱地址格式不正确")
        if self._storage.find_account_by_identifier(AccountKind.EMAIL, normalized) is not None:
            raise ServiceError("该邮箱账号已存在")
        folder_name = (folder or "INBOX").strip() or "INBOX"
        # 与 mail 采集器保持一致：INBOX 大小写不敏感，统一成大写
        mailbox = "INBOX" if folder_name.upper() == "INBOX" else folder_name
        name = (display_name or "").strip() or normalized
        account = Account(
            id=f"mail-{uuid.uuid4().hex[:12]}",
            kind=AccountKind.EMAIL,
            display_name=name,
            enabled=True,
            created_at=self._now(),
            identifier=normalized,
        )
        self._storage.upsert_account(account)
        self._storage.upsert_conversation(
            Conversation(
                id=f"conv-{uuid.uuid4().hex[:12]}",
                account_id=account.id,
                kind=ConversationKind.MAIL,
                title=mailbox,
                # 邮件采集器用 IMAP 文件夹名作为 external_key（如 "INBOX"）
                external_key=mailbox,
                enabled=True,
            )
        )
        self._set_allowlist(account.id, [mailbox])
        self._audit.log(
            "account_added",
            target_kind="account",
            target_id=account.id,
            detail={"kind": AccountKind.EMAIL.value, "folder_count": 1},
        )
        return account

    def set_account_enabled(self, account_id: str, enabled: bool) -> None:
        self._ensure_open()
        if not self._storage.set_account_enabled(account_id, bool(enabled)):
            raise NotFoundError(f"账号不存在：{account_id}")
        self._audit.log(
            "account_enabled_changed",
            target_kind="account",
            target_id=account_id,
            detail={"enabled": bool(enabled)},
        )

    def remove_account(self, account_id: str, delete_messages: bool = True) -> None:
        self._ensure_open()
        if not self._storage.delete_account(account_id, delete_messages=bool(delete_messages)):
            raise NotFoundError(f"账号不存在：{account_id}")
        self._storage.delete_setting(self._allowlist_key(account_id))
        self._storage.clear_summaries()
        self._audit.log(
            "account_removed",
            target_kind="account",
            target_id=account_id,
            detail={"delete_messages": bool(delete_messages)},
        )

    # -- 会话 ---------------------------------------------------------------

    def list_conversations(self, account_id: str | None = None) -> list[Conversation]:
        self._ensure_open()
        return self._storage.list_conversations(account_id)

    def set_conversation_enabled(self, conversation_id: str, enabled: bool) -> None:
        self._ensure_open()
        if not self._storage.update_conversation(conversation_id, enabled=bool(enabled)):
            raise NotFoundError(f"会话不存在：{conversation_id}")
        self._audit.log(
            "conversation_enabled_changed",
            target_kind="conversation",
            target_id=conversation_id,
            detail={"enabled": bool(enabled)},
        )

    def set_conversation_priority(self, conversation_id: str, priority: Priority) -> None:
        self._ensure_open()
        if not isinstance(priority, Priority):
            raise ServiceError("优先级取值不合法")
        if not self._storage.update_conversation(conversation_id, priority=priority):
            raise NotFoundError(f"会话不存在：{conversation_id}")
        self._audit.log(
            "conversation_priority_changed",
            target_kind="conversation",
            target_id=conversation_id,
            detail={"priority": priority.value},
        )

    def refresh_conversations(self, account_id: str) -> ConnectionCheck:
        self._ensure_open()
        account = self._require_account(account_id)
        if account.kind is AccountKind.IMPORT:
            return ConnectionCheck(
                ok=False,
                detail="导入聊天文本账号是本地实验数据，不参与采集，无法刷新会话列表。",
                discovered={"kind": AccountKind.IMPORT.value, "unsupported": True},
            )
        try:
            self._require_read_consent(account, action="刷新会话列表")
        except ConsentRequired as exc:
            return ConnectionCheck(
                ok=False,
                detail=str(exc),
                discovered={"consent_required": True, "scope": self._read_scope(account).value},
            )
        source = self._make_source(account)
        if source is None:
            return ConnectionCheck(
                ok=False,
                detail=self._source_unavailable_detail(account),
                discovered={"available": False},
            )
        try:
            discovered = source.list_conversations() or []
        except Exception as exc:  # noqa: BLE001 - 采集器异常转为可展示结果
            return ConnectionCheck(
                ok=False,
                detail=f"读取会话列表失败（{safe_error(exc)}）",
                discovered={"available": True},
            )
        finally:
            self._close_source(source)

        allowlist = set(self._get_allowlist(account.id))
        new_count = 0
        enabled_count = 0
        for candidate in discovered:
            if not isinstance(candidate, Conversation):
                continue
            existing = self._storage.find_conversation_by_external_key(
                account.id, candidate.external_key
            )
            if existing is not None:
                continue
            allowed = bool(
                candidate.title in allowlist or candidate.external_key in allowlist
            )
            new_count += 1
            enabled_count += int(allowed)
            self._storage.upsert_conversation(
                Conversation(
                    id=candidate.id or uuid.uuid4().hex,
                    account_id=account.id,
                    kind=candidate.kind,
                    title=candidate.title,
                    external_key=candidate.external_key,
                    enabled=allowed,
                    priority=candidate.priority,
                    note=candidate.note,
                )
            )
        self._storage.clear_summaries()
        self._audit.log(
            "conversations_refreshed",
            target_kind="account",
            target_id=account.id,
            detail={"total": len(discovered), "new": new_count, "enabled": enabled_count},
        )
        return ConnectionCheck(
            ok=True,
            detail=f"发现 {len(discovered)} 个会话（新增 {new_count} 个，已授权 {enabled_count} 个）",
            discovered={
                "total": len(discovered),
                "new": new_count,
                "enabled": enabled_count,
            },
        )

    # -- 采集 ---------------------------------------------------------------

    def test_account(self, account_id: str) -> ConnectionCheck:
        self._ensure_open()
        account = self._require_account(account_id)
        if account.kind is AccountKind.IMPORT:
            return ConnectionCheck(
                ok=False,
                detail="导入聊天文本账号是本地实验数据，不进行采集/连接测试（不会访问 QQ 或邮箱）。",
                discovered={"kind": AccountKind.IMPORT.value, "unsupported": True},
            )
        if (
            account.kind is AccountKind.QQ
            and not self._settings_store.load().qq_read_experimental
        ):
            # 默认关闭：连 worker 都不启动（不会 import uiautomation/启动 UIA）。
            return ConnectionCheck(
                ok=False,
                detail=QQ_EXPERIMENTAL_DISABLED_TEST,
                discovered={
                    "kind": AccountKind.QQ.value,
                    "experimental_disabled": True,
                },
            )
        source = self._make_source(account)
        if source is None:
            return ConnectionCheck(ok=False, detail=self._source_unavailable_detail(account))
        try:
            return self._coerce_connection_check(source.test_connection())
        except Exception as exc:  # noqa: BLE001
            return ConnectionCheck(ok=False, detail=f"连接测试失败（{safe_error(exc)}）")
        finally:
            self._close_source(source)

    def run_scan(
        self,
        account_id: str | None = None,
        *,
        trigger: str = "manual",
        foreground_ack: bool = False,
    ) -> list[CollectionResult]:
        self._ensure_open()
        import_requested = False
        if account_id is None:
            accounts = [
                account
                for account in self._storage.list_accounts()
                if account.enabled and account.kind is not AccountKind.IMPORT
            ]
        else:
            accounts = [self._require_account(account_id)]
            import_requested = any(
                account.kind is AccountKind.IMPORT for account in accounts
            )
        if import_requested:
            return [
                self._blocked_result(
                    account,
                    ScanStatus.BLOCKED,
                    "导入聊天文本账号是本地实验数据（非 QQ/邮箱），不参与采集。",
                )
                for account in accounts
            ]
        if not accounts:
            return []

        settings = self._settings_store.load()
        if settings.collection_paused:
            return [
                self._blocked_result(account, ScanStatus.PAUSED, "采集已暂停")
                for account in accounts
            ]

        results: list[CollectionResult] = []
        allowed: list[str] = []
        for account in accounts:
            if account.kind is AccountKind.IMPORT:
                results.append(
                    self._blocked_result(
                        account,
                        ScanStatus.BLOCKED,
                        "导入聊天文本账号是本地实验数据（非 QQ/邮箱），不参与采集。",
                    )
                )
                continue
            if not account.enabled:
                results.append(self._blocked_result(account, ScanStatus.BLOCKED, "账号已停用"))
                continue
            if (
                account.kind is AccountKind.QQ
                and not settings.qq_read_experimental
            ):
                # P0-22：实验性 QQ 读取默认关闭；未开启时直接 BLOCKED，
                # 不构造 WorkerQQMessageSource、不启动子进程/UIA。
                blocked = self._blocked_result(
                    account, ScanStatus.BLOCKED, QQ_EXPERIMENTAL_DISABLED_RUN_SCAN
                )
                self._storage.insert_scan_run(blocked, trigger=trigger)
                results.append(blocked)
                continue
            try:
                self._require_read_consent(account)
            except ConsentRequired as exc:
                blocked = self._blocked_result(account, ScanStatus.BLOCKED, str(exc))
                self._storage.insert_scan_run(blocked, trigger=trigger)
                results.append(blocked)
                continue
            allowed.append(account.id)

        if allowed:
            pipeline = self._build_pipeline()
            self._scanning = True
            try:
                results.extend(
                    pipeline.run(
                        allowed,
                        trigger=trigger,
                        foreground_ack=bool(foreground_ack),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 兜底：流水线本身异常也不能崩溃
                safe_message = safe_error(exc)
                self._last_scan_error = safe_message
                for account_id_value in allowed:
                    failed = self._failed_result(account_id_value, safe_message)
                    self._storage.insert_scan_run(failed, trigger=trigger)
                    results.append(failed)
            finally:
                self._scanning = False

        for result in results:
            if result.status in (ScanStatus.FAILED, ScanStatus.PARTIAL) and result.error:
                self._last_scan_error = result.error
        self._storage.clear_summaries()
        self._audit.log(
            "scan_finished",
            detail={
                "trigger": str(trigger),
                "accounts": len(accounts),
                "scanned": len(allowed),
                "results": len(results),
            },
        )
        return results

    def organize_all(self, *, foreground_ack: bool = False) -> dict[str, Any]:
        """一键整理所有已配置账号：批量扫描（邮箱 + QQ 白名单）后生成本地今日简报。

        - 只调用现有 ``run_scan(None, trigger="one_click")`` 批量路径，**不打开/定位当前聊天窗口**；
        - 单个账号/会话失败不阻断其他账号；
        - 简报使用本地规则（即使已启用外部 AI 摘要模式也只生成本地结果，不上传）；
        - 不发送任何消息；审计仅计数/耗时/provider，无正文/标题；
        - 返回值带可展示 details/历史信息/QQ 失败提示（供 UI，自身不写审计）。
        """
        self._ensure_open()
        started = time.perf_counter()
        accounts = self._storage.list_accounts()
        qq_configured = any(
            account.kind is AccountKind.QQ
            and account.enabled
            and bool(self._get_allowlist(account.id))
            for account in accounts
        )
        mail_configured = any(
            account.kind is AccountKind.EMAIL and account.enabled
            for account in accounts
        )
        configured = {"qq": qq_configured, "mail": mail_configured}
        errors: list[str] = []

        # 历史消息概览（只读本地库；与账号无关，便于失败时告诉用户“历史还在”）
        history_total = self._storage.count_messages()
        all_account_ids = [account.id for account in accounts]
        latest_day = (
            self._storage.latest_message_day(all_account_ids)
            if all_account_ids
            else None
        )
        latest_message_day = latest_day.isoformat() if latest_day else None
        qq_accounts = [
            account
            for account in accounts
            if account.kind is AccountKind.QQ and account.enabled
        ]
        qq_authorized = sum(
            1
            for account in qq_accounts
            for conversation in self._storage.list_conversations(account.id)
            if conversation.enabled
        )

        if not (qq_configured or mail_configured):
            prompt = "还没有可整理的账号，请先完成 QQ/邮箱授权"
            self._audit.log(
                "organize_all",
                detail={
                    "configured": configured,
                    "result_count": 0,
                    "duration_ms": 0,
                    "provider": "local_rules",
                },
            )
            return {
                "ok": False,
                "configured": configured,
                "results": [],
                "summary": None,
                "qq_success": 0,
                "qq_failed": 0,
                "mail_success": 0,
                "mail_failed": 0,
                "errors": [prompt],
                "duration_ms": 0,
                "details": [],
                "qq_authorized": qq_authorized,
                "qq_sessions_scanned": 0,
                "qq_sessions_failed": qq_authorized,
                "qq_failure_hint": None,
                "history_total": history_total,
                "latest_message_day": latest_message_day,
            }

        kind_by_id = {account.id: account.kind for account in accounts}
        results: list[CollectionResult] = []
        try:
            results = self.run_scan(
                None, trigger="one_click", foreground_ack=bool(foreground_ack)
            )
        except Exception as exc:  # noqa: BLE001 - 批量入口不能崩 UI
            errors.append(f"批量扫描失败（{type(exc).__name__}）")
            results = []

        qq_success = qq_failed = mail_success = mail_failed = 0
        details: list[dict[str, Any]] = []
        qq_results: list[CollectionResult] = []
        success_statuses = (ScanStatus.OK, ScanStatus.PARTIAL)
        for result in results:
            kind = kind_by_id.get(result.account_id)
            if kind is AccountKind.QQ:
                qq_results.append(result)
                if result.status in success_statuses:
                    qq_success += 1
                else:
                    qq_failed += 1
            elif kind is AccountKind.EMAIL:
                if result.status in success_statuses:
                    mail_success += 1
                else:
                    mail_failed += 1
            else:
                # IMPORT 账号不参与采集（run_scan 本身也会排除）
                continue
            details.append(
                {
                    "kind": "qq" if kind is AccountKind.QQ else "email",
                    "status": result.status.value,
                    "error": result.error,
                    "warnings": [str(item) for item in (result.warnings or ())],
                    "messages_new": int(result.messages_new),
                    "conversations_scanned": int(result.conversations_scanned),
                }
            )

        qq_sessions_scanned = sum(
            max(0, int(result.conversations_scanned)) for result in qq_results
        )
        qq_sessions_failed = max(0, qq_authorized - qq_sessions_scanned)

        # QQ 失败提示（仅返回给 UI，不写审计）
        qq_failure_hint: str | None = None
        joined_qq_text = "\n".join(
            [str(result.error or "") for result in qq_results]
            + [
                str(warning)
                for result in qq_results
                for warning in (result.warnings or ())
            ]
        )
        if any(
            marker in joined_qq_text
            for marker in ("qq_window_unavailable", "无法将 QQ 主面板置前")
        ):
            qq_failure_hint = (
                "QQ 主面板无法置前（可能被最小化到托盘或被其他窗口遮挡）。"
                "请先打开 QQ 主面板，保持可见，再重试。"
            )
        elif qq_failed > 0 or any(result.error for result in qq_results):
            qq_failure_hint = "QQ 读取失败，请确认 QQ 已登录且主面板未被遮挡"
        if qq_failure_hint and qq_failure_hint not in errors:
            errors.append(qq_failure_hint)

        summary: DailySummary | None = None
        try:
            summary = self.generate_daily_summary(day=now_local().date())
        except Exception as exc:  # noqa: BLE001 - 简报失败不阻断本次整理
            summary = None
            errors.append(f"今日简报生成失败（{type(exc).__name__}）")

        # 扫描后再取历史概览，确保包含本次新增消息
        history_total = self._storage.count_messages()
        latest_day = (
            self._storage.latest_message_day(all_account_ids)
            if all_account_ids
            else None
        )
        latest_message_day = latest_day.isoformat() if latest_day else None

        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        self._audit.log(
            "organize_all",
            detail={
                "configured": configured,
                "qq_success": qq_success,
                "qq_failed": qq_failed,
                "mail_success": mail_success,
                "mail_failed": mail_failed,
                "result_count": len(results),
                "duration_ms": duration_ms,
                "provider": "local_rules",
            },
        )
        return {
            "ok": True,
            "configured": configured,
            "results": results,
            "summary": summary,
            "qq_success": qq_success,
            "qq_failed": qq_failed,
            "mail_success": mail_success,
            "mail_failed": mail_failed,
            "errors": errors,
            "duration_ms": duration_ms,
            "details": details,
            "qq_authorized": qq_authorized,
            "qq_sessions_scanned": qq_sessions_scanned,
            "qq_sessions_failed": qq_sessions_failed,
            "qq_failure_hint": qq_failure_hint,
            "history_total": history_total,
            "latest_message_day": latest_message_day,
        }

    def collection_status(self) -> dict[str, Any]:
        self._ensure_open()
        settings = self._settings_store.load()
        last = self._storage.last_scan_run()
        return {
            "paused": bool(settings.collection_paused),
            "running": bool(self._scanning),
            "last_scan_at": (last.finished_at or last.started_at) if last else None,
            "last_scan_status": last.status.value if last else "",
            "last_error": (last.error if last is not None else None) or self._last_scan_error,
        }

    def pause_collection(self) -> None:
        self._ensure_open()
        self._settings_store.save(replace(self._settings_store.load(), collection_paused=True))
        self._audit.log("collection_paused")

    def resume_collection(self) -> None:
        self._ensure_open()
        self._settings_store.save(
            replace(self._settings_store.load(), collection_paused=False)
        )
        self._audit.log("collection_resumed")

    # -- 一键读取当前 QQ 聊天（实验性；用户一次确认完成授权/账号/白名单/刷新/扫描）------

    def _quick_qq_result(self, **overrides: Any) -> dict[str, Any]:
        """quick_qq_read 的固定响应结构（不含标题原文）。"""
        result: dict[str, Any] = {
            "ok": False,
            "account_id": None,
            "added_account": False,
            "detected_title_len": 0,
            "refresh_ok": False,
            "scan_status": None,
            "messages_seen": 0,
            "messages_new": 0,
            "conversations_scanned": 0,
            "error": None,
        }
        result.update(overrides)
        return result

    def _detect_current_qq_chat(self) -> tuple[str | None, str | None]:
        """worker 子进程 detect_current_chat；返回 (title, error)。

        标题只在内存中交给白名单使用，**不写日志/审计**。
        """
        from .collector_worker import (
            DEFAULT_TIMEOUTS as WORKER_TIMEOUTS,
            CollectorWorkerError,
            run_worker_task,
        )

        task: dict[str, Any] = {"version": 1, "mode": "detect_current_chat"}
        timeout = (
            float(self._qq_worker_timeout)
            if self._qq_worker_timeout
            else float(WORKER_TIMEOUTS["detect_current_chat"])
        )
        try:
            if self._qq_worker_runner is not None:
                data = self._qq_worker_runner(dict(task))
            else:
                data = run_worker_task(
                    task,
                    command=self._qq_worker_command,
                    timeout=timeout,
                    output_dir=self._qq_worker_output_dir,
                    env=self._qq_worker_env,
                )
        except CollectorWorkerError as exc:
            message = str(exc)
            if "no_current_chat" in message:
                return None, "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天"
            return None, f"QQ 读取子进程失败（{redact_text(message, max_len=200)}）"
        except Exception as exc:  # noqa: BLE001 - 调用失败一律转结构化错误
            return None, f"QQ 读取子进程失败（{type(exc).__name__}）"

        if not isinstance(data, dict):
            return None, "QQ 读取子进程返回格式错误"
        if data.get("ok") is not True:
            error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
            if str(error.get("code") or "") == "no_current_chat":
                return None, "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天"
            message = redact_text(
                str(error.get("message") or "QQ 读取失败"), max_len=200
            )
            return None, f"QQ 读取失败（{message}）"
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天"
        return title.strip(), None

    def quick_qq_read(self, *, user_confirmed: bool = False) -> dict[str, Any]:
        """一键读取当前 QQ 聊天（实验性只读）。

        QQ 已打开目标测试会话时，用户勾选/确认只读授权后点击一次：自动开启实验开关、
        写入 QQ 读取同意、创建/复用本机 QQ 账号与白名单、刷新会话并扫描当前会话。
        **不发送任何消息、不读本地 QQ 数据库、不保存标题原文到日志/证据。**
        """
        self._ensure_open()
        if not user_confirmed:
            return self._quick_qq_result(error="需要用户确认只读授权")

        title, detect_error = self._detect_current_qq_chat()
        if detect_error:
            return self._quick_qq_result(error=detect_error)
        detected_title = str(title or "")
        detected_title_len = len(detected_title)

        try:
            # 1) 用户已确认：允许实验读取 + 恢复采集
            self.update_settings(qq_read_experimental=True, collection_paused=False)
            # 2) 只读授权（写入 consent_log，不记录标题）
            self.grant_consent(
                ConsentScope.QQ_READ,
                confirmation_text="一键读取 QQ（用户授权当前聊天）",
            )
            # 3) 创建/复用 QQ 账号，并把检测到的标题并入白名单
            qq_accounts = [
                account
                for account in self._storage.list_accounts()
                if account.kind is AccountKind.QQ
            ]
            if qq_accounts:
                account = next(
                    (item for item in qq_accounts if item.enabled), qq_accounts[0]
                )
                allowlist = self._get_allowlist(account.id)
                if detected_title not in allowlist:
                    self._set_allowlist(account.id, [*allowlist, detected_title])
                if not account.enabled:
                    self.set_account_enabled(account.id, True)
                added_account = False
            else:
                account = self.add_qq_account("本机 QQ（实验）", [detected_title])
                added_account = True

            # 4) 刷新会话列表（失败也继续：下面会兜底创建/启用目标会话）
            refresh_ok = False
            try:
                refresh_ok = bool(self.refresh_conversations(account.id).ok)
            except Exception:  # noqa: BLE001 - 刷新失败不阻塞一键读取
                refresh_ok = False
            conversation = self._storage.find_conversation_by_external_key(
                account.id, detected_title
            )
            if conversation is None:
                conversation = Conversation(
                    id=f"conv-quick-{uuid.uuid4().hex[:12]}",
                    account_id=account.id,
                    kind=ConversationKind.PRIVATE,
                    title=detected_title,
                    external_key=detected_title,
                    enabled=True,
                    note="一键读取（实验性；仅用户本次确认的当前聊天）",
                )
                self._storage.upsert_conversation(conversation)
            elif not conversation.enabled:
                self._storage.update_conversation(conversation.id, enabled=True)

            # 5) 只读扫描当前会话（foreground_ack=True：用户已明确确认）
            scan = self.run_scan(
                account.id, trigger="quick_qq_read", foreground_ack=True
            )
            scan_result = scan[0] if scan else None
            if scan_result is None:
                return self._quick_qq_result(
                    account_id=account.id,
                    added_account=added_account,
                    detected_title_len=detected_title_len,
                    refresh_ok=refresh_ok,
                    error="QQ 读取未返回结果，请稍后重试",
                )
            if scan_result.status is not ScanStatus.OK:
                return self._quick_qq_result(
                    account_id=account.id,
                    added_account=added_account,
                    detected_title_len=detected_title_len,
                    refresh_ok=refresh_ok,
                    scan_status=scan_result.status.value,
                    messages_seen=scan_result.messages_seen,
                    messages_new=scan_result.messages_new,
                    conversations_scanned=scan_result.conversations_scanned,
                    error=scan_result.error
                    or f"QQ 读取失败（状态：{scan_result.status.value}）",
                )
            self._audit.log(
                "quick_qq_read",
                target_kind="account",
                target_id=account.id,
                detail={
                    "added_account": added_account,
                    "detected_title_len": detected_title_len,
                    "refresh_ok": refresh_ok,
                    "messages_seen": scan_result.messages_seen,
                    "messages_new": scan_result.messages_new,
                    "conversations_scanned": scan_result.conversations_scanned,
                },
            )
            return self._quick_qq_result(
                ok=True,
                account_id=account.id,
                added_account=added_account,
                detected_title_len=detected_title_len,
                refresh_ok=refresh_ok,
                scan_status=scan_result.status.value,
                messages_seen=scan_result.messages_seen,
                messages_new=scan_result.messages_new,
                conversations_scanned=scan_result.conversations_scanned,
            )
        except ServiceError as exc:
            return self._quick_qq_result(
                detected_title_len=detected_title_len, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - 不向 UI 抛裸异常/不泄漏标题
            return self._quick_qq_result(
                detected_title_len=detected_title_len,
                error=f"一键读取失败（{type(exc).__name__}），请稍后重试",
            )

    # -- QQ 会话选择器（P0-25：只列出可见会话标题，用户勾选后才读）----------------

    @staticmethod
    def _sanitize_selected_titles(titles: Any) -> list[str]:
        """去空白/去重/限制最多 20 个标题；忽略非字符串。"""
        if not isinstance(titles, (list, tuple)):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in titles:
            if not isinstance(item, str):
                continue
            title = item.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            result.append(title)
            if len(result) >= 20:
                break
        return result

    @staticmethod
    def _sanitize_visible_sessions(value: Any) -> list[dict[str, Any]]:
        """清洗 worker 返回的可见会话：标题去重、unread 只接受非负整数。"""
        if not isinstance(value, (list, tuple)):
            return []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, Mapping):
                continue
            raw_title = item.get("title")
            if not isinstance(raw_title, str):
                continue
            title = raw_title.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            unread = item.get("unread")
            if isinstance(unread, bool) or not isinstance(unread, int) or unread < 0:
                unread = None
            result.append({"title": title, "unread": unread})
            if len(result) >= 30:
                break
        return result

    def _run_qq_worker_json(
        self, task: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """调用 QQ worker 并返回 dict；异常交给调用方映射为结构化错误。"""
        from .collector_worker import CollectorWorkerError, run_worker_task

        if self._qq_worker_runner is not None:
            data = self._qq_worker_runner(dict(task))
        else:
            data = run_worker_task(
                task,
                command=self._qq_worker_command,
                timeout=timeout,
                output_dir=self._qq_worker_output_dir,
                env=self._qq_worker_env,
            )
        if not isinstance(data, dict):
            raise CollectorWorkerError("worker 返回格式错误")
        return data

    def _qq_worker_timeout_for(self, mode: str) -> float:
        from .collector_worker import DEFAULT_TIMEOUTS as WORKER_TIMEOUTS

        if self._qq_worker_timeout:
            return float(self._qq_worker_timeout)
        return float(WORKER_TIMEOUTS.get(mode, WORKER_TIMEOUTS["fetch_messages"]))

    def list_qq_visible_sessions(
        self, *, user_confirmed: bool = False
    ) -> dict[str, Any]:
        """列出 QQ 主面板“当前可见会话”的标题/未读数（不打开会话、不读正文）。

        标题只返回给 UI 供用户勾选；**不写日志/证据，审计只记录数量**。
        """
        self._ensure_open()
        if not user_confirmed:
            return {
                "ok": False,
                "sessions": [],
                "error": "需要用户确认只读取会话列表",
            }
        try:
            self.grant_consent(
                ConsentScope.QQ_READ,
                confirmation_text="列出 QQ 会话以选择白名单（不读取聊天内容）",
            )
        except ServiceError as exc:
            return {"ok": False, "sessions": [], "error": str(exc)}

        from .collector_worker import CollectorWorkerError

        try:
            data = self._run_qq_worker_json(
                {"version": 1, "mode": "list_visible_sessions"},
                timeout=self._qq_worker_timeout_for("list_visible_sessions"),
            )
        except CollectorWorkerError as exc:
            message = str(exc)
            if "no_visible_sessions" in message:
                return {
                    "ok": False,
                    "sessions": [],
                    "error": "没有找到可见的 QQ 会话列表，请先打开 QQ 主面板",
                }
            return {
                "ok": False,
                "sessions": [],
                "error": f"读取 QQ 会话列表失败（{redact_text(message, max_len=200)}）",
            }
        except Exception as exc:  # noqa: BLE001 - 结构化错误，不崩 UI
            return {
                "ok": False,
                "sessions": [],
                "error": f"读取 QQ 会话列表失败（{type(exc).__name__}）",
            }

        if data.get("ok") is not True:
            error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
            if str(error.get("code") or "") == "no_visible_sessions":
                return {
                    "ok": False,
                    "sessions": [],
                    "error": "没有找到可见的 QQ 会话列表，请先打开 QQ 主面板",
                }
            return {
                "ok": False,
                "sessions": [],
                "error": "读取 QQ 会话列表失败（{}）".format(
                    redact_text(str(error.get("message") or "未知原因"), max_len=200)
                ),
            }

        sessions = self._sanitize_visible_sessions(data.get("sessions"))
        if not sessions:
            return {
                "ok": False,
                "sessions": [],
                "error": "没有找到可见的 QQ 会话列表，请先打开 QQ 主面板",
            }
        # 审计只记录数量，不记录任何标题
        self._audit.log("qq_sessions_listed", detail={"session_count": len(sessions)})
        return {"ok": True, "sessions": sessions, "error": None}

    def apply_qq_session_selection(self, titles: list[str]) -> dict[str, Any]:
        """把用户勾选的会话保存为该 QQ 账号白名单，并只启用选中的会话。

        不自动扫描（由用户再点“立即读取一次”）；标题不进入审计/日志。
        """
        self._ensure_open()
        result: dict[str, Any] = {
            "ok": False,
            "account_id": None,
            "selected_count": 0,
            "discovered_count": 0,
            "enabled_count": 0,
            "error": None,
        }
        selected = self._sanitize_selected_titles(titles)
        result["selected_count"] = len(selected)
        if not selected:
            result["error"] = "没有选择要读取的 QQ 会话"
            return result

        try:
            self.grant_consent(
                ConsentScope.QQ_READ,
                confirmation_text="选择 QQ 会话白名单（只读取所选会话）",
            )
            self.update_settings(qq_read_experimental=True, collection_paused=False)

            qq_accounts = [
                account
                for account in self._storage.list_accounts()
                if account.kind is AccountKind.QQ
            ]
            if qq_accounts:
                account = next(
                    (item for item in qq_accounts if item.enabled), qq_accounts[0]
                )
                if not account.enabled:
                    self.set_account_enabled(account.id, True)
                self._set_allowlist(account.id, selected)
            else:
                account = self.add_qq_account("我的 QQ", selected)

            # 刷新生成/更新会话（失败也继续：仅对已发现会话调整 enabled）
            try:
                self.refresh_conversations(account.id)
            except Exception:  # noqa: BLE001 - 刷新失败不阻塞保存选择
                pass

            selected_set = set(selected)
            conversations = self._storage.list_conversations(account.id)
            enabled_count = 0
            for conversation in conversations:
                matched = (
                    conversation.title in selected_set
                    or conversation.external_key in selected_set
                )
                self._storage.update_conversation(conversation.id, enabled=matched)
                enabled_count += int(matched)

            result.update(
                {
                    "ok": True,
                    "account_id": account.id,
                    "selected_count": len(selected),
                    "discovered_count": len(conversations),
                    "enabled_count": enabled_count,
                }
            )
            self._audit.log(
                "qq_session_selection_applied",
                target_kind="account",
                target_id=account.id,
                detail={
                    "selected_count": len(selected),
                    "discovered_count": len(conversations),
                    "enabled_count": enabled_count,
                },
            )
            return result
        except ServiceError as exc:
            result["error"] = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 - 不抛裸异常/不泄漏标题
            result["error"] = (
                f"应用 QQ 会话选择失败（{type(exc).__name__}），请稍后重试"
            )
            return result

    # -- 邮箱收件箱（GUI E2E 核心路径；真实 IMAP 邮件，不按“今天”过滤）--------

    def list_mail_accounts(self) -> list[Account]:
        self._ensure_open()
        return [
            account
            for account in self._storage.list_accounts()
            if account.kind is AccountKind.EMAIL
        ]

    def _resolve_mail_accounts(self, account_id: str | None) -> list[Account]:
        """返回目标邮箱账号；指定 id 时校验存在且必须是 EMAIL 账号。"""
        if account_id is None:
            return self.list_mail_accounts()
        account = self._storage.get_account(account_id)
        if account is None:
            raise NotFoundError(f"账号不存在：{account_id}")
        if account.kind is not AccountKind.EMAIL:
            raise ServiceError("该账号不是邮箱账号；收件箱只支持邮箱（EMAIL）账号")
        return [account]

    @staticmethod
    def _mail_scope_ids(
        accounts: list[Account], *, demo: bool
    ) -> list[str]:
        return [
            account.id
            for account in accounts
            if is_demo_account_id(account.id) is demo
        ]

    def _latest_mail_scan_run(self, accounts: list[Account]) -> CollectionResult | None:
        latest: CollectionResult | None = None
        latest_ts = float("-inf")
        for account in accounts:
            run = self._storage.last_scan_run(account.id)
            if run is None:
                continue
            moment = run.finished_at or run.started_at
            ts = moment.timestamp() if moment is not None else 0.0
            if latest is None or ts >= latest_ts:
                latest, latest_ts = run, ts
        return latest

    def mail_inbox_status(self, account_id: str | None = None) -> dict[str, Any]:
        self._ensure_open()
        accounts = self._resolve_mail_accounts(account_id)
        real_accounts = [
            account for account in accounts if not is_demo_account_id(account.id)
        ]
        demo_accounts = [
            account for account in accounts if is_demo_account_id(account.id)
        ]
        kinds = [ConversationKind.MAIL]
        real_ids = [account.id for account in real_accounts]
        demo_ids = [account.id for account in demo_accounts]
        message_count = (
            self._storage.count_messages_for_accounts(real_ids, kinds=kinds)
            if real_ids
            else 0
        )
        demo_count = (
            self._storage.count_messages_for_accounts(demo_ids, kinds=kinds)
            if demo_ids
            else 0
        )
        credential_checks = [
            self.has_mail_credential(account.identifier)
            for account in real_accounts
            if account.identifier
        ]
        has_credential = bool(real_accounts) and bool(credential_checks) and all(
            credential_checks
        )
        if account_id is None:
            enabled = bool(accounts) and any(account.enabled for account in accounts)
        else:
            enabled = bool(accounts) and accounts[0].enabled
        last_run = self._latest_mail_scan_run(real_accounts)
        settings = self._settings_store.load()
        consent = self._consent.is_granted(ConsentScope.MAIL_READ)
        email = ""
        for candidate in [*real_accounts, *demo_accounts]:
            if candidate.identifier:
                email = candidate.identifier
                break
        return {
            # 固定 10 键（task-10/P0-14 契约）
            "configured": bool(accounts),
            "enabled": enabled,
            "has_credential": has_credential,
            "consent": consent,
            "paused": bool(settings.collection_paused),
            "message_count": message_count,
            "demo_count": demo_count,
            "last_sync_at": (
                (last_run.finished_at or last_run.started_at) if last_run else None
            ),
            "last_status": last_run.status.value if last_run else "",
            "last_error": last_run.error if last_run else None,
            # UI 辅助字段（app-ui 收件箱页使用；不属于固定 10 键）
            "account_id": account_id,
            "account_count": len(accounts),
            "account_ids": [account.id for account in accounts],
            "demo_active": self.is_demo_active(),
            "email": email,
            "credential_saved": has_credential,
            "mail_read_consent": consent,
        }

    def sync_mail(
        self,
        account_id: str | None = None,
        *,
        user_confirmed: bool = False,
    ) -> list[CollectionResult]:
        """只读同步邮箱（真实 IMAP），绝不触碰 QQ。

        - 只处理 EMAIL 账号；演示账号/停用账号/未同意/暂停都返回结构化
          ``CollectionResult(BLOCKED/PAUSED, error=明确原因)``，不静默成功、不抛未处理异常；
        - 正常时走与 run_scan 相同的 pipeline（首次全量/增量 UID、去重、分类、待办复用）。
        """
        self._ensure_open()
        accounts = self._resolve_mail_accounts(account_id)
        results: list[CollectionResult] = []
        if not accounts:
            return results

        settings = self._settings_store.load()
        paused = bool(settings.collection_paused)
        consent = self._consent.is_granted(ConsentScope.MAIL_READ)
        real_targets: list[Account] = []
        for account in accounts:
            if is_demo_account_id(account.id):
                # 聚合同步时静默跳过演示账号（避免 UI 把演示账号当成“同步失败”）；
                # 明确指定演示账号时才返回可操作的结构化说明。
                if account_id is not None:
                    results.append(
                        self._blocked_result(
                            account,
                            ScanStatus.BLOCKED,
                            "演示账号（合成数据）不执行真实邮箱同步；请添加真实 QQ 邮箱账号",
                        )
                    )
                continue
            if not account.enabled:
                results.append(
                    self._blocked_result(account, ScanStatus.BLOCKED, "账号已停用")
                )
                continue
            if paused:
                results.append(
                    self._blocked_result(
                        account, ScanStatus.PAUSED, "采集已暂停：请先恢复采集再同步邮件"
                    )
                )
                continue
            if not consent:
                blocked = self._blocked_result(
                    account,
                    ScanStatus.BLOCKED,
                    "未授予读取邮箱同意：请先在设置中授权读取 QQ 邮箱再同步",
                )
                self._storage.insert_scan_run(blocked, trigger="mail_sync")
                results.append(blocked)
                continue
            real_targets.append(account)

        if real_targets:
            self._scanning = True
            try:
                results.extend(
                    self._build_pipeline().run(
                        [account.id for account in real_targets],
                        trigger="mail_sync",
                        foreground_ack=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 兜底：同步异常不能崩 UI
                safe_message = safe_error(exc)
                self._last_scan_error = safe_message
                for account in real_targets:
                    failed = self._failed_result(account.id, safe_message)
                    self._storage.insert_scan_run(failed, trigger="mail_sync")
                    results.append(failed)
            finally:
                self._scanning = False

        self._storage.clear_summaries()
        self._audit.log(
            "mail_sync_finished",
            detail={
                "accounts": len(accounts),
                "synced": len(real_targets),
                "results": len(results),
                "user_confirmed": bool(user_confirmed),
                "paused": paused,
                "consent": consent,
            },
        )
        return results

    def list_inbox_messages(
        self,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[Message]:
        """收件箱：邮箱账号的真实邮件（含历史日期），按时间倒序；演示消息 is_demo=True。"""
        self._ensure_open()
        accounts = self._resolve_mail_accounts(account_id)
        account_ids = [account.id for account in accounts]
        if not account_ids:
            return []
        safe_limit = max(1, min(1000, int(limit)))
        return self._storage.list_messages_for_accounts(
            account_ids, limit=safe_limit, kinds=[ConversationKind.MAIL]
        )

    def generate_latest_mail_summary(self) -> DailySummary | None:
        """生成「最近有邮件的那一天」的摘要；没有邮件返回 None（历史邮件也能看摘要）。"""
        self._ensure_open()
        accounts = self.list_mail_accounts()
        real_ids = self._mail_scope_ids(accounts, demo=False)
        demo_ids = self._mail_scope_ids(accounts, demo=True)
        kinds = [ConversationKind.MAIL]
        day: date | None = None
        if real_ids:
            day = self._storage.latest_message_day(real_ids, kinds=kinds)
        if day is None and demo_ids:
            day = self._storage.latest_message_day(demo_ids, kinds=kinds)
        if day is None:
            return None
        day_messages = self._storage.list_messages(day=day, limit=1_000_000)
        is_demo = any(message.is_demo for message in day_messages)
        summary = _generate_summary(
            self._storage, day=day, is_demo=is_demo, clock=self._clock
        )
        self._audit.log(
            "mail_summary_generated",
            detail={
                "day": day.isoformat(),
                "total": summary.counts.get("total", 0),
                "is_demo": is_demo,
            },
        )
        return summary

    # -- 摘要 ---------------------------------------------------------------

    def generate_daily_summary(self, day: date | None = None) -> DailySummary:
        self._ensure_open()
        demo_active = self.is_demo_active()
        summary = _generate_summary(
            self._storage,
            day=day or self._now().date(),
            is_demo=demo_active,
            clock=self._clock,
        )
        self._audit.log(
            "summary_generated",
            detail={
                "day": summary.day.isoformat(),
                "total": summary.counts.get("total", 0),
                "todos": len(summary.todos),
                "is_demo": demo_active,
            },
        )
        return summary

    def get_cached_summary(self, day: date | None = None) -> DailySummary | None:
        self._ensure_open()
        return _get_cached_summary(self._storage, day or self._now().date())

    # -- 消息 / 来源 --------------------------------------------------------

    def list_messages(
        self,
        day: date | None = None,
        category: Category | None = None,
        limit: int = 200,
        work_status: MessageWorkStatus | None = None,
    ) -> list[Message]:
        self._ensure_open()
        if category is not None and not isinstance(category, Category):
            raise ServiceError("分类取值不合法")
        status = self._coerce_work_status(work_status, allow_none=True)
        return self._storage.list_messages(
            day=day,
            category=category,
            limit=max(1, int(limit)),
            work_status=status,
        )

    @staticmethod
    def _coerce_work_status(
        status: MessageWorkStatus | str | None, *, allow_none: bool = False
    ) -> MessageWorkStatus | None:
        if status is None:
            if allow_none:
                return None
            raise ServiceError("消息处理状态不合法")
        if isinstance(status, MessageWorkStatus):
            return status
        try:
            return MessageWorkStatus(str(status))
        except ValueError:
            raise ServiceError("消息处理状态不合法") from None

    def set_message_work_status(
        self, message_id: str, status: MessageWorkStatus
    ) -> None:
        """设置单条消息的处理状态（本地用户手动维护，不影响采集/规则语义）。"""
        self._ensure_open()
        normalized = self._coerce_work_status(status)
        assert normalized is not None
        if not isinstance(message_id, str) or not message_id.strip():
            raise NotFoundError(f"消息不存在：{message_id!r}")
        if self._storage.get_message(message_id) is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        if not self._storage.update_message_work_status(message_id, normalized):
            raise NotFoundError(f"消息不存在：{message_id}")
        # 摘要缓存里可能保存了旧的 work_status，清掉让 UI 下次取到最新状态。
        self._storage.clear_summaries()
        self._audit.log(
            "message_work_status_changed",
            target_kind="message",
            target_id=message_id,
            detail={"status": normalized.value, "message_id": message_id},
        )

    def set_message_category(self, message_id: str, category: Category) -> None:
        """手动调整消息的本地重要程度（只改本地，不写回 QQ/邮箱）。"""
        self._ensure_open()
        if isinstance(category, Category):
            normalized = category
        else:
            try:
                normalized = Category(str(category))
            except ValueError:
                raise ServiceError("消息分类取值不合法") from None
        if not isinstance(message_id, str) or not message_id.strip():
            raise NotFoundError(f"消息不存在：{message_id!r}")
        if self._storage.get_message(message_id) is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        if not self._storage.update_message_category(message_id, normalized):
            raise NotFoundError(f"消息不存在：{message_id}")
        self._storage.clear_summaries()
        self._audit.log(
            "message_category_changed",
            target_kind="message",
            target_id=message_id,
            detail={"category": normalized.value, "message_id": message_id},
        )

    def message_work_status_counts(self) -> dict[str, int]:
        """全部本地消息按处理状态计数（四种状态固定返回，无数据为 0）。"""
        self._ensure_open()
        return self._storage.count_messages_by_work_status()

    def get_source_detail(self, message_id: str) -> SourceDetail:
        self._ensure_open()
        row = self._storage.get_message_row(message_id)
        if row is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        account = self._storage.get_account(row["account_id"], include_deleted=True)
        if account is not None:
            account_kind = account.kind
        else:
            account_kind = (
                AccountKind.EMAIL
                if row["conversation_kind"] == ConversationKind.MAIL.value
                else AccountKind.QQ
            )
        if row["account_id"] in DEMO_ACCOUNT_IDS:
            origin_label = "演示数据（合成内容，非真实消息）"
        elif account_kind is AccountKind.IMPORT:
            origin_label = "用户导入的聊天文本（实验性，本地存储；非自动读取 QQ）"
        elif account_kind == AccountKind.QQ:
            origin_label = "QQ 聊天窗口 · 只读可见消息"
        else:
            origin_label = "QQ 邮箱 · IMAP 只读"
        return SourceDetail(
            message_id=row["id"],
            account_kind=account_kind,
            conversation_title=row["conversation_title"],
            sender=row["sender"],
            raw_time_text=row["raw_time_text"],
            collected_at=self._parse(row["collected_at"]) or self._now(),
            timestamp_source=self._timestamp_source(row["timestamp_source"]),
            external_id=row["external_id"],
            locator=row["source_locator"],
            origin_label=origin_label,
        )

    # -- 待办 ---------------------------------------------------------------

    def list_todos(self, day: date | None = None, include_done: bool = False) -> list[TodoItem]:
        self._ensure_open()
        return self._storage.list_todos(day=day, include_done=bool(include_done))

    def update_todo_status(self, todo_id: str, status: TodoStatus) -> None:
        self._ensure_open()
        if not isinstance(status, TodoStatus):
            raise ServiceError("待办状态取值不合法")
        if not self._storage.update_todo_status(todo_id, status):
            raise NotFoundError(f"待办不存在：{todo_id}")
        self._audit.log(
            "todo_status_changed",
            target_kind="todo",
            target_id=todo_id,
            detail={"status": status.value},
        )

    # -- 草稿 ---------------------------------------------------------------

    def generate_drafts(
        self,
        message_id: str,
        styles: list[str] | None = None,
        *,
        use_external: bool = False,
    ) -> list[ReplyDraft]:
        self._ensure_open()
        message = self._storage.get_message(message_id)
        if message is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        style_list = [str(item).strip() for item in (styles or []) if str(item).strip()]
        if not style_list:
            style_list = list(DEFAULT_DRAFT_STYLES)

        if use_external:
            # 门控：未授权时在创建任何外部客户端之前就拒绝
            self._consent.require(ConsentScope.EXTERNAL_AI, action="生成外部 AI 回复草稿")
            # 负向路径：先明确区分「未保存 Key」与「凭据管理器不可用」，
            # 避免用户看到含糊的“适配器不可用”或误以为调用成功。
            _secret, key_error = self._read_external_ai_key_or_error()
            if key_error:
                raise ServiceError(key_error)
            provider = self._make_external_provider()
            if provider is None:
                raise ServiceError(
                    "外部 AI 适配器不可用：请先在设置中填写服务地址与模型，"
                    "并确认 intel 模块已提供外部 AI 客户端"
                )
            drafts = self._collect_drafts(provider, message, style_list, external=True)
            audit_action = "drafts_generated_external"
        else:
            provider = self._get_draft_provider()
            drafts = self._collect_drafts(provider, message, style_list, external=False)
            audit_action = "drafts_generated_local"

        if not drafts:
            raise ServiceError("草稿生成失败：没有生成任何内容")
        self._storage.insert_drafts(drafts)
        self._audit.log(
            audit_action,
            target_kind="message",
            target_id=message.id,
            detail={"count": len(drafts), "external": bool(use_external)},
        )
        return drafts

    def save_draft_edit(self, draft_id: str, text: str) -> None:
        self._ensure_open()
        if self._storage.get_draft(draft_id) is None:
            raise NotFoundError(f"草稿不存在：{draft_id}")
        content = str(text or "")
        self._storage.update_draft_edit(draft_id, content)
        self._audit.log(
            "draft_edited",
            target_kind="draft",
            target_id=draft_id,
            detail={"length": len(content)},
        )

    def list_drafts(self, message_id: str) -> list[ReplyDraft]:
        self._ensure_open()
        return self._storage.list_drafts(message_id)

    # -- 语义分析（本地规则默认；外部 AI 必须显式授权且失败回退本地）----------

    _SEMANTIC_CONVERSATION_LIMIT = 20
    _SEMANTIC_DAILY_LIMIT = 50
    _SEMANTIC_DRAFT_CONTEXT_LIMIT = 8
    _SEMANTIC_TEXT_LIMIT = 300

    @staticmethod
    def _semantic_usage(
        prompt_tokens: int | None = 0,
        completion_tokens: int | None = 0,
        estimated_cost: float | None = 0.0,
    ) -> dict[str, Any]:
        prompt = int(prompt_tokens or 0)
        completion = int(completion_tokens or 0)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "estimated_cost": estimated_cost,
        }

    @staticmethod
    def _semantic_kind(text: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return "chitchat"
        if re.search(r"[?？]|吗|怎么|什么|为什么|哪|是否|能不能", normalized):
            return "question"
        if re.search(
            r"截止|ddl|deadline|今天|明天|后天|周[一二三四五六日]|下周|月底|之前|尽快",
            normalized,
            re.IGNORECASE,
        ):
            return "deadline"
        if re.search(r"我会|稍后|马上|立刻|保证|尽量", normalized):
            return "promise"
        if re.search(r"还没|待定|等确认|看情况|再确认|不确定|未定|回头", normalized):
            return "unresolved"
        if re.search(
            r"请|麻烦|帮我|需要你|务必|提交|发我|回复|确认|安排|处理", normalized
        ):
            return "request"
        return "chitchat"

    @staticmethod
    def _semantic_due_hint(text: str) -> str | None:
        match = re.search(
            r"(今天|明天|后天|下周[一二三四五六日]?|周[一二三四五六日]|月底"
            r"|截止[^，。；;\n]{0,12}|ddl[^，。；;\n]{0,12}|deadline[^，。；;\n]{0,12})",
            text or "",
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1).strip() or None

    def _semantic_item(self, message: Message) -> dict[str, Any] | None:
        text = (message.text or "").strip()
        if not text:
            return None
        kind = self._semantic_kind(text)
        item: dict[str, Any] = {
            "kind": kind,
            "text": text[: self._SEMANTIC_TEXT_LIMIT],
            "source_message_ids": [message.id],
            "uncertain": True,
        }
        due_hint = self._semantic_due_hint(text)
        if due_hint:
            item["due_hint"] = due_hint
        return item

    def _semantic_items(self, messages: Sequence[Message]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            item = self._semantic_item(message)
            if item is not None:
                items.append(item)
        return items

    def _normalize_semantic_items(
        self, value: Any, allowed_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple)):
            return []
        include_ids = allowed_ids is not None
        allowed = set(allowed_ids or ())
        items: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            raw_ids = raw.get("source_message_ids") or ()
            ids = [
                str(item)
                for item in raw_ids
                if isinstance(item, str)
                and (item in allowed if include_ids else True)
            ] if isinstance(raw_ids, (list, tuple)) else []
            item: dict[str, Any] = {
                "kind": str(raw.get("kind") or "chitchat"),
                "text": text[: self._SEMANTIC_TEXT_LIMIT],
                "source_message_ids": ids,
                "uncertain": bool(raw.get("uncertain", True)),
            }
            due_hint = raw.get("due_hint")
            if due_hint:
                item["due_hint"] = str(due_hint)[:80]
            items.append(item)
        return items

    def _semantic_conversation_result(
        self,
        data: Mapping[str, Any],
        *,
        provider: str,
        allowed_ids: Sequence[str],
        default_title: str = "",
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        items = self._normalize_semantic_items(data.get("items"), allowed_ids)
        base_kind_items = {
            "questions": [item for item in items if item["kind"] == "question"],
            "requests": [item for item in items if item["kind"] == "request"],
            "deadlines": [item for item in items if item["kind"] == "deadline"],
            "promises": [item for item in items if item["kind"] == "promise"],
            # 与 intel.semantic 对齐：unresolved 仅指 kind=="unresolved"（不再混入问句/请求）
            "unresolved": [item for item in items if item["kind"] == "unresolved"],
            "chitchat": [item for item in items if item["kind"] == "chitchat"],
        }
        result: dict[str, Any] = {
            "ok": True,
            "provider": provider,
            "conversation_title": str(
                data.get("conversation_title") or default_title or ""
            ),
            "overview": str(data.get("overview") or ""),
            "items": items,
            **base_kind_items,
            "uncertainty_notes": [
                str(item)
                for item in (data.get("uncertainty_notes") or [])
                if str(item).strip()
            ],
            "usage": usage or self._semantic_usage(),
            "error": error,
        }
        for key in ("questions", "requests", "deadlines", "promises", "chitchat"):
            if isinstance(data.get(key), (list, tuple)):
                normalized = self._normalize_semantic_items(data.get(key), allowed_ids)
                if normalized:
                    result[key] = normalized
        return result

    def _semantic_daily_result(
        self,
        data: Mapping[str, Any],
        *,
        provider: str,
        day: date,
        allowed_ids: Sequence[str],
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        raw_sections = data.get("sections") if isinstance(data.get("sections"), Mapping) else {}
        sections: dict[str, list[dict[str, Any]]] = {}
        for name in ("must_today", "soon", "batch_reply", "fyi"):
            sections[name] = self._normalize_semantic_items(
                raw_sections.get(name) or data.get(name), allowed_ids
            )
        citations = [
            str(item)
            for item in (data.get("citations") or [])
            if isinstance(item, str) and item in set(allowed_ids)
        ]
        if not citations:
            seen: list[str] = []
            for items in sections.values():
                for item in items:
                    for source_id in item["source_message_ids"]:
                        if source_id not in seen:
                            seen.append(source_id)
            citations = seen
        caveats = [str(item) for item in (data.get("caveats") or []) if str(item).strip()]
        return {
            "ok": True,
            "provider": provider,
            "day": str(data.get("day") or day.isoformat()),
            "sections": sections,
            "citations": citations,
            "caveats": caveats,
            "usage": usage or self._semantic_usage(),
            "error": error,
        }

    def _semantic_draft_result(
        self,
        text: str,
        *,
        provider: str,
        message: Message,
        style: str,
        context_ids: Sequence[str],
        uncertainty_notes: Sequence[str] = (),
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": bool(str(text or "").strip()),
            "provider": provider,
            "message_id": message.id,
            "style": style,
            "text": str(text or "").strip(),
            "context_message_ids": [str(item) for item in context_ids],
            "uncertainty_notes": [str(item) for item in uncertainty_notes],
            "usage": usage or self._semantic_usage(),
            "error": error,
        }

    def _local_conversation_semantic(
        self, conversation: Conversation, messages: Sequence[Message]
    ) -> dict[str, Any]:
        allowed_ids = [message.id for message in messages]
        data = self._intel_local_semantic(
            "conversation", messages=list(messages), conversation=conversation
        )
        usage = self._semantic_usage()
        if isinstance(data, Mapping):
            return self._semantic_conversation_result(
                data,
                provider="local_rules",
                allowed_ids=allowed_ids,
                default_title=conversation.title,
                usage=usage,
            )
        items = self._semantic_items(messages)
        counts = {
            kind: sum(1 for item in items if item["kind"] == kind)
            for kind in ("question", "request", "deadline", "promise", "chitchat")
        }
        overview = (
            f"本地规则分析：共 {len(messages)} 条消息，其中问题 {counts['question']}、"
            f"请求 {counts['request']}、时间节点 {counts['deadline']}、"
            f"承诺 {counts['promise']}、闲聊 {counts['chitchat']}。"
        )
        result = self._semantic_conversation_result(
            {
                "conversation_title": conversation.title,
                "overview": overview,
                "items": items,
                "uncertainty_notes": [
                    "本地规则生成，未上传任何内容；可能存在漏判/误判，重要事项请人工确认。"
                ],
            },
            provider="local_rules",
            allowed_ids=allowed_ids,
            default_title=conversation.title,
            usage=usage,
        )
        return result

    def _local_daily_semantic(
        self, day: date, messages: Sequence[Message]
    ) -> dict[str, Any]:
        allowed_ids = [message.id for message in messages]
        data = self._intel_local_semantic(
            "daily", messages=list(messages), day=day
        )
        usage = self._semantic_usage()
        if isinstance(data, Mapping):
            return self._semantic_daily_result(
                data, provider="local_rules", day=day, allowed_ids=allowed_ids, usage=usage
            )
        items = self._semantic_items(messages)
        by_message_id = {
            message.id: item
            for message, item in zip(
                [message for message in messages if self._semantic_item(message) is not None],
                items,
            )
        }
        categories = {message.id: message.category for message in messages}
        must_today = [
            item
            for message_id, item in by_message_id.items()
            if categories.get(message_id) is Category.IMPORTANT
        ]
        soon = [item for item in items if item.get("due_hint")]
        batch_reply = [
            item for item in items if item["kind"] in {"request", "question"}
        ]
        fyi = [item for item in items if item not in must_today and item not in soon]
        citations: list[str] = []
        for section in (must_today, soon, batch_reply, fyi):
            for item in section:
                for source_id in item["source_message_ids"]:
                    if source_id not in citations:
                        citations.append(source_id)
        return self._semantic_daily_result(
            {
                "day": day.isoformat(),
                "sections": {
                    "must_today": must_today,
                    "soon": soon,
                    "batch_reply": batch_reply,
                    "fyi": fyi,
                },
                "citations": citations,
                "caveats": [
                    "本地规则生成的每日简报，未上传任何内容；存在漏判/误判可能，请人工核对。"
                ],
            },
            provider="local_rules",
            day=day,
            allowed_ids=allowed_ids,
            usage=usage,
        )

    @staticmethod
    def _local_draft_text(message: Message, style: str) -> str:
        text = (message.text or "").strip()
        if any(keyword in text for keyword in ("?", "？", "请问", "确认", "回复", "答复")):
            topic = "需要确认的事项"
        elif any(keyword in text for keyword in ("截止", "今天", "明天", "之前", "尽快")):
            topic = "时间节点"
        elif any(keyword in text for keyword in ("会议", "评审", "开会", "日程")):
            topic = "会议安排"
        else:
            topic = "你提到的事情"
        templates = {
            "友好": f"你好，收到你的消息啦，关于{topic}我稍后回复你，谢谢！",
            "正式": f"您好，已收到您关于{topic}的消息，我会尽快处理并回复您，感谢您的耐心等待。",
            "简洁": f"收到，关于{topic}我稍后回复你。",
            "礼貌": f"您好，已收到关于{topic}的消息，我会尽快回复您，谢谢。",
            "稍后回复": f"收到，关于{topic}我先确认一下，稍后回复你。",
            "确认收到": "收到，已了解，谢谢！",
        }
        return templates.get(style, f"收到你的消息，关于{topic}我稍后回复你。")

    def _local_draft_semantic(
        self, message: Message, context: Sequence[Message], style: str
    ) -> dict[str, Any]:
        data = self._intel_local_semantic(
            "draft",
            messages=list(context),
            message=message,
            context=list(context),
            style=style,
        )
        context_ids = [item.id for item in context]
        if isinstance(data, Mapping):
            raw_context_ids = data.get("context_message_ids")
            if isinstance(raw_context_ids, (list, tuple)):
                context_ids = [str(item) for item in raw_context_ids if isinstance(item, str)]
            text = str(data.get("text") or "")
            if text.strip():
                return self._semantic_draft_result(
                    text,
                    provider="local_rules",
                    message=message,
                    style=str(data.get("style") or style),
                    context_ids=context_ids,
                    uncertainty_notes=(
                        data.get("uncertainty_notes")
                        or ["本地规则草稿，不会发送，请人工确认后使用。"]
                    ),
                    usage=self._semantic_usage(),
                )
        return self._semantic_draft_result(
            self._local_draft_text(message, style),
            provider="local_rules",
            message=message,
            style=style,
            context_ids=context_ids,
            uncertainty_notes=["本地模板草稿，不会发送；请人工确认后使用。"],
            usage=self._semantic_usage(),
        )

    def _intel_local_semantic(self, kind: str, **kwargs: Any) -> Mapping[str, Any] | None:
        """尝试调用 intel.semantic 的 local_* 函数；未就绪/接口不匹配返回 None。"""
        try:
            from ..intel import semantic as module  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - intel 未就绪时走核心层最小回退
            return None
        candidates = {
            "conversation": (
                "local_conversation_summary",
                "local_conversation_semantic",
                "local_conversation",
                "local_conversation_analysis",
            ),
            "daily": ("local_daily_brief", "local_daily", "local_daily_semantic"),
            "draft": (
                "local_semantic_draft",
                "local_draft_semantic",
                "local_draft",
            ),
        }[kind]
        attempts: list[dict[str, Any]] = [{}]
        if kind == "conversation":
            attempts = [
                {"messages": kwargs.get("messages", []), "conversation_title": getattr(kwargs.get("conversation"), "title", "")},
                {"messages": kwargs.get("messages", [])},
            ]
        elif kind == "daily":
            attempts = [
                {"messages": kwargs.get("messages", []), "day": kwargs.get("day")},
                {"messages": kwargs.get("messages", [])},
            ]
        elif kind == "draft":
            attempts = [
                {
                    "message": kwargs.get("message"),
                    "context_messages": kwargs.get("context", []),
                    "style": kwargs.get("style", ""),
                },
                {"message": kwargs.get("message"), "style": kwargs.get("style", "")},
            ]
        for name in candidates:
            function = getattr(module, name, None)
            if not callable(function):
                continue
            for attempt in attempts:
                try:
                    result = function(**attempt)
                except TypeError:
                    continue
                except Exception:  # noqa: BLE001 - intel 内部异常时本地回退
                    return None
                if isinstance(result, Mapping):
                    return result
                return None
        return None

    def _external_semantic(
        self,
        kind: str,
        *,
        local_factory: Callable[[], dict[str, Any]],
        messages: Sequence[Message],
        context: Sequence[Message] = (),
        day: date | None = None,
        conversation: Conversation | None = None,
        message: Message | None = None,
        style: str = "",
    ) -> dict[str, Any]:
        """外部语义调用（intel.SemanticAnalyzer）；任何失败回退本地并返回明确 error。"""
        try:
            self._consent.require(ConsentScope.EXTERNAL_AI, action="生成语义分析")
        except ConsentRequired as exc:
            result = local_factory()
            result["error"] = f"未授权外部 AI，已回退本地规则：{exc}"
            return result
        _secret, key_error = self._read_external_ai_key_or_error()
        if key_error:
            result = local_factory()
            result["error"] = f"外部 AI 不可用，已回退本地规则：{key_error}"
            return result
        provider = self._make_external_provider()
        if provider is None:
            result = local_factory()
            result["error"] = (
                "外部 AI 适配器不可用（未配置服务地址/模型或 intel 未就绪），已回退本地规则。"
            )
            return result
        client = provider
        if not callable(getattr(client, "complete", None)):
            client = getattr(provider, "_client", None)
        if not callable(getattr(client, "complete", None)):
            result = local_factory()
            result["error"] = (
                "外部 AI 客户端未提供 complete 接口（intel 未就绪），已回退本地规则。"
            )
            return result
        try:
            from ..intel.semantic import SemanticAnalyzer  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 - intel 语义模块未就绪
            result = local_factory()
            result["error"] = "intel 语义分析模块不可用，已回退本地规则。"
            return result
        try:
            analyzer = SemanticAnalyzer(client.complete)
            if kind == "conversation":
                assert conversation is not None
                data = analyzer.analyze_conversation(
                    messages, conversation_title=conversation.title
                )
            elif kind == "daily":
                assert day is not None
                data = analyzer.daily_brief(messages, day)
            else:
                assert message is not None
                data = analyzer.draft(message, context, style=style)
        except Exception as exc:  # noqa: BLE001 - 兜底：仍回退本地
            result = local_factory()
            result["error"] = (
                f"外部 AI 调用失败（{safe_error(exc)}），已回退本地规则。"
            )
            return result
        if not isinstance(data, Mapping):
            result = local_factory()
            result["error"] = "外部 AI 返回格式不合法，已回退本地规则。"
            return result
        result = dict(data)
        if result.get("provider") == "external_llm":
            result["usage"] = self._semantic_usage_from(result, client)
        else:
            # intel.SemanticAnalyzer 已在内部回退本地规则；补契约字段，不假成功
            result["provider"] = "local_rules"
            result["usage"] = self._semantic_usage()
            if not result.get("error"):
                result["error"] = "外部 AI 结果无效，已回退本地规则。"
        return result

    def _semantic_usage_from(
        self, data: Mapping[str, Any], client: Any
    ) -> dict[str, Any]:
        raw = data.get("usage")
        if not isinstance(raw, Mapping):
            raw = getattr(client, "last_usage", None)
        if not isinstance(raw, Mapping):
            raw = {}
        prompt = raw.get("prompt_tokens")
        completion = raw.get("completion_tokens")
        total = raw.get("total_tokens")
        cost = data.get("estimated_cost")
        if cost is None:
            cost = raw.get("estimated_cost", raw.get("cost_usd"))
        prompt_tokens = int(prompt) if isinstance(prompt, (int, float)) else 0
        completion_tokens = int(completion) if isinstance(completion, (int, float)) else 0
        total_tokens = int(total) if isinstance(total, (int, float)) else None
        usage = self._semantic_usage(prompt_tokens, completion_tokens, cost)
        if total_tokens is not None:
            usage["total_tokens"] = total_tokens
        return usage

    def _audit_semantic(
        self,
        action: str,
        *,
        target_kind: str,
        target_id: str,
        result: Mapping[str, Any],
        count: int,
    ) -> None:
        self._audit.log(
            action,
            target_kind=target_kind,
            target_id=target_id,
            detail={
                "provider": str(result.get("provider") or ""),
                "count": int(count),
            },
        )

    def generate_conversation_semantic(
        self, conversation_id: str, *, use_external: bool = False
    ) -> dict[str, Any]:
        """生成会话语义分析（本地规则优先；外部 AI 需授权且失败回退本地）。"""
        self._ensure_open()
        conversation = self._storage.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        messages = list(
            reversed(
                self._storage.list_messages(
                    conversation_id=conversation_id,
                    limit=self._SEMANTIC_CONVERSATION_LIMIT,
                )
            )
        )
        allowed_ids = [message.id for message in messages]
        local = lambda: self._local_conversation_semantic(conversation, messages)
        if use_external:
            result = self._external_semantic(
                "conversation",
                local_factory=local,
                messages=messages,
                conversation=conversation,
            )
        else:
            result = local()
        self._audit_semantic(
            "semantic_conversation_generated",
            target_kind="conversation",
            target_id=conversation_id,
            result=result,
            count=len(result.get("items") or []),
        )
        return result

    def generate_daily_brief(
        self, *, day: date | None = None, use_external: bool = False
    ) -> dict[str, Any]:
        """生成每日语义简报（本地规则优先；外部 AI 需授权且失败回退本地）。"""
        self._ensure_open()
        if day is not None and not isinstance(day, date):
            raise ServiceError("日期格式不合法")
        target_day = day or now_local().date()
        messages = list(
            reversed(
                self._storage.list_messages(
                    day=target_day, limit=self._SEMANTIC_DAILY_LIMIT
                )
            )
        )
        allowed_ids = [message.id for message in messages]
        local = lambda: self._local_daily_semantic(target_day, messages)
        if use_external:
            result = self._external_semantic(
                "daily",
                local_factory=local,
                messages=messages,
                day=target_day,
            )
        else:
            result = local()
        sections = result.get("sections") or {}
        count = sum(len(items) for items in sections.values())
        self._audit_semantic(
            "semantic_daily_brief_generated",
            target_kind="day",
            target_id=target_day.isoformat(),
            result=result,
            count=count,
        )
        return result

    def generate_semantic_draft(
        self,
        message_id: str,
        *,
        style: str = "友好",
        use_external: bool = False,
    ) -> dict[str, Any]:
        """生成单条消息的语义回复草稿文本（绝不发送/绝不写入输入框）。"""
        self._ensure_open()
        message = self._storage.get_message(message_id)
        if message is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        normalized_style = str(style or "").strip()[:20] or "友好"
        context = list(
            reversed(
                self._storage.list_messages(
                    conversation_id=message.conversation_id,
                    limit=self._SEMANTIC_DRAFT_CONTEXT_LIMIT,
                )
            )
        )
        context_ids = [item.id for item in context]
        if message.id not in context_ids:
            context_ids.append(message.id)
        allowed_ids = context_ids
        local = lambda: self._local_draft_semantic(
            message, context, normalized_style
        )
        if use_external:
            result = self._external_semantic(
                "draft",
                local_factory=local,
                messages=context,
                context=context,
                message=message,
                style=normalized_style,
            )
        else:
            result = local()
        self._audit_semantic(
            "semantic_draft_generated",
            target_kind="message",
            target_id=message_id,
            result=result,
            count=1,
        )
        return result

    # -- 设置 / 权限 --------------------------------------------------------

    def get_settings(self) -> AppSettings:
        self._ensure_open()
        stored = self._settings_store.load()
        states = self._consent.states()
        return replace(
            stored,
            qq_read_consent=states[ConsentScope.QQ_READ],
            mail_read_consent=states[ConsentScope.MAIL_READ],
            external_ai_consent=states[ConsentScope.EXTERNAL_AI],
        )

    def update_settings(self, **changes: Any) -> AppSettings:
        self._ensure_open()
        known_fields = set(AppSettings.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(changes) - known_fields
        if unknown:
            raise ServiceError("未知设置项：" + "、".join(sorted(unknown)))
        consent_fields = {"qq_read_consent", "mail_read_consent", "external_ai_consent"}
        if consent_fields & set(changes):
            raise ServiceError(
                "同意状态不能通过 update_settings 修改，请调用 grant_consent / revoke_consent"
            )
        current = self._settings_store.load()
        updates: dict[str, Any] = {}
        for key, value in changes.items():
            expected = getattr(current, key)
            if isinstance(expected, bool):
                updates[key] = bool(value)
            elif isinstance(expected, int):
                updates[key] = int(value)
            elif isinstance(expected, str):
                updates[key] = str(value)
            else:
                updates[key] = value
        if "auto_scan_interval_minutes" in updates and updates["auto_scan_interval_minutes"] < 0:
            raise ServiceError("自动扫描间隔不能为负数")
        if "data_retention_days" in updates and updates["data_retention_days"] < 1:
            raise ServiceError("数据保留天数至少为 1 天")
        base_url = updates.get("external_ai_base_url", "")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ServiceError("外部 AI 服务地址必须以 http:// 或 https:// 开头")
        if "log_level" in updates:
            level = str(updates["log_level"]).upper()
            if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise ServiceError("日志级别取值不合法")
            updates["log_level"] = level
        try:
            saved = self._settings_store.save(replace(current, **updates))
        except ValueError as exc:
            raise ServiceError(str(exc)) from None
        self._audit.log("settings_updated", detail={"fields": sorted(updates)})
        return self.get_settings()

    def grant_consent(self, scope: ConsentScope, *, confirmation_text: str = "") -> None:
        self._ensure_open()
        record = self._consent.grant(scope, confirmation_text=confirmation_text)
        self._mirror_consent(record.scope, True)
        self._audit.log(
            "consent_granted",
            detail={"scope": record.scope.value},
        )

    def revoke_consent(self, scope: ConsentScope) -> None:
        self._ensure_open()
        record = self._consent.revoke(scope)
        self._mirror_consent(record.scope, False)
        self._audit.log("consent_revoked", detail={"scope": record.scope.value})

    def list_consent_history(self) -> list[dict[str, Any]]:
        self._ensure_open()
        return self._consent.history(200)

    # -- 邮箱凭据 -----------------------------------------------------------

    def set_mail_credential(self, email: str, auth_code: str) -> None:
        self._ensure_open()
        key = self._mail_key(email)
        secret = (auth_code or "").strip()
        if not secret:
            raise ServiceError("邮箱授权码不能为空")
        try:
            self._credentials.set_secret(key, secret, label="QQ邮箱授权码")
        except CredentialStoreError as exc:
            raise ServiceError(str(exc)) from None
        self._audit.log(
            "mail_credential_saved",
            detail={"email_hash": hash_text(key)},
        )

    def has_mail_credential(self, email: str) -> bool:
        self._ensure_open()
        try:
            key = self._mail_key(email)
        except ServiceError:
            return False
        try:
            return bool(self._credentials.has_secret(key))
        except CredentialStoreError:
            return False

    def delete_mail_credential(self, email: str) -> None:
        self._ensure_open()
        key = self._mail_key(email)
        try:
            deleted = bool(self._credentials.delete_secret(key))
        except CredentialStoreError as exc:
            raise ServiceError(str(exc)) from None
        self._audit.log(
            "mail_credential_deleted",
            detail={"email_hash": hash_text(key), "deleted": deleted},
        )

    def test_mail_credential(self, email: str) -> ConnectionCheck:
        self._ensure_open()
        try:
            key = self._mail_key(email)
        except ServiceError as exc:
            return ConnectionCheck(ok=False, detail=str(exc))
        try:
            saved_secret = self._credentials.get_secret(key)
        except CredentialStoreError as exc:
            return ConnectionCheck(
                ok=False, detail=f"系统凭据管理器不可用，无法读取邮箱授权码：{exc}"
            )
        if not saved_secret:
            return ConnectionCheck(ok=False, detail="尚未在本机保存该邮箱的授权码")

        normalized = (email or "").strip().lower()
        account = self._storage.find_account_by_identifier(AccountKind.EMAIL, normalized)
        if account is None:
            account = Account(
                id=f"probe-{uuid.uuid4().hex[:8]}",
                kind=AccountKind.EMAIL,
                display_name=normalized,
                enabled=True,
                created_at=self._now(),
                identifier=normalized,
            )
        source = self._make_source(account)
        if source is None:
            return ConnectionCheck(
                ok=False, detail="邮件采集器不可用（尚未接入只读 IMAP 采集器）"
            )
        try:
            return self._coerce_connection_check(source.test_connection())
        except Exception as exc:  # noqa: BLE001
            return ConnectionCheck(ok=False, detail=f"邮箱连接测试失败（{safe_error(exc)}）")
        finally:
            self._close_source(source)

    # -- 外部 AI ------------------------------------------------------------

    def test_external_ai(self) -> ConnectionCheck:
        self._ensure_open()
        try:
            self._consent.require(ConsentScope.EXTERNAL_AI, action="测试外部 AI 连通性")
        except ConsentRequired as exc:
            return ConnectionCheck(
                ok=False,
                detail=str(exc),
                discovered={"consent_required": True},
            )
        _secret, key_error = self._read_external_ai_key_or_error()
        if key_error:
            return ConnectionCheck(ok=False, detail=key_error)
        provider = self._make_external_provider()
        if provider is None:
            return ConnectionCheck(ok=False, detail="外部 AI 适配器不可用（intel 模块未就绪）")
        try:
            checker = getattr(provider, "test_connection", None)
            if callable(checker):
                return self._coerce_connection_check(checker(), success_detail="外部 AI 连接正常")
            return ConnectionCheck(ok=True, detail="外部 AI 适配器可用（未提供连通性自检）")
        except Exception as exc:  # noqa: BLE001
            return ConnectionCheck(
                ok=False, detail=f"外部 AI 连通性测试失败（{safe_error(exc)}）"
            )

    def has_external_ai_key(self) -> bool:
        self._ensure_open()
        try:
            return bool(self._credentials.has_secret(EXTERNAL_AI_KEY))
        except CredentialStoreError:
            return False

    def _read_external_ai_key_or_error(self) -> tuple[str | None, str | None]:
        """读取外部 AI Key，区分「未保存」与「凭据管理器不可用」。

        返回 ``(secret, None)`` 或 ``(None, 可展示中文错误)``；**不把异常抛给 UI**，
        调用方决定是转 ConnectionCheck 还是 ServiceError。
        """
        try:
            secret = self._credentials.get_secret(EXTERNAL_AI_KEY)
        except CredentialStoreError as exc:
            return None, f"系统凭据管理器不可用，无法读取外部 AI API Key：{exc}"
        if not secret:
            return (
                None,
                "尚未保存外部 AI API Key：请先在设置中保存 Key（只保存在 Windows 凭据管理器）。",
            )
        return str(secret), None

    def set_external_ai_key(self, api_key: str) -> None:
        self._ensure_open()
        secret = (api_key or "").strip()
        if not secret:
            raise ServiceError("API Key 不能为空")
        try:
            self._credentials.set_secret(EXTERNAL_AI_KEY, secret, label="外部 AI API Key")
        except CredentialStoreError as exc:
            raise ServiceError(str(exc)) from None
        self._audit.log("external_ai_key_saved", detail={"length": len(secret)})

    # -- 实验性：用户主动导入聊天文本（不联网、不读 QQ 数据库、不等于自动读取 QQ）------

    def import_chat_text(
        self,
        text: str,
        *,
        source_name: str = "",
        options: dict[str, Any] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """解析/导入用户主动提供的聊天文本（实验性；纯本地、不联网）。

        - ``dry_run=True``：只解析并返回脱敏摘要，不写数据库、不创建账号/会话；
        - ``dry_run=False``：写入本机 SQLite（自动创建/复用 IMPORT 账号与会话），
          复用现有 content_hash 去重、分类、待办提取路径；**绝不触发 QQ/IMAP 采集**；
        - 非法输入/解析失败都返回结构化 dict（``ok``/``error``/``warnings``），不抛裸异常；
          返回值不含消息正文原文（只含计数/脱敏警告/指纹）。
        """
        self._ensure_open()
        dry_run = bool(dry_run)
        try:
            from ..importing import EXPERIMENTAL_NOTICE, parse_chat_text
        except Exception:  # noqa: BLE001 - 冻结版可能未打包实验性模块
            return self._import_error_result(
                "实验性聊天文本导入功能不可用：当前版本未包含 importing 模块。",
                dry_run=dry_run,
            )
        if not isinstance(text, str):
            return self._import_error_result("导入内容必须是文本字符串。", dry_run=dry_run)
        try:
            import_options = self._build_import_options(options, source_name=source_name)
        except ServiceError as exc:
            return self._import_error_result(str(exc), dry_run=dry_run)
        try:
            result = parse_chat_text(text, options=import_options)
        except Exception as exc:  # noqa: BLE001 - 解析器内部保证不抛，这里只兜底
            return self._import_error_result(
                f"解析聊天文本失败（{type(exc).__name__}）。请检查文本格式后重试。",
                dry_run=dry_run,
            )
        warnings_payload = [
            {
                "code": item.code.value,
                "message": redact_text(str(item.message), max_len=300),
                "count": int(item.count),
            }
            for item in result.warnings
        ]
        warning_codes = [item["code"] for item in warnings_payload]
        conversation_title = result.conversation.title or "导入的聊天记录"
        preview = [
            {
                "conversation": conversation_title,
                "sender": str(item.raw.sender or ""),
                "timestamp": (
                    item.raw.timestamp.isoformat() if item.raw.timestamp else ""
                ),
                "length": len(item.raw.text or ""),
                "status": "已导入" if not dry_run else "将导入",
            }
            for item in result.messages[:200]
        ]
        source_fingerprint = {
            "length": len(import_options.source_name or ""),
            "sha256_16": hash_text(import_options.source_name or "clipboard", length=16),
        }
        payload: dict[str, Any] = {
            "ok": False,
            "dry_run": dry_run,
            "experimental": True,
            "notice": EXPERIMENTAL_NOTICE,
            # format 给 UI 直接展示，format_id 给程序判断
            "format": result.format.label_zh,
            "format_id": result.format.value,
            "format_label": result.format.label_zh,
            "source_name": import_options.source_name,
            "stats": result.stats.to_dict(),
            "conversation": result.conversation.masked_dict(),
            "conversation_count": 1 if result.messages else 0,
            "message_count": len(result.messages),
            "imported_count": 0,
            "duplicate_count": 0,
            "skipped_count": int(result.stats.skipped_lines),
            "warning_count": len(warnings_payload),
            "warnings": warnings_payload,
            "warning_codes": warning_codes,
            "preview": preview,
            "source": source_fingerprint,
            "error": None,
        }
        if not result.messages:
            payload["error"] = (
                "未能从文本中解析出消息：请确认文本包含“时间 + 发送者: 内容”"
                "或 JSON/CSV 等可识别格式。"
            )
            payload["duplicate_count"] = int(result.stats.duplicate_messages)
            return payload
        if dry_run:
            payload["ok"] = True
            payload["duplicate_count"] = int(result.stats.duplicate_messages)
            return payload

        try:
            imported_count, duplicate_count, account, conversation = (
                self._persist_import_result(
                    result, source_name=import_options.source_name
                )
            )
        except ServiceError as exc:
            payload["error"] = str(exc)
            return payload
        except Exception as exc:  # noqa: BLE001 - 写入异常不能崩 UI
            payload["error"] = redact_text(
                f"写入本地数据库失败（{type(exc).__name__}）。", max_len=300
            )
            return payload

        self._storage.clear_summaries()
        self._audit.log(
            "chat_text_imported",
            target_kind="account",
            target_id=account.id,
            detail={
                "format": result.format.value,
                "imported": imported_count,
                "duplicate": duplicate_count,
                "conversation_count": 1,
                "source_hash": source_fingerprint["sha256_16"],
                "experimental": True,
            },
        )
        payload.update(
            {
                "ok": True,
                "imported_count": imported_count,
                "duplicate_count": duplicate_count,
                "account_id": account.id,
                "conversation_id": conversation.id,
            }
        )
        return payload

    @staticmethod
    def _import_error_result(message: str, *, dry_run: bool) -> dict[str, Any]:
        """实验性导入的结构化错误（与成功响应同构，UI 不需要 try/except）。"""
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "experimental": True,
            "notice": _IMPORT_NOTICE_FALLBACK,
            "format": "未知格式",
            "format_id": "unknown",
            "format_label": "未知格式",
            "source_name": "",
            "stats": {},
            "conversation": None,
            "conversation_count": 0,
            "message_count": 0,
            "imported_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
            "warning_count": 0,
            "warnings": [],
            "warning_codes": [],
            "preview": [],
            "source": {"length": 0, "sha256_16": ""},
            "error": redact_text(message, max_len=300),
        }

    def _build_import_options(
        self, options: Mapping[str, Any] | None, *, source_name: str
    ):
        """把 UI 传入的 options dict 白名单化并校验成 ImportOptions。

        未知 key 会被忽略（向前兼容）；已知 key 的非法值抛 :class:`ServiceError`，
        由 :meth:`import_chat_text` 转成结构化错误。
        """
        from ..importing import ImportOptions

        if options is None:
            raw: dict[str, Any] = {}
        elif isinstance(options, Mapping):
            raw = dict(options)
        else:
            raise ServiceError("导入选项必须是字典（dict）。")

        def _string(value: Any, name: str, default: str = "") -> str:
            if value is None:
                return default
            if not isinstance(value, str):
                raise ServiceError(f"导入选项 {name} 必须是字符串。")
            return value.strip()

        def _label(value: str, limit: int) -> str:
            cleaned = " ".join(str(value or "").split())
            return cleaned[:limit]

        format_hint = (
            _string(raw.get("format_hint", raw.get("format")), "format_hint") or "auto"
        )
        conversation_key = _label(
            _string(raw.get("conversation_key"), "conversation_key"), 200
        )
        conversation_title = _label(
            _string(raw.get("conversation_title", raw.get("title")), "conversation_title"),
            200,
        )
        kind_text = (
            _string(
                raw.get("conversation_kind", raw.get("kind")), "conversation_kind"
            ).lower()
            or ConversationKind.PRIVATE.value
        )
        try:
            conversation_kind = ConversationKind(kind_text)
        except ValueError:
            raise ServiceError(
                "导入选项 conversation_kind 非法：只支持 private（私聊）或 group（群聊）。"
            ) from None
        if conversation_kind is ConversationKind.MAIL:
            raise ServiceError("聊天文本导入只支持私聊/群聊，不支持邮件会话。")
        account_label = _label(
            _string(raw.get("account_label"), "account_label") or "imported", 100
        )
        effective_source = _label(
            _string(source_name or raw.get("source_name"), "source_name"), 200
        )
        kwargs: dict[str, Any] = {
            "format_hint": format_hint,
            "conversation_key": conversation_key,
            "conversation_title": conversation_title,
            "conversation_kind": conversation_kind,
            "account_label": account_label,
            "source_name": effective_source,
        }

        self_names_value = raw.get("self_sender_names")
        if self_names_value is not None:
            if isinstance(self_names_value, str):
                names = tuple(
                    item.strip()
                    for item in re.split(r"[,，、;；]", self_names_value)
                    if item.strip()
                )
            elif isinstance(self_names_value, (list, tuple, set)):
                names_list: list[str] = []
                for item in self_names_value:
                    if not isinstance(item, str):
                        raise ServiceError(
                            "导入选项 self_sender_names 必须是字符串或字符串列表。"
                        )
                    cleaned = item.strip()
                    if cleaned:
                        names_list.append(cleaned)
                names = tuple(names_list)
            else:
                raise ServiceError(
                    "导入选项 self_sender_names 必须是字符串或字符串列表。"
                )
            kwargs["self_sender_names"] = names

        for key in ("max_input_chars", "max_messages", "max_message_chars"):
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ServiceError(f"导入选项 {key} 必须是 >= 1 的整数。")
            kwargs[key] = value

        dedupe_value = raw.get("dedupe")
        if dedupe_value is not None:
            if not isinstance(dedupe_value, bool):
                raise ServiceError("导入选项 dedupe 必须是布尔值。")
            kwargs["dedupe"] = dedupe_value

        now_value = raw.get("now")
        if now_value is not None:
            if isinstance(now_value, datetime):
                kwargs["now"] = now_value
            elif isinstance(now_value, str):
                try:
                    kwargs["now"] = datetime.fromisoformat(now_value.strip())
                except ValueError:
                    raise ServiceError(
                        "导入选项 now 必须是 ISO8601 时间字符串或 datetime。"
                    ) from None
            else:
                raise ServiceError("导入选项 now 必须是 ISO8601 时间字符串或 datetime。")

        try:
            return ImportOptions(**kwargs)
        except (TypeError, ValueError) as exc:  # noqa: BLE001 - 统一转结构化错误
            raise ServiceError(
                redact_text(f"导入选项不合法：{exc}", max_len=200)
            ) from None

    def _persist_import_result(self, result: Any, *, source_name: str):
        """把解析结果写入本机 SQLite：IMPORT 账号/会话复用 + 去重/分类/待办。"""
        now = self._now()
        account = next(
            (
                item
                for item in self._storage.list_accounts()
                if item.kind is AccountKind.IMPORT
            ),
            None,
        )
        if account is None:
            account = Account(
                id=IMPORT_ACCOUNT_ID,
                kind=AccountKind.IMPORT,
                display_name=IMPORT_ACCOUNT_DISPLAY_NAME,
                enabled=False,
                created_at=now,
                identifier=IMPORT_ACCOUNT_IDENTIFIER,
            )
            self._storage.upsert_account(account)

        meta = result.conversation
        external_key = (
            str(meta.conversation_key or "").strip()
            or f"import:{hash_text(meta.title or 'chat', length=12)}:{meta.kind.value}"
        )
        conversation = self._storage.find_conversation_by_external_key(
            account.id, external_key
        )
        if conversation is None:
            conversation = Conversation(
                id=f"conv-import-{uuid.uuid4().hex[:12]}",
                account_id=account.id,
                kind=meta.kind,
                title=meta.title or "导入的聊天记录",
                external_key=external_key,
                enabled=True,
                note="导入聊天文本（实验性，仅本地；不参与自动采集）",
            )
            self._storage.upsert_conversation(conversation)
        elif conversation.kind is ConversationKind.MAIL:
            raise ServiceError("会话类型冲突：导入会话不能是邮件线程。")

        classifier = self._get_classifier()
        todo_extractor = self._get_todo_extractor()
        safe_source = source_name or "clipboard"
        imported_count = 0
        duplicate_count = 0
        for item in result.messages:
            raw = item.raw
            content_hash = compute_content_hash(
                conversation.id,
                raw.sender,
                raw.is_self,
                raw.text,
                raw.timestamp,
                raw.timestamp_source,
            )
            if self._storage.message_exists(content_hash):
                duplicate_count += 1
                continue
            category = Category.NORMAL
            reasons: tuple[str, ...] = ()
            try:
                decision = classifier.classify(
                    raw,
                    conversation_title=conversation.title,
                    conversation_kind=conversation.kind,
                    conversation_priority=conversation.priority,
                )
                category = decision.category
                reasons = tuple(str(reason) for reason in decision.reasons)
            except Exception:  # noqa: BLE001 - 分类失败不影响导入
                reasons = ("导入消息分类失败，默认归类为普通",)
            message = Message(
                id=uuid.uuid4().hex,
                account_id=account.id,
                conversation_id=conversation.id,
                conversation_title=conversation.title,
                conversation_kind=conversation.kind,
                sender=raw.sender,
                is_self=raw.is_self,
                text=raw.text,
                timestamp=raw.timestamp,
                timestamp_source=raw.timestamp_source,
                collected_at=now,
                category=category,
                category_reasons=reasons,
                content_hash=content_hash,
                source_locator=f"import:{safe_source}:{item.line_no}",
                external_id=raw.external_id,
                subject="",
                is_demo=False,
            )
            if not self._storage.insert_message(message, raw_time_text=item.raw_time_text):
                duplicate_count += 1
                continue
            imported_count += 1
            try:
                extracted = todo_extractor.extract(raw) or []
            except Exception:  # noqa: BLE001 - 待办提取失败不影响导入
                extracted = []
            todos = [
                TodoItem(
                    id=uuid.uuid4().hex,
                    message_id=message.id,
                    conversation_id=conversation.id,
                    conversation_title=conversation.title,
                    text=str(todo.text or "").strip(),
                    due_at=todo.due_at,
                    due_text=todo.due_text,
                    confidence=float(todo.confidence),
                    status=TodoStatus.OPEN,
                    created_at=now,
                )
                for todo in extracted
                if str(todo.text or "").strip()
            ]
            if todos:
                try:
                    self._storage.insert_todos(todos)
                except Exception:  # noqa: BLE001
                    pass
        return imported_count, duplicate_count, account, conversation

    # -- 演示数据 -----------------------------------------------------------

    def load_demo_data(self) -> None:
        self._ensure_open()
        _load_demo_data(self._storage, clock=self._clock)

    def clear_demo_data(self) -> None:
        self._ensure_open()
        _clear_demo_data(self._storage)

    def is_demo_active(self) -> bool:
        self._ensure_open()
        return _is_demo_active(self._storage)

    # -- 本地数据 -----------------------------------------------------------

    def get_storage_stats(self) -> dict[str, Any]:
        self._ensure_open()
        stats = self._storage.stats()
        return {
            "message_count": stats["messages"],
            "conversation_count": stats["conversations"],
            "todo_count": stats["todos"],
            "draft_count": stats["drafts"],
            "db_bytes": stats["db_size_bytes"],
            "data_dir": str(self._paths.data_dir),
        }

    def delete_all_local_data(self) -> None:
        self._ensure_open()
        mail_identifiers = [
            account.identifier
            for account in self._storage.list_accounts()
            if account.kind == AccountKind.EMAIL and account.identifier
        ]
        self._storage.delete_all_data()
        for identifier in mail_identifiers:
            try:
                self._credentials.delete_secret(self._mail_key(identifier))
            except Exception:  # noqa: BLE001 - 清理尽量完成
                pass
        try:
            self._credentials.delete_secret(EXTERNAL_AI_KEY)
        except Exception:  # noqa: BLE001
            pass
        self._settings_store.reset()
        self._last_scan_error = None
        self._storage.append_audit("all_local_data_deleted")

    def export_diagnostics(self) -> str:
        self._ensure_open()
        export_path = self._paths.exports_dir
        payload: dict[str, Any] = {
            "app": APP_NAME,
            "app_id": APP_ID,
            "version": APP_VERSION,
            "generated_at": to_iso(self._now()),
            "data_dir": str(self._paths.data_dir),
            "storage": self._storage.stats(),
            "counts": self.get_storage_stats(),
            "settings": self._exportable_settings(self._settings_store.load()),
            "consent": {scope.value: granted for scope, granted in self._consent.states().items()},
            "credential_presence": self._credential_presence(),
            "collection": self.collection_status(),
            "demo_active": self.is_demo_active(),
            "recent_audit": self._storage.list_audit(limit=_MAX_DIAGNOSTIC_ITEMS),
            "notes": [
                "诊断包不包含消息正文、邮箱授权码或 API Key。",
                "凭据只存在于 Windows 凭据管理器；这里只记录是否已保存。",
            ],
        }
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        target = Path(export_path) / f"diagnostics-{stamp}.json"
        self._write_json_atomic(target, payload)
        self._audit.log(
            "diagnostics_exported",
            detail={"file": target.name, "bytes": target.stat().st_size},
        )
        return str(target)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        try:
            self._storage.append_audit("shutdown")
        except Exception:  # noqa: BLE001
            pass
        self._storage.close()
        self._shutdown = True

    # ======================================================================
    # 内部实现
    # ======================================================================

    def _ensure_open(self) -> None:
        if self._shutdown:
            raise ServiceError("服务已关闭")

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _parse(value: Any) -> datetime | None:
        from .storage import from_iso

        return from_iso(value) if isinstance(value, str) else None

    @staticmethod
    def _timestamp_source(value: Any):
        from ..contracts import TimestampSource

        try:
            return TimestampSource(value)
        except ValueError:
            return TimestampSource.COLLECTED

    def _require_account(self, account_id: str) -> Account:
        account = self._storage.get_account(account_id)
        if account is None:
            raise NotFoundError(f"账号不存在：{account_id}")
        return account

    # -- 同意 ---------------------------------------------------------------

    @staticmethod
    def _read_scope(account: Account) -> ConsentScope:
        if account.kind is AccountKind.IMPORT:
            raise ServiceError("导入聊天文本账号是本地实验数据，不需要读取授权，也不参与采集。")
        return (
            ConsentScope.QQ_READ
            if account.kind is AccountKind.QQ
            else ConsentScope.MAIL_READ
        )

    def _require_read_consent(self, account: Account, *, action: str = "") -> None:
        self._consent.require(self._read_scope(account), action=action)

    def _mirror_consent(self, scope: ConsentScope, granted: bool) -> None:
        field_name = {
            ConsentScope.QQ_READ: "qq_read_consent",
            ConsentScope.MAIL_READ: "mail_read_consent",
            ConsentScope.EXTERNAL_AI: "external_ai_consent",
        }[scope]
        current = self._settings_store.load()
        if getattr(current, field_name) == granted:
            return
        self._settings_store.save(replace(current, **{field_name: bool(granted)}))

    # -- 白名单 -------------------------------------------------------------

    @staticmethod
    def _allowlist_key(account_id: str) -> str:
        return f"allowlist:{account_id}"

    def _get_allowlist(self, account_id: str) -> list[str]:
        raw = self._storage.get_setting(self._allowlist_key(account_id), [])
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]

    def _set_allowlist(self, account_id: str, entries: list[str]) -> None:
        self._storage.set_setting(self._allowlist_key(account_id), entries)

    # -- 采集器 / intel -----------------------------------------------------

    def _make_source(self, account: Account) -> Any | None:
        """构造采集器：优先注入的工厂，其次内置接线，最后通用自动发现。"""
        if self._source_factory is not None:
            try:
                return self._source_factory(account)
            except Exception as exc:  # noqa: BLE001 - 采集器构造失败按不可用处理
                self._logger.warning(
                    "采集器初始化失败（%s）: %s", account.kind.value, type(exc).__name__
                )
                return None
        builtin = self._wire_builtin_source(account)
        if builtin is not None:
            return builtin
        try:
            return default_source_factory(account)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "采集器自动发现失败（%s）: %s", account.kind.value, type(exc).__name__
            )
            return None

    def _wire_builtin_source(self, account: Account) -> Any | None:
        """按账号类型接线真实采集器；采集器模块缺失时返回 None（不影响其它功能）。"""
        if account.kind is AccountKind.IMPORT:
            # 用户主动导入的文本不是采集源，绝不联网/读 QQ。
            return None
        if account.kind == AccountKind.EMAIL:
            return self._wire_mail_source(account)
        return self._wire_qq_source(account)

    def _wire_mail_source(self, account: Account) -> Any | None:
        """邮件：MailCollector（只读 IMAP）+ 凭据管理器 + SQLite 增量状态。"""
        try:
            from ..collectors.mail import DEFAULT_FOLDERS, MailCollector
        except Exception:  # noqa: BLE001 - 采集器模块不可用
            return None
        folders = self._get_allowlist(account.id) or list(DEFAULT_FOLDERS)
        try:
            source = MailCollector(
                account=account,
                credential_store=self._credentials,
                sync_state=self._sync_state_store,
                folders=folders,
            )
        except Exception as exc:  # noqa: BLE001 - 账号未配置邮箱地址等
            self._logger.warning("邮件采集器接线失败：%s", type(exc).__name__)
            return None
        # 把采集器实际使用的凭据 key 暴露给服务层（set/has/delete 凭据时保持一致）
        credential_key = getattr(source, "_credential_key", None)
        if isinstance(credential_key, str) and credential_key:
            self._mail_credential_keys.setdefault(account.identifier.lower(), credential_key)
        return source

    def _wire_qq_source(self, account: Account) -> Any | None:
        """QQ：WorkerQQMessageSource —— UIA/comtypes 只在子进程中 import（COM 隔离）。"""
        allowlist = self._get_allowlist(account.id)
        try:
            return WorkerQQMessageSource(
                account=account,
                allowlist=allowlist,
                command=self._qq_worker_command,
                timeout=self._qq_worker_timeout,
                runner=self._qq_worker_runner,
                env=self._qq_worker_env,
                output_dir=self._qq_worker_output_dir,
            )
        except Exception as exc:  # noqa: BLE001 - 接线失败按不可用处理，不影响 UI
            self._logger.warning("QQ 采集子进程接线失败：%s", type(exc).__name__)
            return None

    @staticmethod
    def _close_source(source: Any) -> None:
        closer = getattr(source, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    def _source_unavailable_detail(self, account: Account) -> str:
        label = "QQ" if account.kind == AccountKind.QQ else "邮件"
        return f"{label} 只读采集器不可用（尚未接入或未配置）"

    def _get_classifier(self) -> Classifier:
        if self._classifier is None:
            self._classifier = _load_default_classifier()
        return self._classifier

    def _get_todo_extractor(self) -> TodoExtractor:
        if self._todo_extractor is None:
            self._todo_extractor = _load_default_todo_extractor(self._clock)
        return self._todo_extractor

    def _get_draft_provider(self) -> DraftProvider:
        if self._draft_provider is None:
            self._draft_provider = _load_default_draft_provider(self._clock)
        return self._draft_provider

    def _build_pipeline(self) -> ScanPipeline:
        return ScanPipeline(
            self._storage,
            # 走 _make_source：注入工厂 > 内置 QQ/邮件接线 > 通用自动发现
            source_factory=self._make_source,
            classifier=self._get_classifier(),
            todo_extractor=self._get_todo_extractor(),
            is_paused=lambda: self._settings_store.load().collection_paused,
            clock=self._clock,
            logger=self._logger,
        )

    # -- 外部 AI ------------------------------------------------------------

    def _make_external_provider(self) -> Any | None:
        if self._external_provider_factory is not None:
            try:
                provider = self._external_provider_factory()
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "外部 AI 提供方初始化失败：%s", type(exc).__name__
                )
                return None
            if provider is None:
                return None
            if callable(getattr(provider, "propose", None)):
                return provider
            return ExternalDraftAdapter(provider)

        settings = self._settings_store.load()
        if not settings.external_ai_base_url or not settings.external_ai_model:
            return None
        try:
            from ..intel.external_ai import ExternalLLMClient  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001
            return None
        config = ExternalAIConfig(
            base_url=settings.external_ai_base_url,
            model=settings.external_ai_model,
            key_ref=EXTERNAL_AI_KEY,
        )
        attempts: tuple[dict[str, Any], ...] = (
            {
                "config": config,
                "credential_store": self._credentials,
                "consent_manager": self._consent,
            },
            {"config": config, "credential_store": self._credentials},
            {"config": config},
            {},
        )
        for kwargs in attempts:
            try:
                return ExternalDraftAdapter(ExternalLLMClient(**kwargs))
            except Exception:  # noqa: BLE001 - 接口未定稿，尝试下一种构造方式
                continue
        return None

    def _collect_drafts(
        self,
        provider: Any,
        message: Message,
        styles: list[str],
        *,
        external: bool,
    ) -> list[ReplyDraft]:
        recent = tuple(
            self._storage.list_messages(
                conversation_id=message.conversation_id, limit=8
            )
        )
        drafts: list[ReplyDraft] = []
        for style in styles:
            context = DraftContext(
                conversation_title=message.conversation_title,
                recent_messages=recent,
                style=style,
            )
            try:
                produced = provider.propose(message, context)
            except Exception as exc:  # noqa: BLE001
                if external:
                    raise ServiceError(
                        f"外部 AI 生成草稿失败（{safe_error(exc)}）"
                    ) from None
                self._logger.warning(
                    "本地草稿生成失败，使用内置模板：%s", type(exc).__name__
                )
                produced = TemplateDraftProvider().propose(message, context)
            drafts.extend(
                self._normalize_drafts(produced, message, style, external=external)
            )
        return drafts

    def _normalize_drafts(
        self,
        produced: Any,
        message: Message,
        style: str,
        *,
        external: bool,
    ) -> list[ReplyDraft]:
        drafts: list[ReplyDraft] = []
        for item in produced or []:
            if isinstance(item, ReplyDraft):
                text = str(item.text or "").strip()
                if not text:
                    continue
                drafts.append(
                    replace(
                        item,
                        id=item.id or uuid.uuid4().hex,
                        message_id=message.id,
                        kind=DraftKind.EXTERNAL_LLM if external else DraftKind.LOCAL_RULES,
                        style=item.style or style,
                        created_at=item.created_at or self._now(),
                        model_label=item.model_label
                        or ("外部 AI" if external else "本地模板"),
                        requires_external_consent=bool(external),
                    )
                )
            elif isinstance(item, str):
                text = item.strip()
                if text:
                    drafts.append(self._make_draft(message, style, text, external))
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    drafts.append(self._make_draft(message, style, text, external))
        return drafts

    def _make_draft(
        self, message: Message, style: str, text: str, external: bool
    ) -> ReplyDraft:
        return ReplyDraft(
            id=uuid.uuid4().hex,
            message_id=message.id,
            kind=DraftKind.EXTERNAL_LLM if external else DraftKind.LOCAL_RULES,
            style=style,
            text=text,
            created_at=self._now(),
            model_label="外部 AI" if external else "本地模板",
            requires_external_consent=bool(external),
        )

    # -- 结果辅助 -----------------------------------------------------------

    def _blocked_result(
        self, account: Account, status: ScanStatus, error: str
    ) -> CollectionResult:
        now = self._now()
        return CollectionResult(
            scan_id=uuid.uuid4().hex,
            account_id=account.id,
            status=status,
            error=error,
            started_at=now,
            finished_at=now,
        )

    def _failed_result(self, account_id: str, error: str) -> CollectionResult:
        now = self._now()
        return CollectionResult(
            scan_id=uuid.uuid4().hex,
            account_id=account_id,
            status=ScanStatus.FAILED,
            error=error,
            started_at=now,
            finished_at=now,
        )

    @staticmethod
    def _coerce_connection_check(
        value: Any, *, success_detail: str = "连接成功（只读）"
    ) -> ConnectionCheck:
        if isinstance(value, ConnectionCheck):
            return value
        if isinstance(value, bool):
            return ConnectionCheck(ok=value, detail=success_detail if value else "连接失败")
        if value is None:
            return ConnectionCheck(ok=True, detail=success_detail)
        return ConnectionCheck(ok=True, detail=redact_text(value, max_len=200))

    def _mail_key(self, email: str) -> str:
        """邮箱授权码在凭据管理器中的 key。

        真实邮件采集器（collectors.mail.mail_credential_key）使用
        ``mail.auth_code.<email>``；为兼容两者，优先采用采集器的 key，采集器缺失时
        回退到 security.credential_store 的 ``mail:qq:<email>`` 规范。
        """
        normalized = (email or "").strip().lower()
        if not normalized or "@" not in normalized:
            raise ServiceError("邮箱地址格式不正确")
        cached = self._mail_credential_keys.get(normalized)
        if cached:
            return cached
        try:
            from ..collectors.mail import mail_credential_key as collector_mail_key

            key = collector_mail_key(normalized)
            self._mail_credential_keys[normalized] = key
            return key
        except Exception:  # noqa: BLE001 - 采集器未就绪时用安全模块规范
            return mail_credential_key(normalized)

    def _credential_presence(self) -> dict[str, Any]:
        mail_accounts = [
            account
            for account in self._storage.list_accounts()
            if account.kind == AccountKind.EMAIL and account.identifier
        ]
        saved = 0
        for account in mail_accounts:
            try:
                if self._credentials.has_secret(self._mail_key(account.identifier)):
                    saved += 1
            except Exception:  # noqa: BLE001
                continue
        return {
            "mail_accounts": len(mail_accounts),
            "mail_codes_saved": saved,
            "external_ai_key_saved": self.has_external_ai_key(),
        }

    @staticmethod
    def _exportable_settings(settings: AppSettings) -> dict[str, Any]:
        data = settings.to_dict()
        url = str(data.get("external_ai_base_url") or "")
        if url:
            try:
                parts = urlsplit(url)
                data["external_ai_base_url"] = (
                    f"{parts.scheme}://{parts.netloc}{parts.path}"[:200]
                )
            except ValueError:
                data["external_ai_base_url"] = "<无法解析>"
        model = str(data.get("external_ai_model") or "")
        if model and looks_like_secret(model):
            data["external_ai_model"] = "<已脱敏>"
        # 双重保险：任何设置项都不应包含密钥
        for key, value in list(data.items()):
            if isinstance(value, str) and looks_like_secret(value):
                data[key] = "<已脱敏>"
        return data

    @staticmethod
    def _write_json_atomic(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


__all__ = [
    "DEFAULT_DRAFT_STYLES",
    "NotFoundError",
    "ServiceError",
    "SocialSecretaryService",
    "default_source_factory",
    "set_default_source_factory",
]
