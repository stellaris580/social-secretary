"""核心层测试用的 fake 实现（不依赖 intel / collectors 的真实实现）。

所有 fake 都只实现 contracts 中的协议，保证核心层可以在队友模块未就绪时独立测试。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from social_secretary.contracts import (
    Account,
    AccountKind,
    Category,
    CategoryDecision,
    Classifier,
    ConnectionCheck,
    Conversation,
    ConversationKind,
    DraftContext,
    DraftKind,
    ExtractedTodo,
    Message,
    Priority,
    RawMessage,
    ReplyDraft,
    TimestampSource,
    TodoExtractor,
)
from social_secretary.core.dedupe import compute_content_hash
from social_secretary.core.storage import now_local


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------

def make_account(
    account_id: str = "acct-1",
    *,
    kind: AccountKind = AccountKind.QQ,
    display_name: str = "测试账号",
    enabled: bool = True,
    identifier: str = "",
) -> Account:
    return Account(
        id=account_id,
        kind=kind,
        display_name=display_name,
        enabled=enabled,
        created_at=now_local(),
        identifier=identifier,
    )


def make_conversation(
    conversation_id: str = "conv-1",
    *,
    account_id: str = "acct-1",
    kind: ConversationKind = ConversationKind.PRIVATE,
    title: str = "测试会话",
    external_key: str | None = None,
    enabled: bool = True,
    priority: Priority = Priority.NORMAL,
) -> Conversation:
    return Conversation(
        id=conversation_id,
        account_id=account_id,
        kind=kind,
        title=title,
        external_key=external_key or f"ext-{conversation_id}",
        enabled=enabled,
        priority=priority,
    )


def make_message(
    conversation: Conversation,
    text: str = "你好",
    *,
    message_id: str | None = None,
    account_id: str | None = None,
    category: Category = Category.NORMAL,
    sender: str = "张三",
    is_self: bool = False,
    timestamp: datetime | None = None,
    timestamp_source: TimestampSource | None = None,
    collected_at: datetime | None = None,
    reasons: tuple[str, ...] = (),
) -> Message:
    collected = collected_at or timestamp or now_local()
    source = timestamp_source or (
        TimestampSource.UI if timestamp is not None else TimestampSource.COLLECTED
    )
    content_hash = compute_content_hash(
        conversation.id, sender, is_self, text, timestamp, source
    )
    return Message(
        id=message_id or uuid.uuid4().hex,
        account_id=account_id or conversation.account_id,
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        conversation_kind=conversation.kind,
        sender=sender,
        is_self=is_self,
        text=text,
        timestamp=timestamp,
        timestamp_source=source,
        collected_at=collected,
        category=category,
        category_reasons=reasons,
        content_hash=content_hash,
    )


def make_raw(
    conversation_key: str,
    text: str,
    *,
    sender: str = "张三",
    is_self: bool = False,
    timestamp: datetime | None = None,
    timestamp_source: TimestampSource | None = None,
    external_id: str | None = None,
    source_locator: str | None = None,
    raw_time_text: str | None = None,
    extras: dict[str, Any] | None = None,
) -> RawMessage:
    source = timestamp_source or (
        TimestampSource.UI if timestamp is not None else TimestampSource.COLLECTED
    )
    return RawMessage(
        conversation_key=conversation_key,
        sender=sender,
        is_self=is_self,
        text=text,
        timestamp=timestamp,
        timestamp_source=source,
        raw_time_text=raw_time_text,
        external_id=external_id,
        source_locator=source_locator,
        extras=dict(extras or {}),
    )


# ---------------------------------------------------------------------------
# intel / 采集器 fake
# ---------------------------------------------------------------------------

class FakeClassifier:
    """按消息文本返回预设分类。"""

    def __init__(self, rules: dict[str, Category] | None = None) -> None:
        self._rules = rules or {}
        self.calls = 0

    def classify(
        self,
        raw: RawMessage,
        *,
        conversation_title: str,
        conversation_kind: ConversationKind,
        conversation_priority: Priority = Priority.NORMAL,
    ) -> CategoryDecision:
        self.calls += 1
        category = self._rules.get(raw.text, Category.NORMAL)
        return CategoryDecision(
            category=category, reasons=(f"fake-rule:{category.value}",), score=0.5
        )


class FakeTodoExtractor:
    """按消息文本返回预设待办。"""

    def __init__(self, rules: dict[str, list[ExtractedTodo]] | None = None) -> None:
        self._rules = rules or {}
        self.calls = 0

    def extract(self, raw: RawMessage) -> list[ExtractedTodo]:
        self.calls += 1
        return list(self._rules.get(raw.text, []))


class FakeDraftProvider:
    """本地草稿 fake：每个 style 生成一条草稿。"""

    kind = DraftKind.LOCAL_RULES

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def propose(self, message: Message, context: DraftContext) -> list[ReplyDraft]:
        self.calls.append((message.id, context.style))
        if self.fail:
            raise RuntimeError("fake local provider failed")
        return [
            ReplyDraft(
                id="",
                message_id=message.id,
                kind=DraftKind.LOCAL_RULES,
                style=context.style,
                text=f"fake-local[{context.style}] 收到，稍后回复。",
            )
        ]


class FakeExternalProvider:
    """外部 AI fake：只有被真正调用时才计数（用于断言门控生效）。"""

    kind = DraftKind.EXTERNAL_LLM

    def __init__(self, *, fail: bool = False, connection_ok: bool = True) -> None:
        self.fail = fail
        self.connection_ok = connection_ok
        self.calls = 0
        self.test_calls = 0

    def propose(self, message: Message, context: DraftContext) -> list[ReplyDraft]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fake external provider failed")
        return [
            ReplyDraft(
                id="",
                message_id=message.id,
                kind=DraftKind.EXTERNAL_LLM,
                style=context.style,
                text="fake-external 草稿内容",
                model_label="fake-model",
            )
        ]

    def test_connection(self) -> ConnectionCheck:
        self.test_calls += 1
        return ConnectionCheck(
            ok=self.connection_ok,
            detail="fake external ok" if self.connection_ok else "fake external down",
        )


class FakeMessageSource:
    """只读消息源 fake。fetch_messages 记录调用，便于断言白名单强制。"""

    def __init__(
        self,
        kind: AccountKind = AccountKind.QQ,
        *,
        conversations: list[Conversation] | None = None,
        messages: dict[str, list[RawMessage]] | None = None,
        connection_ok: bool = True,
        fail_conversations: set[str] | None = None,
        fail_list: bool = False,
        fail_fetch: bool = False,
    ) -> None:
        self.kind = kind
        self.conversations = list(conversations or [])
        self.messages = dict(messages or {})
        self.connection_ok = connection_ok
        self.fail_conversations = set(fail_conversations or set())
        self.fail_list = fail_list
        self.fail_fetch = fail_fetch
        self.fetch_calls: list[str] = []
        self.closed = False

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(
            ok=self.connection_ok,
            detail="fake connected" if self.connection_ok else "fake connection failed",
        )

    def list_conversations(self) -> list[Conversation]:
        if self.fail_list:
            raise RuntimeError("fake list_conversations failed")
        return list(self.conversations)

    def fetch_messages(
        self,
        conversation: Conversation,
        *,
        since: datetime | None = None,
        limit: int = 100,
        foreground_ack: bool = False,
    ) -> list[RawMessage]:
        self.fetch_calls.append(conversation.id)
        if self.fail_fetch:
            raise RuntimeError("fake fetch_messages failed")
        if conversation.id in self.fail_conversations:
            raise RuntimeError(f"fake fetch failed for {conversation.id}")
        if conversation.external_key in self.messages:
            return list(self.messages[conversation.external_key])
        return list(self.messages.get(conversation.id, []))

    def close(self) -> None:
        self.closed = True


def make_service(**kwargs: Any):
    """构造使用隔离数据目录与内存凭据库的服务（数据目录由 tests/conftest 隔离）。"""
    from social_secretary.core.service import SocialSecretaryService
    from social_secretary.security.credential_store import InMemoryCredentialStore

    kwargs.setdefault("credential_store", InMemoryCredentialStore())
    return SocialSecretaryService(**kwargs)


def assert_service_api_surface(service: Any) -> None:
    """结构检查 ServiceAPI 契约。

    ``contracts.ServiceAPI`` 没有 ``@runtime_checkable``（契约文件由 Lead 维护，app-core
    不修改），因此不能用 ``isinstance``；这里逐个检查协议里声明的方法都真实可调用。
    """
    from social_secretary.contracts import ServiceAPI

    expected = [
        name
        for name, value in vars(ServiceAPI).items()
        if not name.startswith("_") and callable(value)
    ]
    assert len(expected) >= 40, f"ServiceAPI 方法数异常：{len(expected)}"
    missing = [name for name in expected if not callable(getattr(service, name, None))]
    assert not missing, f"服务缺少 ServiceAPI 方法：{missing}"
