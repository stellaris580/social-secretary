"""Ui 测试与截图专用的假服务：完整实现 ``contracts.ServiceAPI``。

重要说明：
- 所有数据都是**程序生成的合成示例**（人名、消息都带「示例」），不含任何真实聊天/邮件内容。
- 全部数据只存在内存里，不写文件、不联网、不创建凭据。
- 记录所有服务调用（``calls``），便于测试断言 UI 是否调用了正确的方法、是否误调用了危险方法。
- 提供 ``fail_method()`` 注入异常，用于测试 UI 的错误提示路径。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

from social_secretary.contracts import (
    Account,
    AccountKind,
    AppSettings,
    Category,
    CollectionResult,
    ConnectionCheck,
    ConsentScope,
    Conversation,
    ConversationKind,
    DailySummary,
    DraftKind,
    Message,
    MessageWorkStatus,
    Priority,
    ReplyDraft,
    ScanStatus,
    SourceDetail,
    TimestampSource,
    TodoItem,
    TodoStatus,
)

DIAGNOSTICS_HINT = "（演示服务，仅生成计数信息，不写文件、不包含任何正文）"


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


class FakeService:
    """内存版 ServiceAPI 实现（仅供 UI 测试/截图）。

    ``now`` 可注入固定时刻，便于测试跨零点/固定日期场景；演示数据的时间都基于这个
    「演示时钟」生成，并锚定在当天 20:00，保证所有消息都落在同一天（不会因测试在
    深夜运行而把消息推到前一天）。``generate_daily_summary()`` 在不传 ``day`` 时也以
    这个演示日期为准，因此即使运行过程中真实日期跨天，计数也不会变成 0。
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._failures: dict[str, Exception] = {}
        self._now_dt = (now or datetime.now().astimezone()).replace(microsecond=0)
        self.settings = AppSettings()
        self._accounts: list[Account] = []
        self._conversations: list[Conversation] = []
        self._messages: list[Message] = []
        self._todos: list[TodoItem] = []
        self._drafts: dict[str, list[ReplyDraft]] = {}
        self._summary: DailySummary | None = None
        self._demo_active = False
        self._paused = True
        self._last_scan_at: datetime | None = None
        self._last_scan_status: str | None = None
        self._last_scan_error: str | None = None
        self._consent_history: list[dict[str, Any]] = []
        self._mail_credentials: dict[str, int] = {}   # 只记录长度，绝不保存明文
        self._external_ai_key_length: int | None = None
        self._imported_hashes: set[str] = set()
        self._deleted_all = False
        # organize_all 测试可调：成功/失败的数量（默认演示：QQ 2 成功、邮箱 1 成功）
        self.organize_qq_success = 2
        self.organize_mail_success = 1
        self.organize_qq_failed = 0
        self.organize_mail_failed = 0
        # P0 真实失败反馈字段（默认演示成功；测试可覆盖）
        self.organize_qq_authorized = 3
        self.organize_qq_sessions_scanned = 2
        self.organize_qq_sessions_failed = 0
        self.organize_qq_failure_hint = ""
        self.organize_mail_new = 1
        self.organize_history_total: int | None = None
        self.organize_latest_day: date | None = None
        self._load_demo_data()
        # 演示数据加载后处于「未暂停」状态；把设置与内部状态保持一致。
        self.settings = replace(self.settings, collection_paused=self._paused)

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------
    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def _maybe_fail(self, name: str) -> None:
        exc = self._failures.get(name)
        if exc is not None:
            raise exc

    def fail_method(self, name: str, message: str = "演示故障（FakeService）") -> None:
        self._failures[name] = RuntimeError(message)

    def clear_failure(self, name: str) -> None:
        self._failures.pop(name, None)

    def calls_to(self, name: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return [call for call in self.calls if call[0] == name]

    def called(self, name: str) -> bool:
        return bool(self.calls_to(name))

    def methods_called(self) -> set[str]:
        return {call[0] for call in self.calls}

    def reset_calls(self) -> None:
        self.calls.clear()

    # ------------------------------------------------------------------
    # 演示数据
    # ------------------------------------------------------------------
    def demo_day(self) -> date:
        """演示数据的「今天」（注入时钟的日期）。"""
        return self._now_dt.date()

    def _load_demo_data(self) -> None:
        now = self._now_dt
        today = now.date()
        # 演示消息都放在当天 20:00 之前，偏移最多 3 小时，保证不会跨到前一天。
        base = datetime.combine(today, time(20, 0)).replace(tzinfo=now.tzinfo)

        self._accounts = [
            Account(
                id="acc-qq",
                kind=AccountKind.QQ,
                display_name="我的 QQ（示例）",
                enabled=True,
                created_at=now - timedelta(days=30),
            ),
            Account(
                id="acc-mail",
                kind=AccountKind.EMAIL,
                display_name="QQ 邮箱（示例）",
                enabled=True,
                created_at=now - timedelta(days=30),
                identifier="demo@qq.com",
            ),
        ]
        self._conversations = [
            Conversation(
                id="conv-zhang",
                account_id="acc-qq",
                kind=ConversationKind.PRIVATE,
                title="小张（示例）",
                external_key="小张（示例）",
                enabled=True,
                priority=Priority.HIGH,
                last_scanned_at=now - timedelta(hours=1),
                last_message_at=base - timedelta(minutes=35),
            ),
            Conversation(
                id="conv-project",
                account_id="acc-qq",
                kind=ConversationKind.GROUP,
                title="项目讨论组（示例）",
                external_key="项目讨论组（示例）",
                enabled=True,
                priority=Priority.NORMAL,
                last_scanned_at=now - timedelta(hours=1),
                last_message_at=base - timedelta(minutes=50),
            ),
            Conversation(
                id="conv-family",
                account_id="acc-qq",
                kind=ConversationKind.GROUP,
                title="家人群（示例）",
                external_key="家人群（示例）",
                enabled=False,
                priority=Priority.LOW,
                last_scanned_at=None,
                last_message_at=base - timedelta(days=2),
            ),
            Conversation(
                id="conv-inbox",
                account_id="acc-mail",
                kind=ConversationKind.MAIL,
                title="QQ 邮箱 · 收件箱（示例）",
                external_key="INBOX",
                enabled=True,
                priority=Priority.NORMAL,
                last_scanned_at=now - timedelta(minutes=20),
                last_message_at=base - timedelta(minutes=20),
            ),
        ]
        self._messages = [
            Message(
                id="msg-1",
                account_id="acc-qq",
                conversation_id="conv-zhang",
                conversation_title="小张（示例）",
                conversation_kind=ConversationKind.PRIVATE,
                sender="小张（示例）",
                is_self=False,
                text="下午 3 点前把活动方案发我一下，客户在等，麻烦了。",
                timestamp=base - timedelta(minutes=35),
                timestamp_source=TimestampSource.UI,
                collected_at=now - timedelta(minutes=10),
                category=Category.IMPORTANT,
                category_reasons=("私聊里直接请求你做事", "包含明确截止时间"),
                content_hash="demo-hash-1",
                source_locator="QQ 聊天窗口 · 第 12 条可见气泡（示例）",
                external_id="demo-qq-1",
            ),
            Message(
                id="msg-2",
                account_id="acc-mail",
                conversation_id="conv-inbox",
                conversation_title="QQ 邮箱 · 收件箱（示例）",
                conversation_kind=ConversationKind.MAIL,
                sender="活动负责人（示例）",
                is_self=False,
                text="请在周五下班前确认周六活动的场地和人数，并回复这封邮件。",
                timestamp=base - timedelta(minutes=20),
                timestamp_source=TimestampSource.UI,
                collected_at=now - timedelta(minutes=10),
                category=Category.IMPORTANT,
                category_reasons=("邮件要求回复", "包含截止时间"),
                content_hash="demo-hash-2",
                source_locator="IMAP INBOX UID 1024（示例）",
                external_id="demo-mail-1",
            ),
            Message(
                id="msg-3",
                account_id="acc-qq",
                conversation_id="conv-project",
                conversation_title="项目讨论组（示例）",
                conversation_kind=ConversationKind.GROUP,
                sender="李工（示例）",
                is_self=False,
                text="会议改到明天上午 10 点，还是在 A 会议室。",
                timestamp=base - timedelta(minutes=50),
                timestamp_source=TimestampSource.RELATIVE,
                collected_at=now - timedelta(minutes=10),
                category=Category.NORMAL,
                category_reasons=(),
                content_hash="demo-hash-3",
                source_locator="QQ 聊天窗口 · 第 5 条可见气泡（示例）",
                external_id="demo-qq-2",
            ),
            Message(
                id="msg-4",
                account_id="acc-qq",
                conversation_id="conv-project",
                conversation_title="项目讨论组（示例）",
                conversation_kind=ConversationKind.GROUP,
                sender="我（示例）",
                is_self=True,
                text="收到，我准时到。",
                timestamp=base - timedelta(minutes=48),
                timestamp_source=TimestampSource.UI,
                collected_at=now - timedelta(minutes=10),
                category=Category.NORMAL,
                content_hash="demo-hash-4",
            ),
            Message(
                id="msg-5",
                account_id="acc-qq",
                conversation_id="conv-project",
                conversation_title="项目讨论组（示例）",
                conversation_kind=ConversationKind.GROUP,
                sender="行政（示例）",
                is_self=False,
                text="群公告：本周五 18:00 组织团建，报名链接在群文件里，自愿参加。",
                timestamp=base - timedelta(minutes=90),
                timestamp_source=TimestampSource.UI,
                collected_at=now - timedelta(minutes=10),
                category=Category.FYI,
                category_reasons=("群公告", "通知类消息"),
                content_hash="demo-hash-5",
                source_locator="QQ 聊天窗口 · 第 2 条可见气泡（示例）",
                external_id="demo-qq-3",
            ),
            Message(
                id="msg-6",
                account_id="acc-mail",
                conversation_id="conv-inbox",
                conversation_title="QQ 邮箱 · 收件箱（示例）",
                conversation_kind=ConversationKind.MAIL,
                sender="系统通知（示例）",
                is_self=False,
                text="你的 QQ 邮箱已成功开启 IMAP 服务。",
                timestamp=base - timedelta(hours=3),
                timestamp_source=TimestampSource.UI,
                collected_at=now - timedelta(minutes=10),
                category=Category.FYI,
                category_reasons=("系统通知",),
                content_hash="demo-hash-6",
                source_locator="IMAP INBOX UID 980（示例）",
                external_id="demo-mail-2",
            ),
        ]
        # 演示数据里的所有消息都标记 is_demo=True：收件箱/摘要必须能区分「真实/演示」。
        self._messages = [replace(message, is_demo=True) for message in self._messages]
        self._todos = [
            TodoItem(
                id="todo-1",
                message_id="msg-1",
                conversation_id="conv-zhang",
                conversation_title="小张（示例）",
                text="下午 3 点前把活动方案发给小张",
                due_at=base.replace(hour=15, minute=0) + timedelta(days=0)
                if base.hour < 15
                else base.replace(hour=15, minute=0) + timedelta(days=1),
                due_text="下午 3 点前",
                confidence=0.86,
                status=TodoStatus.OPEN,
                created_at=now - timedelta(minutes=10),
            ),
            TodoItem(
                id="todo-2",
                message_id="msg-2",
                conversation_id="conv-inbox",
                conversation_title="QQ 邮箱 · 收件箱（示例）",
                text="确认周六活动的场地和人数",
                due_at=base + timedelta(days=1, hours=2),
                due_text="周五下班前",
                confidence=0.72,
                status=TodoStatus.OPEN,
                created_at=now - timedelta(minutes=10),
            ),
            TodoItem(
                id="todo-3",
                message_id="msg-3",
                conversation_id="conv-project",
                conversation_title="项目讨论组（示例）",
                text="明天上午 10 点去 A 会议室开会",
                due_at=base + timedelta(days=1, hours=-base.hour + 10),
                due_text="明天上午 10 点",
                confidence=0.48,
                status=TodoStatus.OPEN,
                created_at=now - timedelta(minutes=10),
            ),
            TodoItem(
                id="todo-4",
                message_id="msg-1",
                conversation_id="conv-zhang",
                conversation_title="小张（示例）",
                text="给小张回一句「收到，马上处理」",
                due_text=None,
                confidence=0.9,
                status=TodoStatus.DONE,
                created_at=now - timedelta(minutes=10),
            ),
        ]
        self._drafts = {}
        self._summary = None
        self._demo_active = True
        self._last_scan_at = None
        self._last_scan_status = None
        self._paused = False

    # ------------------------------------------------------------------
    # 账号
    # ------------------------------------------------------------------
    def list_accounts(self) -> list[Account]:
        self._record("list_accounts")
        self._maybe_fail("list_accounts")
        return list(self._accounts)

    def add_qq_account(self, display_name: str, allowlist: list[str]) -> Account:
        self._record("add_qq_account", display_name, list(allowlist))
        self._maybe_fail("add_qq_account")
        account = Account(
            id=f"acc-qq-{len(self._accounts) + 1}",
            kind=AccountKind.QQ,
            display_name=display_name,
            enabled=True,
            created_at=_now(),
            identifier="",
        )
        self._accounts.append(account)
        for index, name in enumerate(allowlist, start=1):
            if not name.strip():
                continue
            self._conversations.append(
                Conversation(
                    id=f"conv-allow-{len(self._conversations) + index}",
                    account_id=account.id,
                    kind=ConversationKind.PRIVATE,
                    title=name.strip(),
                    external_key=name.strip(),
                    enabled=True,
                    priority=Priority.NORMAL,
                )
            )
        return account

    def add_mail_account(self, email: str, display_name: str, folder: str = "INBOX") -> Account:
        self._record("add_mail_account", email, display_name, folder)
        self._maybe_fail("add_mail_account")
        account = Account(
            id=f"acc-mail-{len(self._accounts) + 1}",
            kind=AccountKind.EMAIL,
            display_name=display_name or email,
            enabled=True,
            created_at=_now(),
            identifier=email,
        )
        self._accounts.append(account)
        self._conversations.append(
            Conversation(
                id=f"conv-mail-{len(self._conversations) + 1}",
                account_id=account.id,
                kind=ConversationKind.MAIL,
                title=f"QQ 邮箱 · {folder}（示例）",
                external_key=folder,
                enabled=True,
            )
        )
        return account

    def set_account_enabled(self, account_id: str, enabled: bool) -> None:
        self._record("set_account_enabled", account_id, enabled)
        self._maybe_fail("set_account_enabled")
        self._accounts = [
            replace(account, enabled=enabled) if account.id == account_id else account
            for account in self._accounts
        ]

    def remove_account(self, account_id: str, delete_messages: bool = True) -> None:
        self._record("remove_account", account_id, delete_messages)
        self._maybe_fail("remove_account")
        self._accounts = [account for account in self._accounts if account.id != account_id]
        conv_ids = {c.id for c in self._conversations if c.account_id == account_id}
        self._conversations = [c for c in self._conversations if c.account_id != account_id]
        if delete_messages:
            self._messages = [m for m in self._messages if m.conversation_id not in conv_ids]
            self._todos = [t for t in self._todos if t.conversation_id not in conv_ids]
            self._summary = None

    # ------------------------------------------------------------------
    # 会话授权
    # ------------------------------------------------------------------
    def list_conversations(self, account_id: str | None = None) -> list[Conversation]:
        self._record("list_conversations", account_id)
        self._maybe_fail("list_conversations")
        if account_id is None:
            return list(self._conversations)
        return [c for c in self._conversations if c.account_id == account_id]

    def set_conversation_enabled(self, conversation_id: str, enabled: bool) -> None:
        self._record("set_conversation_enabled", conversation_id, enabled)
        self._maybe_fail("set_conversation_enabled")
        self._conversations = [
            replace(conversation, enabled=enabled)
            if conversation.id == conversation_id
            else conversation
            for conversation in self._conversations
        ]

    def set_conversation_priority(self, conversation_id: str, priority: Priority) -> None:
        self._record("set_conversation_priority", conversation_id, priority)
        self._conversations = [
            replace(conversation, priority=priority)
            if conversation.id == conversation_id
            else conversation
            for conversation in self._conversations
        ]

    def refresh_conversations(self, account_id: str) -> ConnectionCheck:
        self._record("refresh_conversations", account_id)
        self._maybe_fail("refresh_conversations")
        count = len([c for c in self._conversations if c.account_id == account_id])
        return ConnectionCheck(
            ok=True,
            detail=f"（演示）已刷新 {count} 个会话。",
            latency_ms=120,
            discovered={"conversations": count},
        )

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    def test_account(self, account_id: str) -> ConnectionCheck:
        self._record("test_account", account_id)
        return ConnectionCheck(ok=True, detail="（演示）账号连接正常。", latency_ms=60)

    def run_scan(
        self,
        account_id: str | None = None,
        *,
        trigger: str = "manual",
        foreground_ack: bool = False,
    ) -> list[CollectionResult]:
        self._record("run_scan", account_id, trigger=trigger, foreground_ack=foreground_ack)
        self._maybe_fail("run_scan")
        now = _now()
        results: list[CollectionResult] = []
        for account in self._accounts:
            if not account.enabled:
                continue
            if account_id and account.id != account_id:
                continue
            convs = [c for c in self._conversations if c.account_id == account.id and c.enabled]
            new_count = 3 if account.kind == AccountKind.QQ else 1
            results.append(
                CollectionResult(
                    scan_id=f"scan-{account.id}-{int(now.timestamp())}",
                    account_id=account.id,
                    status=ScanStatus.OK,
                    messages_seen=new_count + 2,
                    messages_new=new_count,
                    conversations_scanned=len(convs),
                    warnings=(),
                    started_at=now - timedelta(seconds=2),
                    finished_at=now,
                    foreground_used=foreground_ack and account.kind == AccountKind.QQ,
                    duration_ms=1800,
                )
            )
        self._last_scan_at = now
        self._last_scan_status = ScanStatus.OK.value
        self._last_scan_error = None
        return results

    def collection_status(self) -> dict[str, Any]:
        self._record("collection_status")
        return {
            "paused": self._paused,
            "running": False,
            "last_scan_at": self._last_scan_at,
            "last_scan_status": self._last_scan_status,
            "last_error": self._last_scan_error,
        }

    def pause_collection(self) -> None:
        self._record("pause_collection")
        self._maybe_fail("pause_collection")
        self._paused = True
        self.settings = replace(self.settings, collection_paused=True)

    def resume_collection(self) -> None:
        self._record("resume_collection")
        self._maybe_fail("resume_collection")
        self._paused = False
        self.settings = replace(self.settings, collection_paused=False)

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    def generate_daily_summary(self, day: date | None = None) -> DailySummary:
        self._record("generate_daily_summary", day)
        self._maybe_fail("generate_daily_summary")
        summary = self._build_summary(day)
        self._summary = summary
        return summary

    def get_cached_summary(self, day: date | None = None) -> DailySummary | None:
        self._record("get_cached_summary", day)
        self._maybe_fail("get_cached_summary")
        return self._summary

    def _build_summary(self, day: date | None = None) -> DailySummary:
        # 默认使用「演示日期」而不是系统实时日期：避免测试恰好在零点前后运行时，
        # 演示消息（前一天 20:00 附近）与「今天」不一致导致计数为 0。
        target = day or self.demo_day()
        messages = [
            message
            for message in self._messages
            if (message.timestamp or message.collected_at).astimezone().date() == target
        ]
        important = tuple(m for m in messages if m.category == Category.IMPORTANT)
        normal = tuple(m for m in messages if m.category == Category.NORMAL and not m.is_self)
        fyi = tuple(m for m in messages if m.category == Category.FYI)
        todos = tuple(todo for todo in self._todos if todo.status == TodoStatus.OPEN)
        counts = {
            "important": len(important),
            "normal": len(normal),
            "fyi": len(fyi),
            "todo_open": len(todos),
        }
        if important:
            narrative = (
                f"今天有 {len(important)} 件重要的事需要你处理：小张请你在下午 3 点前发活动方案，"
                f"活动负责人请你在周五下班前确认场地和人数。其余 {len(normal)} 条是普通交流，"
                f"{len(fyi)} 条是通知/公告，可以稍后再看。"
            )
        else:
            narrative = "今天暂时没有需要特别处理的消息，可以先忙自己的事情。"
        caveats = (
            "QQ 前台读取会短暂打断你正在进行的电脑操作；本次只读取了你勾选的会话。",
            "摘要由本机规则生成，不联网；分类和待办是机器判断，重要事项请以原文为准。",
        )
        conversation_ids = {message.conversation_id for message in messages}
        return DailySummary(
            day=target,
            generated_at=self._now_dt,
            counts=counts,
            important=important,
            normal=normal,
            fyi=fyi,
            todos=todos,
            conversations_touched=len(conversation_ids),
            narrative=narrative,
            caveats=caveats,
            scan_status=ScanStatus.OK,
            is_demo=self._demo_active,
        )

    # ------------------------------------------------------------------
    # 邮箱收件箱（P0-15：真实邮件 GUI 验收核心路径）
    # ------------------------------------------------------------------
    def _mail_accounts(self) -> list[Account]:
        return [account for account in self._accounts if account.kind == AccountKind.EMAIL]

    def list_mail_accounts(self) -> list[Account]:
        self._record("list_mail_accounts")
        self._maybe_fail("list_mail_accounts")
        return list(self._mail_accounts())

    def sync_mail(
        self,
        account_id: str | None = None,
        *,
        user_confirmed: bool = False,
    ) -> list[CollectionResult]:
        self._record("sync_mail", account_id, user_confirmed=user_confirmed)
        self._maybe_fail("sync_mail")
        if not user_confirmed:
            raise PermissionError("同步邮件需要用户明确确认（user_confirmed=True）。")
        if self._paused:
            self._paused = False
            self.settings = replace(self.settings, collection_paused=False)
        self._last_scan_at = self._now_dt
        self._last_scan_status = ScanStatus.OK.value
        self._last_scan_error = None
        accounts = self._mail_accounts()
        if account_id:
            accounts = [account for account in accounts if account.id == account_id]
        inbox = self.list_inbox_messages(account_id=account_id)
        results: list[CollectionResult] = []
        for account in accounts:
            count = len([m for m in inbox if m.account_id == account.id])
            results.append(
                CollectionResult(
                    scan_id=f"mail-sync-{len(self.calls)}",
                    account_id=account.id,
                    status=ScanStatus.OK,
                    messages_seen=count,
                    messages_new=count,
                    conversations_scanned=1,
                    started_at=self._now_dt,
                    finished_at=self._now_dt,
                    duration_ms=120,
                    warnings=(),
                )
            )
        return results

    def list_inbox_messages(
        self,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[Message]:
        self._record("list_inbox_messages", account_id, limit)
        self._maybe_fail("list_inbox_messages")
        ids = {account.id for account in self._mail_accounts()}
        if account_id:
            ids &= {account_id}
        messages = [message for message in self._messages if message.account_id in ids]
        messages.sort(key=lambda message: (message.timestamp or message.collected_at), reverse=True)
        return messages[: max(0, limit)]

    def generate_latest_mail_summary(self) -> DailySummary | None:
        self._record("generate_latest_mail_summary")
        self._maybe_fail("generate_latest_mail_summary")
        inbox = self.list_inbox_messages(limit=1000)
        if not inbox:
            return None
        latest_day = max(
            (message.timestamp or message.collected_at).astimezone().date() for message in inbox
        )
        summary = self._build_summary(latest_day)
        summary = replace(summary, is_demo=all(message.is_demo for message in inbox))
        self._summary = summary
        return summary

    def mail_inbox_status(self, account_id: str | None = None) -> dict[str, Any]:
        self._record("mail_inbox_status", account_id)
        self._maybe_fail("mail_inbox_status")
        accounts = self._mail_accounts()
        if account_id:
            accounts = [account for account in accounts if account.id == account_id]
        inbox = self.list_inbox_messages(account_id=account_id)
        email = accounts[0].identifier if accounts else None
        return {
            "account_count": len(accounts),
            "email": email,
            "credential_saved": bool(email and email in self._mail_credentials),
            "mail_read_consent": bool(self.settings.mail_read_consent),
            "paused": self._paused,
            "message_count": len(inbox),
            "real_count": sum(1 for message in inbox if not message.is_demo),
            "demo_count": sum(1 for message in inbox if message.is_demo),
            "last_sync_at": self._last_scan_at,
            "last_error": self._last_scan_error,
        }

    def seed_inbox_mail(
        self,
        *,
        sender: str = "真实发件人",
        subject: str = "真实邮件主题",
        text: str = "真实邮件正文（合成测试内容）",
        timestamp: datetime | None = None,
        is_demo: bool = False,
        attachment_names: tuple[str, ...] = (),
    ) -> Message:
        """测试/截图辅助：往收件箱里放一条（默认真实标记的）合成邮件。"""
        accounts = self._mail_accounts()
        if not accounts:
            account = self.add_mail_account("real@qq.com", "真实邮箱（合成）")
        else:
            account = accounts[0]
        conversation = next(
            (c for c in self._conversations if c.account_id == account.id),
            None,
        )
        if conversation is None:
            conversation = Conversation(
                id=f"conv-mail-real-{len(self._conversations) + 1}",
                account_id=account.id,
                kind=ConversationKind.MAIL,
                title=f"QQ 邮箱 · INBOX（{account.identifier or account.display_name}）",
                external_key="INBOX",
                enabled=True,
            )
            self._conversations.append(conversation)
        # 不传 timestamp 时使用演示时钟本身，确保“今天的邮件”落在演示日期当天，
        # 避免在凌晨运行测试时 now-2h 跨到前一天导致摘要日期断言失败。
        when = timestamp or self._now_dt
        message = Message(
            id=f"msg-inbox-{len(self._messages) + 1}",
            account_id=account.id,
            conversation_id=conversation.id,
            conversation_title=conversation.title,
            conversation_kind=ConversationKind.MAIL,
            sender=sender,
            is_self=False,
            text=text,
            timestamp=when,
            timestamp_source=TimestampSource.COLLECTED,
            collected_at=when,
            category=Category.NORMAL,
            category_reasons=(),
            content_hash=f"hash-inbox-{len(self._messages) + 1}",
            source_locator=f"IMAP INBOX · UID {9000 + len(self._messages)}",
            external_id=f"uid-{9000 + len(self._messages)}",
            subject=subject,
            is_demo=is_demo,
            attachment_names=tuple(attachment_names),
        )
        self._messages.append(message)
        return message

    # ------------------------------------------------------------------
    # 消息 / 来源
    # ------------------------------------------------------------------
    def list_messages(
        self,
        day: date | None = None,
        category: Category | None = None,
        limit: int = 200,
        work_status: MessageWorkStatus | None = None,
    ) -> list[Message]:
        self._record("list_messages", day, category, limit, work_status)
        self._maybe_fail("list_messages")
        messages = list(self._messages)
        if day is not None:
            messages = [
                m for m in messages if (m.timestamp or m.collected_at).astimezone().date() == day
            ]
        if category is not None:
            messages = [m for m in messages if m.category == category]
        if work_status is not None:
            try:
                target_status = MessageWorkStatus(work_status)
            except ValueError:
                target_status = None
            if target_status is not None:
                messages = [m for m in messages if m.work_status == target_status]
        messages.sort(key=lambda m: (m.timestamp or m.collected_at), reverse=True)
        return messages[: max(0, limit)]

    def set_message_work_status(self, message_id: str, status: MessageWorkStatus) -> None:
        self._maybe_fail("set_message_work_status")
        try:
            target_status = MessageWorkStatus(status)
        except ValueError as exc:
            raise ValueError(f"未知的处理状态：{status}") from exc
        self._record("set_message_work_status", message_id, target_status.value)
        for index, message in enumerate(self._messages):
            if message.id == message_id:
                self._messages[index] = replace(message, work_status=target_status)
                return
        raise ValueError(f"未找到消息：{message_id}")

    def message_work_status_counts(self) -> dict[str, int]:
        self._record("message_work_status_counts")
        self._maybe_fail("message_work_status_counts")
        counts = {status.value: 0 for status in MessageWorkStatus}
        for message in self._messages:
            try:
                counts[MessageWorkStatus(message.work_status).value] += 1
            except ValueError:
                counts[MessageWorkStatus.UNHANDLED.value] += 1
        counts["total"] = len(self._messages)
        counts["important_unhandled"] = sum(
            1
            for message in self._messages
            if message.category == Category.IMPORTANT
            and (
                MessageWorkStatus(getattr(message, "work_status", MessageWorkStatus.UNHANDLED))
                in (MessageWorkStatus.UNHANDLED, MessageWorkStatus.NEEDS_REPLY)
            )
        )
        return counts

    def set_message_category(self, message_id: str, category: Category) -> None:
        self._maybe_fail("set_message_category")
        try:
            target = Category(category)
        except ValueError as exc:
            raise ValueError(f"未知的重要程度：{category}") from exc
        self._record("set_message_category", message_id, target.value)
        for index, message in enumerate(self._messages):
            if message.id == message_id:
                self._messages[index] = replace(message, category=target)
                return
        raise ValueError(f"未找到消息：{message_id}")

    # ------------------------------------------------------------------
    # AI 语义摘要 / 每日简报 / 语义草稿（合成实现）
    # ------------------------------------------------------------------
    @staticmethod
    def _semantic_provider(use_external: bool) -> str:
        return "external_ai" if use_external else "local_rules"

    def generate_conversation_semantic(
        self, conversation_id: str, *, use_external: bool = False
    ) -> dict[str, Any]:
        self._record("generate_conversation_semantic", conversation_id, use_external=use_external)
        self._maybe_fail("generate_conversation_semantic")
        messages = [m for m in self._messages if m.conversation_id == conversation_id]
        if not messages:
            raise ValueError("没有找到该会话的消息。")
        provider = self._semantic_provider(use_external)
        source_ids = [m.id for m in messages[:5]]
        return {
            "ok": True,
            "provider": provider,
            "conversation_id": conversation_id,
            "conversation_title": messages[0].conversation_title,
            "overview": "（合成）这个会话主要围绕活动安排与截止时间，需要你确认场地和人数。",
            "sections": [
                {
                    "title": "问题",
                    "items": [
                        {"text": "（合成）对方在等你的活动方案确认。", "source_message_ids": source_ids[:1]}
                    ],
                },
                {
                    "title": "任务/截止",
                    "items": [
                        {"text": "（合成）周五下班前确认场地和人数。", "source_message_ids": source_ids[-1:]}
                    ],
                },
                {
                    "title": "承诺",
                    "items": [{"text": "（合成）你回复过「收到，下午处理」。", "source_message_ids": []}],
                },
                {
                    "title": "未决/闲聊",
                    "items": [{"text": "（合成）其余为闲聊，可稍后看。", "source_message_ids": []}],
                },
            ],
            "source_message_ids": source_ids,
            "uncertain": ["（合成）语义摘要由规则生成，重要事项请以原文为准。"],
            "usage": {
                "calls": 1,
                "tokens": 123 if use_external else 0,
                "mode": "external" if use_external else "local",
            },
            "error": None,
        }

    def generate_daily_brief(self, *, day: date | None = None, use_external: bool = False) -> dict[str, Any]:
        self._record("generate_daily_brief", day, use_external=use_external)
        self._maybe_fail("generate_daily_brief")
        messages = list(self._messages)
        if day is not None:
            messages = [m for m in messages if (m.timestamp or m.collected_at).astimezone().date() == day]
        if not messages:
            return {
                "ok": False,
                "provider": self._semantic_provider(use_external),
                "sections": [],
                "source_message_ids": [],
                "uncertain": [],
                "usage": {},
                "error": "今天没有可生成简报的消息。",
            }
        important = [m for m in messages if m.category == Category.IMPORTANT]
        source_ids = [m.id for m in messages[:6]]
        return {
            "ok": True,
            "provider": self._semantic_provider(use_external),
            "day": (day or messages[0].timestamp.date()).isoformat(),
            "sections": [
                {
                    "title": "今天必须处理",
                    "items": [
                        {"text": "（合成）活动方案确认（重要）。", "source_message_ids": [m.id for m in important[:1]]}
                    ],
                },
                {
                    "title": "近期",
                    "items": [{"text": "（合成）周五前确认场地和人数。", "source_message_ids": source_ids[-1:]}],
                },
                {
                    "title": "可集中回复",
                    "items": [{"text": "（合成）下午统一回复讨论组。", "source_message_ids": []}],
                },
                {
                    "title": "仅供了解",
                    "items": [{"text": "（合成）群公告与系统通知。", "source_message_ids": []}],
                },
            ],
            "source_message_ids": source_ids,
            "uncertain": ["（合成）每日简报由规则生成，请核对原文。"],
            "caveats": ["（合成）不会发送任何消息。"],
            "usage": {
                "calls": 1,
                "tokens": 200 if use_external else 0,
                "mode": "external" if use_external else "local",
            },
            "error": None,
        }

    def generate_semantic_draft(
        self,
        message_id: str,
        *,
        style: str = "友好",
        use_external: bool = False,
    ) -> dict[str, Any]:
        self._record("generate_semantic_draft", message_id, style, use_external=use_external)
        self._maybe_fail("generate_semantic_draft")
        message = next((m for m in self._messages if m.id == message_id), None)
        if message is None:
            raise ValueError(f"未找到消息：{message_id}")
        valid_styles = {"正式", "简洁", "友好"}
        if style not in valid_styles:
            return {"ok": False, "error": f"不支持的风格：{style}", "provider": None}
        drafts = {
            "正式": "（合成）您好，关于活动方案，我方将在今天下午 3 点前确认场地与人数，感谢配合。",
            "简洁": "（合成）收到，下午 3 点前确认场地和人数。",
            "友好": "（合成）收到啦，我们下午 3 点前把场地和人数确认好，谢谢提醒！",
        }
        return {
            "ok": True,
            "provider": self._semantic_provider(use_external),
            "message_id": message_id,
            "style": style,
            "draft": drafts[style],
            "context_message_ids": [message_id],
            "uncertain": ["（合成）草稿仅供参考，请自行核对后复制。"],
            "usage": {
                "calls": 1,
                "tokens": 80 if use_external else 0,
                "mode": "external" if use_external else "local",
            },
            "error": None,
        }

    def get_source_detail(self, message_id: str) -> SourceDetail:
        self._record("get_source_detail", message_id)
        self._maybe_fail("get_source_detail")
        message = next((m for m in self._messages if m.id == message_id), None)
        if message is None:
            raise ValueError(f"未找到消息：{message_id}")
        account = next((a for a in self._accounts if a.id == message.account_id), None)
        kind = account.kind if account is not None else AccountKind.QQ
        if kind == AccountKind.EMAIL:
            origin = "IMAP 收件箱 · 邮件头时间（示例）"
        else:
            origin = "QQ 前台聊天窗口 · 可见气泡（示例）"
        raw_time_text = (
            f"{message.timestamp:%H:%M}" if message.timestamp is not None else None
        )
        return SourceDetail(
            message_id=message.id,
            account_kind=kind,
            conversation_title=message.conversation_title,
            sender=message.sender,
            raw_time_text=raw_time_text,
            collected_at=message.collected_at,
            timestamp_source=message.timestamp_source,
            external_id=message.external_id,
            locator=message.source_locator,
            origin_label=origin,
        )

    # ------------------------------------------------------------------
    # 待办
    # ------------------------------------------------------------------
    def list_todos(self, day: date | None = None, include_done: bool = False) -> list[TodoItem]:
        self._record("list_todos", day, include_done)
        self._maybe_fail("list_todos")
        todos = list(self._todos)
        if not include_done:
            todos = [todo for todo in todos if todo.status == TodoStatus.OPEN]
        todos.sort(key=lambda todo: (todo.due_at is None, todo.due_at or _now()))
        return todos

    def update_todo_status(self, todo_id: str, status: TodoStatus) -> None:
        self._record("update_todo_status", todo_id, status)
        self._maybe_fail("update_todo_status")
        self._todos = [
            replace(todo, status=status) if todo.id == todo_id else todo for todo in self._todos
        ]

    # ------------------------------------------------------------------
    # 草稿
    # ------------------------------------------------------------------
    def generate_drafts(self, message_id: str, styles: list[str] | None = None) -> list[ReplyDraft]:
        self._record("generate_drafts", message_id, styles)
        self._maybe_fail("generate_drafts")
        message = next((m for m in self._messages if m.id == message_id), None)
        if message is None:
            raise ValueError(f"未找到消息：{message_id}")
        style_list = list(styles) if styles else ["简洁", "礼貌"]
        external = bool(self.settings.external_ai_consent)
        kind = DraftKind.EXTERNAL_LLM if external else DraftKind.LOCAL_RULES
        model_label = self.settings.external_ai_model if external else "本机规则"
        texts = {
            "简洁": "好的，我马上处理，下午 3 点前发给你。",
            "礼貌": "收到，感谢提醒。我会在下午 3 点前把活动方案发给您，如果有变动会提前说明。",
            "详细": "收到。我先确认一下手头的资料，下午 3 点前把活动方案整理好发给你；如果时间来不及，我会提前告诉你。",
        }
        drafts = [
            ReplyDraft(
                id=f"draft-{message_id}-{index}",
                message_id=message_id,
                kind=kind,
                style=style,
                text=texts.get(style, texts["简洁"]),
                created_at=_now(),
                model_label=model_label,
                requires_external_consent=external,
            )
            for index, style in enumerate(style_list)
        ]
        self._drafts[message_id] = drafts
        return list(drafts)

    def save_draft_edit(self, draft_id: str, text: str) -> None:
        self._record("save_draft_edit", draft_id, text)
        self._maybe_fail("save_draft_edit")
        for message_id, drafts in self._drafts.items():
            for index, draft in enumerate(drafts):
                if draft.id == draft_id:
                    drafts[index] = replace(draft, edited_text=text)
                    self._drafts[message_id] = drafts
                    return
        raise ValueError(f"未找到草稿：{draft_id}")

    def list_drafts(self, message_id: str) -> list[ReplyDraft]:
        self._record("list_drafts", message_id)
        self._maybe_fail("list_drafts")
        return list(self._drafts.get(message_id, []))

    # ------------------------------------------------------------------
    # 设置 / 权限 / 隐私
    # ------------------------------------------------------------------
    def get_settings(self) -> AppSettings:
        self._record("get_settings")
        self._maybe_fail("get_settings")
        return self.settings

    def update_settings(self, **changes: Any) -> AppSettings:
        self._record("update_settings", **changes)
        self._maybe_fail("update_settings")
        known = {field for field in AppSettings.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(changes) - known
        if unknown:
            raise TypeError(f"未知设置项：{sorted(unknown)}")
        self.settings = replace(self.settings, **changes)
        return self.settings

    def grant_consent(self, scope: ConsentScope, *, confirmation_text: str = "") -> None:
        self._record("grant_consent", scope, confirmation_text=confirmation_text)
        self._maybe_fail("grant_consent")
        updates: dict[str, bool] = {}
        if scope == ConsentScope.QQ_READ:
            updates["qq_read_consent"] = True
        elif scope == ConsentScope.MAIL_READ:
            updates["mail_read_consent"] = True
        elif scope == ConsentScope.EXTERNAL_AI:
            updates["external_ai_consent"] = True
        self.settings = replace(self.settings, **updates)
        self._consent_history.append(
            {
                "scope": scope.value,
                "action": "granted",
                "granted_at": _now(),
                "confirmation_text": confirmation_text,
            }
        )

    def revoke_consent(self, scope: ConsentScope) -> None:
        self._record("revoke_consent", scope)
        self._maybe_fail("revoke_consent")
        updates: dict[str, bool] = {}
        if scope == ConsentScope.QQ_READ:
            updates["qq_read_consent"] = False
        elif scope == ConsentScope.MAIL_READ:
            updates["mail_read_consent"] = False
        elif scope == ConsentScope.EXTERNAL_AI:
            updates["external_ai_consent"] = False
        self.settings = replace(self.settings, **updates)
        self._consent_history.append(
            {"scope": scope.value, "action": "revoked", "granted_at": _now(), "confirmation_text": ""}
        )

    def list_consent_history(self) -> list[dict[str, Any]]:
        self._record("list_consent_history")
        self._maybe_fail("list_consent_history")
        return list(self._consent_history)

    # ------------------------------------------------------------------
    # 邮箱凭据（只记录长度，不保存明文）
    # ------------------------------------------------------------------
    def set_mail_credential(self, email: str, auth_code: str) -> None:
        self._record("set_mail_credential", email, len(auth_code))
        self._maybe_fail("set_mail_credential")
        if not auth_code:
            raise ValueError("授权码不能为空")
        self._mail_credentials[email] = len(auth_code)

    def has_mail_credential(self, email: str) -> bool:
        self._record("has_mail_credential", email)
        return email in self._mail_credentials

    def delete_mail_credential(self, email: str) -> None:
        self._record("delete_mail_credential", email)
        self._mail_credentials.pop(email, None)

    def test_mail_credential(self, email: str) -> ConnectionCheck:
        self._record("test_mail_credential", email)
        self._maybe_fail("test_mail_credential")
        ok = email in self._mail_credentials
        return ConnectionCheck(
            ok=ok,
            detail="（演示）登录成功，IMAP 连接正常。" if ok else "（演示）还没有保存授权码。",
            latency_ms=180 if ok else None,
        )

    # ------------------------------------------------------------------
    # 外部 AI
    # ------------------------------------------------------------------
    def test_external_ai(self) -> ConnectionCheck:
        self._record("test_external_ai")
        ok = self.has_external_ai_key() and self.settings.external_ai_consent
        return ConnectionCheck(ok=ok, detail="（演示）外部 AI 可用。" if ok else "（演示）外部 AI 未启用。")

    def has_external_ai_key(self) -> bool:
        self._record("has_external_ai_key")
        return self._external_ai_key_length is not None

    def set_external_ai_key(self, api_key: str) -> None:
        self._record("set_external_ai_key", len(api_key))
        if not api_key:
            raise ValueError("API 密钥不能为空")
        self._external_ai_key_length = len(api_key)

    # ------------------------------------------------------------------
    # 演示 / 测试
    # ------------------------------------------------------------------
    def load_demo_data(self) -> None:
        self._record("load_demo_data")
        self._maybe_fail("load_demo_data")
        self._load_demo_data()
        self.settings = replace(self.settings, collection_paused=self._paused)

    def clear_demo_data(self) -> None:
        self._record("clear_demo_data")
        self._maybe_fail("clear_demo_data")
        self._messages = []
        self._todos = []
        self._drafts = {}
        self._conversations = []
        self._accounts = []
        self._summary = None
        self._demo_active = False
        self._paused = True
        self.settings = replace(self.settings, collection_paused=True)

    def is_demo_active(self) -> bool:
        self._record("is_demo_active")
        self._maybe_fail("is_demo_active")
        return self._demo_active

    # ------------------------------------------------------------------
    # 实验性：用户主动导入聊天文本（不联网、不读 QQ）
    # ------------------------------------------------------------------
    _IMPORT_LINE = re.compile(
        r"^(?:\[(?P<ts>[^\]]{1,40})\]\s*)?(?P<sender>[^:：]{1,40})[:：]\s*(?P<body>.+)$"
    )

    def _import_scope(self) -> tuple[Account, Conversation]:
        account = next(
            (item for item in self._accounts if item.kind == AccountKind.IMPORT),
            None,
        )
        if account is None:
            account = Account(
                id="acc-import",
                kind=AccountKind.IMPORT,
                display_name="导入的聊天文本（实验）",
                enabled=True,
                created_at=self._now_dt,
                identifier="",
            )
            self._accounts.append(account)
        conversation = next(
            (item for item in self._conversations if item.account_id == account.id),
            None,
        )
        if conversation is None:
            conversation = Conversation(
                id="conv-import",
                account_id=account.id,
                kind=ConversationKind.GROUP,
                title="导入的聊天文本（实验）",
                external_key="import",
                enabled=True,
            )
            self._conversations.append(conversation)
        return account, conversation

    @classmethod
    def _parse_import_lines(
        cls, text: str
    ) -> tuple[list[tuple[str, str, datetime | None]], int]:
        parsed: list[tuple[str, str, datetime | None]] = []
        skipped = 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = cls._IMPORT_LINE.match(line)
            if match is None:
                skipped += 1
                continue
            stamp: datetime | None = None
            raw_ts = match.group("ts")
            if raw_ts:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
                    try:
                        stamp = datetime.strptime(raw_ts.strip(), fmt).replace(
                            tzinfo=datetime.now().astimezone().tzinfo
                        )
                        break
                    except ValueError:
                        continue
            parsed.append((match.group("sender").strip(), match.group("body").strip(), stamp))
        return parsed, skipped

    def import_chat_text(
        self,
        text: str,
        *,
        source_name: str = "",
        options: dict[str, Any] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        self._record("import_chat_text", source_name, dry_run=dry_run)
        self._maybe_fail("import_chat_text")
        raw = (text or "").strip()
        base = {
            "ok": False,
            "dry_run": bool(dry_run),
            "source_name": source_name,
            "format": "",
            "conversation_count": 0,
            "message_count": 0,
            "imported_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
            "warnings": [],
            "error": None,
            "preview": [],
        }
        if not raw:
            base["error"] = "聊天文本为空：请先粘贴或选择文本文件。"
            return base
        account, conversation = self._import_scope()
        parsed, skipped = self._parse_import_lines(raw)
        warnings: list[dict[str, Any]] = []
        if skipped:
            warnings.append(
                {"code": "skipped_lines", "message": "有若干行无法识别，已跳过", "count": skipped}
            )
        if not parsed:
            base.update(
                {
                    "skipped_count": skipped,
                    "warnings": warnings,
                    "error": "没有识别到可导入的聊天行：请确认格式类似「张三: 你好」或「[2026-11-01 10:00] 张三: 你好」。",
                }
            )
            return base
        incoming: list[tuple[str, str, datetime | None]] = []
        duplicate_count = 0
        for sender, body, stamp in parsed:
            key = hashlib.sha256(
                f"{conversation.id}|{sender}|{body}|{stamp.isoformat() if stamp else ''}".encode("utf-8")
            ).hexdigest()
            if key in self._imported_hashes:
                duplicate_count += 1
                continue
            incoming.append((sender, body, stamp))
        if not dry_run:
            for index, (sender, body, stamp) in enumerate(incoming):
                when = stamp or (self._now_dt - timedelta(minutes=len(incoming) - index))
                self._messages.append(
                    Message(
                        id=f"msg-import-{len(self._messages) + 1}",
                        account_id=account.id,
                        conversation_id=conversation.id,
                        conversation_title=conversation.title,
                        conversation_kind=conversation.kind,
                        sender=sender,
                        is_self=False,
                        text=body,
                        timestamp=when,
                        timestamp_source=TimestampSource.COLLECTED,
                        collected_at=self._now_dt,
                        category=Category.NORMAL,
                        category_reasons=(),
                        content_hash=hashlib.sha256(
                            f"{sender}|{body}|{when.isoformat()}".encode("utf-8")
                        ).hexdigest(),
                        source_locator=source_name or "用户导入的聊天文本",
                        external_id=f"import-{len(self._messages) + 1}",
                        subject="",
                        is_demo=False,
                    )
                )
                self._imported_hashes.add(
                    hashlib.sha256(
                        f"{conversation.id}|{sender}|{body}|{stamp.isoformat() if stamp else ''}".encode("utf-8")
                    ).hexdigest()
                )
        preview = [
            {
                "conversation": conversation.title,
                "sender": sender,
                "timestamp": stamp.isoformat() if stamp else "",
                "length": len(body),
                "status": "已导入" if not dry_run else "将导入",
            }
            for sender, body, stamp in parsed[:200]
        ]
        base.update(
            {
                "ok": True,
                "format": "文本行（发送者: 内容）",
                "conversation_count": 1,
                "message_count": len(parsed),
                "imported_count": 0 if dry_run else len(incoming),
                "duplicate_count": duplicate_count,
                "skipped_count": skipped,
                "warnings": warnings,
                "preview": preview,
            }
        )
        return base

    # ------------------------------------------------------------------
    # 一键整理所有消息（P0 一键编排）
    # ------------------------------------------------------------------
    def organize_all(self, *, foreground_ack: bool = False) -> dict[str, Any]:
        self._record("organize_all", foreground_ack=foreground_ack)
        self._maybe_fail("organize_all")
        enabled = [account for account in self._accounts if account.enabled]
        configured_qq = any(account.kind == AccountKind.QQ for account in enabled)
        configured_mail = any(account.kind == AccountKind.EMAIL for account in enabled)
        errors: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        history_total = (
            int(self.organize_history_total)
            if self.organize_history_total is not None
            else len(self._messages)
        )
        latest_day = self.organize_latest_day
        if latest_day is None:
            moments = [m.timestamp or m.collected_at for m in self._messages]
            latest_day = max((moment.date() for moment in moments if moment is not None), default=None)
        if not configured_qq and not configured_mail:
            return {
                "ok": False,
                "configured": {"qq": False, "mail": False},
                "results": [],
                "summary": None,
                "qq_success": 0,
                "qq_failed": 0,
                "mail_success": 0,
                "mail_failed": 0,
                "errors": [],
                "details": [],
                "qq_authorized": 0,
                "qq_sessions_scanned": 0,
                "qq_sessions_failed": 0,
                "qq_failure_hint": "",
                "history_total": history_total,
                "latest_message_day": latest_day.isoformat() if latest_day else None,
                "duration_ms": 0,
                "error": "还没有可整理的账号：请先完成 QQ/邮箱授权。",
            }
        qq_success = self.organize_qq_success if configured_qq else 0
        mail_success = self.organize_mail_success if configured_mail else 0
        qq_failed = self.organize_qq_failed if configured_qq else 0
        mail_failed = self.organize_mail_failed if configured_mail else 0
        for _ in range(qq_success):
            results.append({"source": "qq", "ok": True})
        for _ in range(mail_success):
            results.append({"source": "mail", "ok": True})
        for _ in range(qq_failed):
            results.append({"source": "qq", "ok": False})
        for _ in range(mail_failed):
            results.append({"source": "mail", "ok": False})
        qq_authorized = self.organize_qq_authorized if configured_qq else 0
        qq_scanned = self.organize_qq_sessions_scanned if configured_qq else 0
        qq_sessions_failed = (
            max(qq_failed, self.organize_qq_sessions_failed) if configured_qq else 0
        )
        qq_failure_hint = (
            self.organize_qq_failure_hint if configured_qq and qq_sessions_failed else ""
        )
        if qq_sessions_failed:
            errors.append(
                {
                    "source": "qq",
                    "message": qq_failure_hint
                    or "（演示）部分 QQ 会话整理失败，请确认 QQ 已登录且窗口未被遮挡。",
                }
            )
        if mail_failed:
            errors.append(
                {"source": "mail", "message": "（演示）部分邮箱整理失败，请检查网络或授权码。"}
            )
        details: list[dict[str, Any]] = []
        if configured_qq:
            details.append(
                {
                    "kind": "qq",
                    "status": "partial" if qq_sessions_failed else "ok",
                    "error": qq_failure_hint or None,
                    "warnings": [],
                    "messages_new": 0,
                    "conversations_scanned": qq_scanned,
                }
            )
        if configured_mail:
            details.append(
                {
                    "kind": "mail",
                    "status": "failed" if mail_failed else "ok",
                    "error": "（演示）邮箱同步失败" if mail_failed else None,
                    "warnings": [],
                    "messages_new": self.organize_mail_new if mail_success else 0,
                    "conversations_scanned": 1 if mail_success else 0,
                }
            )
        if not self._paused:
            self._last_scan_at = self._now_dt
        self._last_scan_status = ScanStatus.OK.value if not errors else ScanStatus.PARTIAL.value
        self._last_scan_error = errors[0]["message"] if errors else None
        summary = self._build_summary()
        if errors:
            summary = replace(summary, scan_status=ScanStatus.PARTIAL)
        self._summary = summary
        return {
            "ok": (qq_success + mail_success) > 0,
            "configured": {"qq": configured_qq, "mail": configured_mail},
            "results": results,
            "summary": summary,
            "qq_success": qq_success,
            "qq_failed": qq_failed,
            "mail_success": mail_success,
            "mail_failed": mail_failed,
            "errors": errors,
            "details": details,
            "qq_authorized": qq_authorized,
            "qq_sessions_scanned": qq_scanned,
            "qq_sessions_failed": qq_sessions_failed,
            "qq_failure_hint": qq_failure_hint,
            "history_total": history_total,
            "latest_message_day": latest_day.isoformat() if latest_day else None,
            "duration_ms": 123,
            "error": None if (qq_success + mail_success) > 0 else "这次没有成功整理任何来源。",
        }

    # ------------------------------------------------------------------
    # 实验性：一键读取当前 QQ 聊天（P0-25）
    # ------------------------------------------------------------------
    def quick_qq_read(self, *, user_confirmed: bool = False) -> dict[str, Any]:
        self._record("quick_qq_read", user_confirmed=user_confirmed)
        self._maybe_fail("quick_qq_read")
        if not user_confirmed:
            return {
                "ok": False,
                "added_account": False,
                "refresh_ok": False,
                "scan_status": "blocked",
                "messages_seen": 0,
                "messages_new": 0,
                "conversations_scanned": 0,
                "error": "需要用户确认后才能读取 QQ 当前聊天。",
            }
        qq_accounts = [
            account
            for account in self._accounts
            if account.kind == AccountKind.QQ and account.enabled
        ]
        added_account = False
        if not qq_accounts:
            account = self.add_qq_account("我的 QQ（一键读取）", ["当前聊天（示例）"])
            added_account = True
            qq_accounts = [account]
        account = qq_accounts[0]
        conversations = [
            conversation
            for conversation in self._conversations
            if conversation.account_id == account.id
        ]
        if not conversations:
            self._conversations.append(
                Conversation(
                    id=f"conv-quick-{len(self._conversations) + 1}",
                    account_id=account.id,
                    kind=ConversationKind.PRIVATE,
                    title="当前聊天（示例）",
                    external_key="当前聊天（示例）",
                    enabled=True,
                )
            )
        elif not any(conversation.enabled for conversation in conversations):
            self._conversations = [
                replace(conversation, enabled=True) if conversation.id == conversations[0].id else conversation
                for conversation in self._conversations
            ]
        self.settings = replace(
            self.settings,
            qq_read_experimental=True,
            qq_read_consent=True,
            collection_paused=False,
        )
        self._paused = False
        self._last_scan_at = self._now_dt
        self._last_scan_status = ScanStatus.OK.value
        self._last_scan_error = None
        return {
            "ok": True,
            "added_account": added_account,
            "refresh_ok": True,
            "scan_status": ScanStatus.OK.value,
            "messages_seen": 3,
            "messages_new": 2,
            "conversations_scanned": 1,
            "error": None,
        }

    # ------------------------------------------------------------------
    # 实验性：QQ 会话选择器（P0-2x，隐私优先）
    # ------------------------------------------------------------------
    def list_qq_visible_sessions(self, *, user_confirmed: bool = False) -> dict[str, Any]:
        self._record("list_qq_visible_sessions", user_confirmed=user_confirmed)
        self._maybe_fail("list_qq_visible_sessions")
        if not user_confirmed:
            return {
                "ok": False,
                "sessions": [],
                "error": "需要用户确认后才能读取 QQ 会话列表。",
            }
        qq_account_ids = {
            account.id for account in self._accounts if account.kind == AccountKind.QQ
        }
        sessions = [
            {"title": conversation.title, "len": 0, "unread": 0}
            for conversation in self._conversations
            if conversation.account_id in qq_account_ids
        ][:20]
        if not sessions:
            sessions = [{"title": "当前聊天（示例）", "len": 0, "unread": 0}]
        return {"ok": True, "sessions": sessions, "error": None}

    def apply_qq_session_selection(self, titles: list[str]) -> dict[str, Any]:
        cleaned = [str(title).strip() for title in (titles or []) if str(title).strip()]
        self._record("apply_qq_session_selection", len(cleaned))
        self._maybe_fail("apply_qq_session_selection")
        if not cleaned:
            return {
                "ok": False,
                "account_id": None,
                "selected_count": 0,
                "discovered_count": 0,
                "enabled_count": 0,
                "error": "没有勾选任何 QQ 会话。",
            }
        qq_accounts = [account for account in self._accounts if account.kind == AccountKind.QQ]
        if not qq_accounts:
            account = self.add_qq_account("我的 QQ", [])
        else:
            account = qq_accounts[0]
        conversations = [
            conversation
            for conversation in self._conversations
            if conversation.account_id == account.id
        ]
        selected = set(cleaned)
        enabled_count = 0
        updated: list[Conversation] = []
        for conversation in self._conversations:
            if conversation.account_id == account.id:
                enable = conversation.title in selected
                enabled_count += 1 if enable else 0
                updated.append(replace(conversation, enabled=enable))
            else:
                updated.append(conversation)
        self._conversations = updated
        self.settings = replace(
            self.settings,
            qq_read_experimental=True,
            qq_read_consent=True,
        )
        return {
            "ok": True,
            "account_id": account.id,
            "selected_count": len(cleaned),
            "discovered_count": len(conversations),
            "enabled_count": enabled_count,
            "error": None,
        }

    # ------------------------------------------------------------------
    # 本地数据
    # ------------------------------------------------------------------
    def get_storage_stats(self) -> dict[str, Any]:
        self._record("get_storage_stats")
        self._maybe_fail("get_storage_stats")
        draft_count = sum(len(drafts) for drafts in self._drafts.values())
        return {
            "message_count": len(self._messages),
            "conversation_count": len(self._conversations),
            "todo_count": len(self._todos),
            "draft_count": draft_count,
            "db_bytes": 24_576,
            "data_dir": "（演示服务：数据仅在内存中）",
        }

    def delete_all_local_data(self) -> None:
        self._record("delete_all_local_data")
        self._maybe_fail("delete_all_local_data")
        self._deleted_all = True
        self._messages = []
        self._todos = []
        self._drafts = {}
        self._conversations = []
        self._accounts = []
        self._summary = None
        self._demo_active = False
        self._paused = True
        self.settings = replace(self.settings, collection_paused=True)

    def export_diagnostics(self) -> str:
        self._record("export_diagnostics")
        self._maybe_fail("export_diagnostics")
        return f"（演示环境）诊断信息未写文件：只包含计数与版本信息。{DIAGNOSTICS_HINT}"

    def shutdown(self) -> None:
        self._record("shutdown")


__all__ = ["DIAGNOSTICS_HINT", "FakeService"]
