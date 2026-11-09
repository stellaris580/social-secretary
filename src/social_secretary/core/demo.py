"""演示数据：**全部为合成内容**，不读取任何真实消息、不联网。

用途：让用户/验收人员在未登录 QQ、未配置邮箱时也能完整体验界面流程
（分类 → 待办 → 草稿 → 每日摘要）。演示数据通过 ``demo_active`` 标记 + 演示账号
（id 以 ``demo-`` 开头）识别，``is_demo_active`` 返回真时 UI 需要显著标注「演示」。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from ..contracts import (
    Account,
    AccountKind,
    Category,
    CollectionResult,
    Conversation,
    ConversationKind,
    DraftKind,
    Message,
    Priority,
    ReplyDraft,
    ScanStatus,
    TimestampSource,
    TodoItem,
    TodoStatus,
)
from .dedupe import compute_content_hash
from .storage import Storage, now_local, to_iso

DEMO_FLAG_KEY = "demo_active"
DEMO_LOADED_AT_KEY = "demo_loaded_at"
DEMO_ACCOUNT_IDS = ("demo-qq-account", "demo-mail-account")
DEMO_QQ_ACCOUNT_ID = "demo-qq-account"
DEMO_MAIL_ACCOUNT_ID = "demo-mail-account"


def is_demo_active(storage: Storage) -> bool:
    """演示模式是否处于激活状态（标记存在且演示账号还在）。"""
    if not storage.get_setting(DEMO_FLAG_KEY, False):
        return False
    return any(storage.get_account(account_id) is not None for account_id in DEMO_ACCOUNT_IDS)


def load_demo_data(
    storage: Storage, *, day: date | None = None, clock: Any | None = None
) -> None:
    """载入（或重新载入）演示数据。重复调用是幂等的。"""
    clear_demo_data(storage, audit=False)
    current = (clock or now_local)()
    base_day = day or current.date()
    tz = current.tzinfo or now_local().tzinfo

    qq_account = Account(
        id=DEMO_QQ_ACCOUNT_ID,
        kind=AccountKind.QQ,
        display_name="演示账号 · QQ（合成数据）",
        enabled=True,
        created_at=current,
        identifier="demo-qq",
    )
    mail_account = Account(
        id=DEMO_MAIL_ACCOUNT_ID,
        kind=AccountKind.EMAIL,
        display_name="演示账号 · 邮箱（合成数据）",
        enabled=True,
        created_at=current,
        identifier="demo@example.com",
    )
    storage.upsert_account(qq_account)
    storage.upsert_account(mail_account)

    conversations = [
        Conversation(
            id="demo-conv-private",
            account_id=DEMO_QQ_ACCOUNT_ID,
            kind=ConversationKind.PRIVATE,
            title="演示 · 同事小王",
            external_key="demo-qq-private",
            enabled=True,
            priority=Priority.NORMAL,
            note="演示数据：合成私聊",
        ),
        Conversation(
            id="demo-conv-group",
            account_id=DEMO_QQ_ACCOUNT_ID,
            kind=ConversationKind.GROUP,
            title="演示 · 项目协作群",
            external_key="demo-qq-group",
            enabled=True,
            priority=Priority.HIGH,
            note="演示数据：合成群聊",
        ),
        Conversation(
            id="demo-conv-mail",
            account_id=DEMO_MAIL_ACCOUNT_ID,
            kind=ConversationKind.MAIL,
            title="INBOX",
            external_key="demo-mail-inbox",
            enabled=True,
            priority=Priority.NORMAL,
            note="演示数据：合成邮件",
        ),
    ]
    for conversation in conversations:
        storage.upsert_conversation(conversation)
    by_id = {conversation.id: conversation for conversation in conversations}

    def at(hour: int, minute: int) -> datetime:
        return datetime.combine(base_day, time(hour, minute), tzinfo=tz)

    specs: list[tuple[str, str, str, Category, datetime, bool, tuple[str, ...]]] = [
        (
            "demo-msg-1",
            "demo-conv-private",
            "小王",
            "明天上午 10 点项目评审会，请带好材料，评审会大概一小时。",
            Category.IMPORTANT,
            at(9, 5),
            False,
            ("包含时间与会议安排", "来自常用联系人"),
        ),
        (
            "demo-msg-2",
            "demo-conv-group",
            "张三",
            "请今天下班前确认预算表并回复我，谢谢。",
            Category.IMPORTANT,
            at(9, 40),
            False,
            ("包含截止时间与明确请求", "群聊中提到了你的名字"),
        ),
        (
            "demo-msg-3",
            "demo-conv-private",
            "小王",
            "周末团建报名接龙，感兴趣的同事在群里接龙就行。",
            Category.NORMAL,
            at(10, 15),
            False,
            ("日常通知",),
        ),
        (
            "demo-msg-4",
            "demo-conv-group",
            "李四",
            "本周技术分享会的资料已经上传到共享盘，感兴趣的同学可以看看。",
            Category.FYI,
            at(10, 45),
            False,
            ("仅供参考的信息",),
        ),
        (
            "demo-msg-5",
            "demo-conv-mail",
            "系统通知",
            "【演示】你订阅的行业周报已更新，内容仅作了解，无需回复。",
            Category.FYI,
            at(11, 10),
            False,
            ("邮件订阅通知，无需处理",),
        ),
        (
            "demo-msg-6",
            "demo-conv-private",
            "我",
            "好的，我马上处理。",
            Category.NORMAL,
            at(11, 20),
            True,
            ("己方消息",),
        ),
    ]

    messages: list[Message] = []
    for index, (message_id, conv_id, sender, text, category, timestamp, is_self, reasons) in enumerate(
        specs, start=1
    ):
        conversation = by_id[conv_id]
        content_hash = compute_content_hash(
            conversation.id,
            sender,
            is_self,
            text,
            timestamp,
            TimestampSource.UI,
        )
        # 演示邮箱消息也给一个合成主题，方便收件箱页演示（is_demo 由账号前缀在读取时回填）
        subject = ""
        if conversation.kind is ConversationKind.MAIL:
            subject = text.split("\n", 1)[0][:80]
        messages.append(
            Message(
                id=message_id,
                account_id=conversation.account_id,
                conversation_id=conversation.id,
                conversation_title=conversation.title,
                conversation_kind=conversation.kind,
                sender=sender,
                is_self=is_self,
                text=text,
                timestamp=timestamp,
                timestamp_source=TimestampSource.UI,
                collected_at=timestamp,
                category=category,
                category_reasons=reasons,
                content_hash=content_hash,
                source_locator=f"demo://{conversation.id}/{index}",
                external_id=f"demo-ext-{index}",
                subject=subject,
                is_demo=True,
            )
        )
    storage.insert_messages(messages)

    todos = [
        TodoItem(
            id="demo-todo-1",
            message_id="demo-msg-1",
            conversation_id="demo-conv-private",
            conversation_title=by_id["demo-conv-private"].title,
            text="参加项目评审会（带好材料）",
            due_at=at(10, 0),
            due_text="明天上午 10:00",
            confidence=0.86,
            status=TodoStatus.OPEN,
            created_at=current,
        ),
        TodoItem(
            id="demo-todo-2",
            message_id="demo-msg-2",
            conversation_id="demo-conv-group",
            conversation_title=by_id["demo-conv-group"].title,
            text="确认预算表并回复张三",
            due_at=at(18, 0),
            due_text="今天下班前",
            confidence=0.8,
            status=TodoStatus.OPEN,
            created_at=current,
        ),
    ]
    storage.insert_todos(todos)

    drafts = [
        ReplyDraft(
            id="demo-draft-1",
            message_id="demo-msg-1",
            kind=DraftKind.LOCAL_RULES,
            style="简洁",
            text="收到，明早 10 点我会准时参加并带好材料。",
            created_at=current,
            model_label="本地模板（演示）",
            requires_external_consent=False,
        ),
        ReplyDraft(
            id="demo-draft-2",
            message_id="demo-msg-2",
            kind=DraftKind.LOCAL_RULES,
            style="礼貌",
            text="收到，我核对预算表后会在下班前回复您，谢谢提醒。",
            created_at=current,
            model_label="本地模板（演示）",
            requires_external_consent=False,
        ),
    ]
    storage.insert_drafts(drafts)

    for account in (qq_account, mail_account):
        storage.insert_scan_run(
            CollectionResult(
                scan_id=f"demo-scan-{account.id}",
                account_id=account.id,
                status=ScanStatus.OK,
                messages_seen=len(messages),
                messages_new=len(messages),
                conversations_scanned=len(conversations),
                warnings=("演示数据：未连接真实账号，全部内容为合成数据",),
                started_at=at(12, 30),
                finished_at=at(12, 30),
                foreground_used=False,
                duration_ms=8,
            ),
            trigger="demo",
        )

    storage.set_setting(DEMO_FLAG_KEY, True)
    storage.set_setting(DEMO_LOADED_AT_KEY, to_iso(current))
    storage.clear_summaries()
    storage.append_audit(
        "demo_loaded",
        detail={
            "messages": len(messages),
            "todos": len(todos),
            "drafts": len(drafts),
            "is_demo": True,
        },
    )


def clear_demo_data(storage: Storage, *, audit: bool = True) -> None:
    """删除全部演示数据（不影响真实账号/消息）。"""
    for account_id in DEMO_ACCOUNT_IDS:
        try:
            storage.delete_account(account_id, delete_messages=True)
        except Exception:  # noqa: BLE001 - 清理要尽量成功，不因单步失败中断
            pass
    storage.delete_setting(DEMO_FLAG_KEY)
    storage.delete_setting(DEMO_LOADED_AT_KEY)
    storage.clear_summaries()
    if audit:
        storage.append_audit("demo_cleared", detail={"is_demo": True})


__all__ = [
    "DEMO_ACCOUNT_IDS",
    "DEMO_FLAG_KEY",
    "DEMO_LOADED_AT_KEY",
    "DEMO_MAIL_ACCOUNT_ID",
    "DEMO_QQ_ACCOUNT_ID",
    "clear_demo_data",
    "is_demo_active",
    "load_demo_data",
]
