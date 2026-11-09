"""采集流水线：白名单会话 → 采集 → 去重 → 落库 → 分类 → 待办提取 → scan_run 记录。

安全与稳定性约束：
- **只读**：本模块只调用 ``MessageSource`` 的读取方法，不存在任何发送/修改路径。
- 白名单强制：只读取 ``enabled=True`` 的会话；采集器返回的未授权会话消息一律丢弃。
- 采集器异常不崩溃：单个账号/会话失败记录为 PARTIAL/FAILED，并把脱敏后的错误写进
  ``scan_runs``，不向 UI 抛异常。
- 暂停：``is_paused()`` 为真时直接返回 ``ScanStatus.PAUSED``，不触碰采集器。
- 外部 AI 不参与本流程：分类/待办均使用本地注入实现，无任何网络请求。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from ..contracts import (
    Account,
    AccountKind,
    Category,
    CategoryDecision,
    Classifier,
    CollectionResult,
    Conversation,
    ConversationKind,
    ExtractedTodo,
    Message,
    Priority,
    RawMessage,
    ScanStatus,
    TodoExtractor,
    TodoItem,
    TodoStatus,
)
from ..security.log_redaction import safe_error
from .dedupe import MessageDeduplicator
from .storage import Storage, is_demo_account_id, normalize_todo_text, now_local

DEFAULT_FETCH_LIMIT = 200

#: 邮件附件仅保存文件名（不下载内容），并限制数量/长度，避免 UI/DB 被异常数据拖垮。
MAX_ATTACHMENT_NAMES = 10
MAX_ATTACHMENT_NAME_CHARS = 120

SourceFactory = Callable[[Account], Any | None]
ConsentGate = Callable[[Account], str | None]


def sanitize_attachment_names(value: Any) -> tuple[str, ...]:
    """把采集器 extras 里的 ``attachment_names`` 清洗成安全文件名元组。

    - 只接受 list/tuple[str]；非字符串项丢弃；
    - 去控制字符（保留可打印字符）、strip、单名截断到 120 字符；
    - 去重（保序）、最多 10 个；不读取/下载任何附件内容。
    """
    if not isinstance(value, (list, tuple)):
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = "".join(char for char in item if char.isprintable()).strip()
        if not name:
            continue
        if len(name) > MAX_ATTACHMENT_NAME_CHARS:
            name = name[:MAX_ATTACHMENT_NAME_CHARS]
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
        if len(cleaned) >= MAX_ATTACHMENT_NAMES:
            break
    return tuple(cleaned)


class NullClassifier:
    """intel 未就绪时的安全兜底：全部归为「普通」，不联网。"""

    def classify(
        self,
        raw: RawMessage,
        *,
        conversation_title: str,
        conversation_kind,
        conversation_priority: Priority = Priority.NORMAL,
    ) -> CategoryDecision:
        return CategoryDecision(
            category=Category.NORMAL,
            reasons=("本地分类规则未加载，默认归类为普通",),
            score=0.0,
        )


class NullTodoExtractor:
    """intel 未就绪时的空实现。"""

    def extract(self, raw: RawMessage) -> list[ExtractedTodo]:
        return []


class ScanPipeline:
    """一次扫描编排器。每个账号产出一个 :class:`CollectionResult`。"""

    def __init__(
        self,
        storage: Storage,
        *,
        source_factory: SourceFactory,
        classifier: Classifier | None = None,
        todo_extractor: TodoExtractor | None = None,
        is_paused: Callable[[], bool] | None = None,
        clock: Callable[[], Any] | None = None,
        fetch_limit: int = DEFAULT_FETCH_LIMIT,
        logger: Any | None = None,
    ) -> None:
        self._storage = storage
        self._source_factory = source_factory
        self._classifier: Classifier = classifier or NullClassifier()
        self._todo_extractor: TodoExtractor = todo_extractor or NullTodoExtractor()
        self._is_paused = is_paused or (lambda: False)
        self._clock = clock or now_local
        self._fetch_limit = max(1, int(fetch_limit))
        self._logger = logger

    # -- 公共入口 -----------------------------------------------------------

    def run(
        self,
        account_ids: Sequence[str] | None = None,
        *,
        trigger: str = "manual",
        foreground_ack: bool = False,
        consent_gate: ConsentGate | None = None,
    ) -> list[CollectionResult]:
        accounts = self._select_accounts(account_ids)
        if not accounts:
            return []
        if self._is_paused():
            return [self._simple_result(account, ScanStatus.PAUSED, "采集已暂停") for account in accounts]

        results: list[CollectionResult] = []
        for account in accounts:
            if consent_gate is not None:
                reason = consent_gate(account)
                if reason:
                    result = self._simple_result(account, ScanStatus.BLOCKED, reason)
                    self._storage.insert_scan_run(result, trigger=trigger)
                    results.append(result)
                    continue
            results.append(
                self._scan_account(
                    account, trigger=trigger, foreground_ack=foreground_ack
                )
            )
        return results

    # -- 单账号 -------------------------------------------------------------

    def _scan_account(
        self,
        account: Account,
        *,
        trigger: str,
        foreground_ack: bool,
    ) -> CollectionResult:
        started = self._clock()
        scan_id = uuid.uuid4().hex
        warnings: list[str] = []
        errors: list[str] = []
        messages_seen = 0
        messages_new = 0
        conversations_scanned = 0
        conversations = [
            conversation
            for conversation in self._storage.list_conversations(account.id)
            if conversation.enabled
        ]
        source = None

        if not conversations:
            warnings.append("没有已启用的会话（白名单为空），本次未读取任何内容")
        else:
            try:
                source = self._source_factory(account)
            except Exception as exc:  # noqa: BLE001 - 采集器初始化失败不应崩溃
                source = None
                errors.append(safe_error(exc))
                warnings.append("采集器初始化失败")

            if source is None:
                if not errors:
                    errors.append("采集器不可用（未接入或未配置）")
                    warnings.append("采集器不可用，无法读取消息")
            else:
                failed_conversations: list[tuple[Conversation, str]] = []
                for conversation in conversations:
                    try:
                        raws = source.fetch_messages(
                            conversation,
                            since=conversation.last_scanned_at,
                            limit=self._fetch_limit,
                            foreground_ack=foreground_ack,
                        )
                    except Exception as exc:  # noqa: BLE001 - 先记录，稍后自动重试一次
                        failed_conversations.append((conversation, type(exc).__name__))
                        warnings.append(
                            f"会话《{conversation.title}》首次读取失败（{type(exc).__name__}），将自动重试"
                        )
                        continue
                    conversations_scanned += 1
                    batch = MessageDeduplicator()
                    for raw in raws or []:
                        messages_seen += 1
                        try:
                            if self._ingest(account, conversation, raw, batch, warnings):
                                messages_new += 1
                        except Exception as exc:  # noqa: BLE001 - 单条消息失败不中断整批
                            warnings.append(
                                f"消息处理失败（{type(exc).__name__}），已跳过"
                            )
                    try:
                        self._storage.update_conversation(
                            conversation.id, last_scanned_at=self._clock()
                        )
                    except Exception:  # noqa: BLE001
                        warnings.append(f"会话《{conversation.title}》扫描时间更新失败")

                # 多会话切换常见「第一个会话读完后窗口失焦/树未就绪」；失败会话自动重试一次，
                # 避免用户看到“只读了一部分”却不知道为什么。成功会话不会重复读取（去重兜底）。
                for conversation, first_error in failed_conversations:
                    try:
                        raws = source.fetch_messages(
                            conversation,
                            since=conversation.last_scanned_at,
                            limit=self._fetch_limit,
                            foreground_ack=foreground_ack,
                        )
                    except Exception as exc:  # noqa: BLE001 - 重试仍失败才计入错误
                        errors.append(safe_error(exc))
                        warnings.append(
                            f"会话《{conversation.title}》重试仍失败（{type(exc).__name__}；首次：{first_error}）"
                        )
                        continue
                    conversations_scanned += 1
                    batch = MessageDeduplicator()
                    for raw in raws or []:
                        messages_seen += 1
                        try:
                            if self._ingest(account, conversation, raw, batch, warnings):
                                messages_new += 1
                        except Exception as exc:  # noqa: BLE001
                            warnings.append(f"消息处理失败（{type(exc).__name__}），已跳过")
                    try:
                        self._storage.update_conversation(
                            conversation.id, last_scanned_at=self._clock()
                        )
                    except Exception:  # noqa: BLE001
                        warnings.append(f"会话《{conversation.title}》扫描时间更新失败")

        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                warnings.append("采集器关闭时出现异常（已忽略）")

        if errors:
            status = ScanStatus.FAILED if conversations_scanned == 0 else ScanStatus.PARTIAL
        else:
            status = ScanStatus.OK

        finished = self._clock()
        result = CollectionResult(
            scan_id=scan_id,
            account_id=account.id,
            status=status,
            messages_seen=messages_seen,
            messages_new=messages_new,
            conversations_scanned=conversations_scanned,
            error="；".join(errors) if errors else None,
            warnings=tuple(warnings),
            started_at=started,
            finished_at=finished,
            foreground_used=bool(
                foreground_ack and account.kind == AccountKind.QQ
            ),
            duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
        self._storage.insert_scan_run(result, trigger=trigger)
        self._safe_audit(
            "scan_account_finished",
            account.id,
            {
                "scan_id": scan_id,
                "status": status.value,
                "conversations_scanned": conversations_scanned,
                "messages_seen": messages_seen,
                "messages_new": messages_new,
                "duration_ms": result.duration_ms,
                "trigger": trigger,
                "warnings": len(warnings),
            },
        )
        return result

    # -- 单条消息 -----------------------------------------------------------

    def _ingest(
        self,
        account: Account,
        conversation: Conversation,
        raw: RawMessage,
        batch: MessageDeduplicator,
        warnings: list[str],
    ) -> bool:
        target = self._resolve_conversation(account, conversation, raw, warnings)
        if target is None:
            return False

        decision = batch.decide(target.id, raw)
        if decision.duplicate_in_batch:
            return False
        if raw.external_id and self._storage.find_message_by_external_id(
            target.id, raw.external_id
        ) is not None:
            return False
        if self._storage.message_exists(decision.content_hash):
            return False

        category = Category.NORMAL
        reasons: tuple[str, ...] = ()
        try:
            category_decision = self._classifier.classify(
                raw,
                conversation_title=target.title,
                conversation_kind=target.kind,
                conversation_priority=target.priority,
            )
            category = category_decision.category
            reasons = tuple(str(reason) for reason in category_decision.reasons)
        except Exception as exc:  # noqa: BLE001 - 分类失败不影响入库
            warnings.append(f"分类失败（{type(exc).__name__}），默认归类为普通")
            reasons = ("分类器异常，默认归类为普通",)

        collected_at = self._clock()
        # 邮件主题/附件名来自采集器 extras；QQ/导入消息没有这些字段，保持空。
        subject = ""
        attachment_names: tuple[str, ...] = ()
        if target.kind is ConversationKind.MAIL:
            extras = raw.extras or {}
            subject = str(extras.get("subject") or "").strip()
            attachment_names = sanitize_attachment_names(extras.get("attachment_names"))
        message = Message(
            id=uuid.uuid4().hex,
            account_id=account.id,
            conversation_id=target.id,
            conversation_title=target.title,
            conversation_kind=target.kind,
            sender=raw.sender,
            is_self=raw.is_self,
            text=raw.text,
            timestamp=raw.timestamp,
            timestamp_source=raw.timestamp_source,
            collected_at=collected_at,
            category=category,
            category_reasons=reasons,
            content_hash=decision.content_hash,
            source_locator=raw.source_locator,
            external_id=raw.external_id,
            subject=subject,
            is_demo=is_demo_account_id(account.id),
            attachment_names=attachment_names,
        )
        inserted = self._storage.insert_message(message, raw_time_text=raw.raw_time_text)
        if not inserted:
            return False

        # 待办提取（本地规则实现；异常不影响消息入库）
        try:
            extracted = self._todo_extractor.extract(raw) or []
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"待办提取失败（{type(exc).__name__}），已跳过")
            extracted = []
        todos: list[TodoItem] = []
        seen_todo_texts: set[str] = set()
        for item in extracted:
            text = normalize_todo_text(item.text)
            if not text or text in seen_todo_texts:
                continue
            seen_todo_texts.add(text)
            todos.append(
                TodoItem(
                    id=uuid.uuid4().hex,
                    message_id=message.id,
                    conversation_id=target.id,
                    conversation_title=target.title,
                    text=text,
                    due_at=item.due_at,
                    due_text=item.due_text,
                    confidence=float(item.confidence),
                    status=TodoStatus.OPEN,
                    created_at=collected_at,
                )
            )
        if todos:
            try:
                self._storage.insert_todos(todos)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"待办入库失败（{type(exc).__name__}），已跳过")
        return True

    def _resolve_conversation(
        self,
        account: Account,
        conversation: Conversation,
        raw: RawMessage,
        warnings: list[str],
    ) -> Conversation | None:
        key = (raw.conversation_key or "").strip()
        # 不同采集器用不同值回填 conversation_key：
        # - QQ：external_key（会话标题）
        # - 邮件：conversation.id（本核心层传入的会话 id）
        if not key or key in (conversation.external_key, conversation.id):
            return conversation
        # 采集器在读取 A 会话时返回了 B 会话的消息：只允许映射到已授权会话
        other = self._storage.find_conversation_by_external_key(account.id, key)
        if other is None:
            warnings.append("忽略未知会话的消息（不在会话表中）")
            return None
        if not other.enabled:
            warnings.append(f"忽略未授权会话《{other.title}》的消息")
            return None
        return other

    # -- 辅助 ---------------------------------------------------------------

    def _select_accounts(self, account_ids: Sequence[str] | None) -> list[Account]:
        if account_ids is None:
            return self._storage.list_accounts(include_disabled=False)
        accounts: list[Account] = []
        for account_id in account_ids:
            account = self._storage.get_account(account_id)
            if account is not None:
                accounts.append(account)
        return accounts

    def _simple_result(
        self, account: Account, status: ScanStatus, error: str
    ) -> CollectionResult:
        now = self._clock()
        return CollectionResult(
            scan_id=uuid.uuid4().hex,
            account_id=account.id,
            status=status,
            error=error,
            started_at=now,
            finished_at=now,
        )

    def _safe_audit(self, action: str, account_id: str, detail: dict[str, Any]) -> None:
        try:
            self._storage.append_audit(
                action, target_kind="account", target_id=account_id, detail=detail
            )
        except Exception:  # noqa: BLE001 - 审计写失败不影响扫描
            pass


__all__ = [
    "ConsentGate",
    "DEFAULT_FETCH_LIMIT",
    "MAX_ATTACHMENT_NAME_CHARS",
    "MAX_ATTACHMENT_NAMES",
    "NullClassifier",
    "NullTodoExtractor",
    "ScanPipeline",
    "SourceFactory",
    "sanitize_attachment_names",
]
