"""每日摘要生成。

- 只聚合**本地数据库**中的消息（不联网、不调用任何模型）。
- 按「用户本地日期」聚合：消息入库时已计算 ``local_day``（时间为空时用采集时间）。
- ``narrative`` 使用本地中文模板拼装，不包含外部 AI 调用。
- 摘要写 ``summaries`` 表缓存；``get_cached_summary`` 读取缓存。
- ``caveats`` 明确标注采集局限：时间来源、QQ 可见范围、采集失败、演示数据等。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..contracts import (
    AccountKind,
    Category,
    ConversationKind,
    DailySummary,
    Message,
    MessageWorkStatus,
    ScanStatus,
    TimestampSource,
    TodoItem,
    TodoStatus,
)
from .storage import Storage, from_iso, local_day_of, now_local, to_iso

_WEEKDAYS = "一二三四五六日"

# 摘要中「需要优先关注」最多列出的会话/消息数
_MAX_IMPORTANT_HIGHLIGHTS = 3


def today_local() -> date:
    return now_local().date()


def format_day(day: date) -> str:
    return f"{day.year}年{day.month:02d}月{day.day:02d}日（周{_WEEKDAYS[day.weekday()]}）"


# ---------------------------------------------------------------------------
# 序列化（缓存到 summaries 表）
# ---------------------------------------------------------------------------

def message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "account_id": message.account_id,
        "conversation_id": message.conversation_id,
        "conversation_title": message.conversation_title,
        "conversation_kind": message.conversation_kind.value,
        "sender": message.sender,
        "is_self": message.is_self,
        "text": message.text,
        "timestamp": to_iso(message.timestamp),
        "timestamp_source": message.timestamp_source.value,
        "collected_at": to_iso(message.collected_at),
        "category": message.category.value,
        "category_reasons": list(message.category_reasons),
        "work_status": message.work_status.value,
        "content_hash": message.content_hash,
        "source_locator": message.source_locator,
        "external_id": message.external_id,
        "subject": message.subject,
        "is_demo": message.is_demo,
        "attachment_names": list(message.attachment_names),
    }


def _parse_work_status(value: Any) -> MessageWorkStatus:
    """缓存 payload 兼容：缺失/未知状态回退 unhandled。"""
    try:
        return MessageWorkStatus(str(value or MessageWorkStatus.UNHANDLED.value))
    except ValueError:
        return MessageWorkStatus.UNHANDLED


def message_from_dict(data: dict[str, Any]) -> Message:
    return Message(        id=str(data.get("id", "")),
        account_id=str(data.get("account_id", "")),
        conversation_id=str(data.get("conversation_id", "")),
        conversation_title=str(data.get("conversation_title", "")),
        conversation_kind=ConversationKind(data.get("conversation_kind", "private")),
        sender=str(data.get("sender", "")),
        is_self=bool(data.get("is_self", False)),
        text=str(data.get("text", "")),
        timestamp=from_iso(data.get("timestamp")),
        timestamp_source=TimestampSource(data.get("timestamp_source", "collected")),
        collected_at=from_iso(data.get("collected_at")) or now_local(),
        category=Category(data.get("category", "normal")),
        category_reasons=tuple(str(item) for item in data.get("category_reasons") or ()),
        work_status=_parse_work_status(data.get("work_status")),
        content_hash=str(data.get("content_hash", "")),
        source_locator=data.get("source_locator"),
        external_id=data.get("external_id"),
        subject=str(data.get("subject") or ""),
        is_demo=bool(data.get("is_demo", False)),
        attachment_names=tuple(
            str(item) for item in (data.get("attachment_names") or ()) if isinstance(item, str)
        ),
    )


def todo_to_dict(todo: TodoItem) -> dict[str, Any]:
    return {
        "id": todo.id,
        "message_id": todo.message_id,
        "conversation_id": todo.conversation_id,
        "conversation_title": todo.conversation_title,
        "text": todo.text,
        "due_at": to_iso(todo.due_at),
        "due_text": todo.due_text,
        "confidence": todo.confidence,
        "status": todo.status.value,
        "created_at": to_iso(todo.created_at),
    }


def todo_from_dict(data: dict[str, Any]) -> TodoItem:
    return TodoItem(
        id=str(data.get("id", "")),
        message_id=str(data.get("message_id", "")),
        conversation_id=str(data.get("conversation_id", "")),
        conversation_title=str(data.get("conversation_title", "")),
        text=str(data.get("text", "")),
        due_at=from_iso(data.get("due_at")),
        due_text=data.get("due_text"),
        confidence=float(data.get("confidence", 0.5)),
        status=TodoStatus(data.get("status", "open")),
        created_at=from_iso(data.get("created_at")),
    )


def summary_to_dict(summary: DailySummary) -> dict[str, Any]:
    return {
        "day": summary.day.isoformat(),
        "generated_at": to_iso(summary.generated_at),
        "counts": dict(summary.counts),
        "important": [message_to_dict(item) for item in summary.important],
        "normal": [message_to_dict(item) for item in summary.normal],
        "fyi": [message_to_dict(item) for item in summary.fyi],
        "todos": [todo_to_dict(item) for item in summary.todos],
        "conversations_touched": summary.conversations_touched,
        "narrative": summary.narrative,
        "caveats": list(summary.caveats),
        "scan_status": summary.scan_status.value,
        "is_demo": summary.is_demo,
    }


def summary_from_dict(data: dict[str, Any]) -> DailySummary:
    return DailySummary(
        day=date.fromisoformat(str(data.get("day", today_local().isoformat()))),
        generated_at=from_iso(data.get("generated_at")) or now_local(),
        counts={str(key): int(value) for key, value in (data.get("counts") or {}).items()},
        important=tuple(message_from_dict(item) for item in data.get("important") or ()),
        normal=tuple(message_from_dict(item) for item in data.get("normal") or ()),
        fyi=tuple(message_from_dict(item) for item in data.get("fyi") or ()),
        todos=tuple(todo_from_dict(item) for item in data.get("todos") or ()),
        conversations_touched=int(data.get("conversations_touched", 0)),
        narrative=str(data.get("narrative", "")),
        caveats=tuple(str(item) for item in data.get("caveats") or ()),
        scan_status=ScanStatus(data.get("scan_status", "ok")),
        is_demo=bool(data.get("is_demo", False)),
    )


# ---------------------------------------------------------------------------
# 生成 / 缓存
# ---------------------------------------------------------------------------

def generate_daily_summary(
    storage: Storage,
    *,
    day: date | None = None,
    is_demo: bool = False,
    clock: Any | None = None,
) -> DailySummary:
    """生成（并缓存）指定本地日期的摘要。"""
    target_day = day or (clock or now_local)().date()
    generated_at = (clock or now_local)()

    messages = storage.list_messages(day=target_day, limit=1_000_000)
    received = [message for message in messages if not message.is_self]
    important = tuple(item for item in received if item.category == Category.IMPORTANT)
    normal = tuple(item for item in received if item.category == Category.NORMAL)
    fyi = tuple(item for item in received if item.category == Category.FYI)
    todos = tuple(storage.list_todos(day=target_day, include_done=True, limit=10_000))
    open_todos = sum(1 for todo in todos if todo.status == TodoStatus.OPEN)
    conversations_touched = storage.distinct_conversations_touched(target_day)
    scan_runs = storage.list_scan_runs(day=target_day, limit=1_000)
    scan_status = storage.scan_status_for_day(target_day)
    latest_runs = storage.latest_scan_runs_for_day(target_day)
    latest_scan_ids = {run.scan_id for run in latest_runs.values()}
    enabled_account_ids = {
        account.id for account in storage.enabled_scan_accounts()
    }
    account_kinds = {
        account.id: account.kind for account in storage.list_accounts()
    }
    # 同一天较早的失败/部分失败（已被该账号更新的 run 覆盖）：只作为历史说明
    historical_failures = [
        run
        for run in scan_runs
        if run.scan_id not in latest_scan_ids
        and run.status in (ScanStatus.PARTIAL, ScanStatus.FAILED)
        and run.account_id in enabled_account_ids
    ]

    counts = {
        "total": len(received),
        "important": len(important),
        "normal": len(normal),
        "fyi": len(fyi),
        "todos": open_todos,
        "conversations": conversations_touched,
        "sent": len(messages) - len(received),
    }
    caveats = build_caveats(
        messages=messages,
        received=received,
        scan_runs=scan_runs,
        scan_status=scan_status,
        is_demo=is_demo,
        historical_failures=historical_failures,
        account_kinds=account_kinds,
    )
    narrative = build_narrative(
        day=target_day,
        counts=counts,
        important=important,
        open_todos=open_todos,
        scan_status=scan_status,
    )
    summary = DailySummary(
        day=target_day,
        generated_at=generated_at,
        counts=counts,
        important=important,
        normal=normal,
        fyi=fyi,
        todos=todos,
        conversations_touched=conversations_touched,
        narrative=narrative,
        caveats=tuple(caveats),
        scan_status=scan_status,
        is_demo=is_demo,
    )
    storage.save_summary(target_day, generated_at, is_demo, summary_to_dict(summary))
    return summary


def get_cached_summary(storage: Storage, day: date | None = None) -> DailySummary | None:
    """读取缓存摘要；没有缓存返回 None。

    若缓存的 scan_status 与“当天每个已启用账号最新 run”的聚合状态不一致
    （例如旧缓存是 partial，但后续成功整理已把所有账号刷新为 OK），
    重新生成并刷新缓存，让页面上的黄色告警及时消失；历史 scan_runs 不删除。
    """
    target_day = day or today_local()
    cached = storage.load_summary(target_day)
    if cached is None:
        return None
    try:
        summary = summary_from_dict(cached["payload"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        current_status = storage.scan_status_for_day(target_day)
    except Exception:  # noqa: BLE001 - 缓存读取不应因聚合失败而崩
        return summary
    if summary.scan_status is not current_status:
        try:
            return generate_daily_summary(
                storage, day=target_day, is_demo=summary.is_demo
            )
        except Exception:  # noqa: BLE001 - 刷新失败仍返回旧缓存
            return summary
    return summary


# ---------------------------------------------------------------------------
# 文案模板
# ---------------------------------------------------------------------------

def build_narrative(
    *,
    day: date,
    counts: dict[str, int],
    important: tuple[Message, ...],
    open_todos: int,
    scan_status: ScanStatus = ScanStatus.OK,
) -> str:
    """纯本地中文模板，不调用任何模型。

    负向路径要求：**采集失败/被阻止/暂停导致的「0 消息」不能被说成「今天没有新消息」**，
    否则用户会误以为已经完整检查过。
    """
    total = counts.get("total", 0)
    if total <= 0:
        if scan_status in (ScanStatus.FAILED, ScanStatus.PARTIAL):
            return (
                f"{format_day(day)}：今天暂无可展示的消息——本次采集未能完成（详见下方提示）。"
                "这不代表今天没有新消息；请修复采集问题后重试。"
            )
        if scan_status in (ScanStatus.BLOCKED, ScanStatus.PAUSED):
            return (
                f"{format_day(day)}：今天暂无可展示的消息——采集未获授权或已暂停（详见下方提示）。"
                "这不代表今天没有新消息；请先完成授权或恢复采集后重试。"
            )
        return (
            f"{format_day(day)}：今天没有新的消息需要整理。"
            "建议保持「只读采集」，需要时再手动刷新一次。"
        )

    parts = [
        f"{format_day(day)}：今天共整理 {total} 条收到的消息",
        f"其中重要 {counts.get('important', 0)} 条、普通 {counts.get('normal', 0)} 条、"
        f"仅供了解 {counts.get('fyi', 0)} 条",
        f"来自 {counts.get('conversations', 0)} 个会话。",
    ]
    narrative = "，".join(parts[:2]) + "，" + parts[2]

    if important:
        titles: list[str] = []
        for message in important:
            if message.conversation_title and message.conversation_title not in titles:
                titles.append(message.conversation_title)
            if len(titles) >= _MAX_IMPORTANT_HIGHLIGHTS:
                break
        if titles:
            narrative += "需要优先关注：" + "、".join(titles) + "。"

    if open_todos:
        narrative += f"今天有 {open_todos} 条待办需要处理，可在「待办」页逐条标记完成。"
    else:
        narrative += "今天暂时没有新增待办。"

    narrative += "回复草稿由本地规则生成，仅供参考，本程序不会自动发送任何消息。"
    return narrative


def build_caveats(
    *,
    messages: list[Message],
    received: list[Message],
    scan_runs: list[Any],
    scan_status: ScanStatus,
    is_demo: bool,
    historical_failures: list[Any] | None = None,
    account_kinds: dict[str, AccountKind] | None = None,
) -> list[str]:
    historical_failures = list(historical_failures or [])
    account_kinds = account_kinds or {}
    caveats: list[str] = []
    if is_demo:
        caveats.append("当前为演示数据（全部为合成内容），不代表你的真实消息。")

    if scan_status in (ScanStatus.PARTIAL, ScanStatus.FAILED):
        # 最新一次采集仍失败/部分失败：保持当前失败提示
        caveats.append("今天的采集存在失败/部分失败记录，摘要可能不完整。")
    elif historical_failures:
        # 当天早些时候失败过，但每个已启用账号的最新 run 都已成功：
        # 说明历史记录保留、不代表本次失败，不再显示“当前失败”告警。
        caveats.append(
            "今天早些时候有采集失败/部分失败记录；后续成功整理已覆盖当前授权来源，"
            "历史记录仍保留，不代表本次整理失败。"
        )
    if scan_status == ScanStatus.BLOCKED:
        caveats.append("有账号因未授权或环境不满足而未能采集。")
    if any(bool(getattr(run, "foreground_used", False)) for run in scan_runs):
        caveats.append("采集 QQ 时曾短暂占用前台窗口，可能影响你当时的操作。")

    if any(item.timestamp_source == TimestampSource.COLLECTED for item in received):
        caveats.append(
            "部分消息缺少原始时间，已按采集时间归入今天（时间来源标注为「采集时间」）。"
        )
    if any(item.timestamp_source == TimestampSource.RELATIVE for item in received):
        caveats.append("部分消息的时间由界面相对时间推断，可能与真实发送时间有偏差。")

    qq_messages = [
        item
        for item in messages
        if item.conversation_kind in (ConversationKind.PRIVATE, ConversationKind.GROUP)
    ]
    historical_qq_failure = any(
        account_kinds.get(str(getattr(run, "account_id", ""))) is AccountKind.QQ
        for run in historical_failures
    )
    if (qq_messages or historical_qq_failure) and not is_demo:
        # 即使最新 QQ 采集已成功，也只覆盖当前可见/已加载的消息，风险不能清除
        caveats.append(
            "QQ 只读采集仅覆盖聊天窗口当前可见的消息，更早/未加载的历史消息仍可能缺失。"
        )

    if not messages and not is_demo:
        if scan_status in (ScanStatus.PARTIAL, ScanStatus.FAILED):
            # 采集不完整时不能断言“今天没有新消息”（与失败提示矛盾，会误导用户）
            caveats.append(
                "本次采集不完整，无法确认今天是否有新消息；已保存的历史消息不会丢失。"
            )
        elif scan_status in (ScanStatus.BLOCKED, ScanStatus.PAUSED):
            caveats.append(
                "本次采集未完成（未授权或已暂停），无法确认今天是否有新消息；"
                "已保存的历史消息不会丢失。"
            )
        elif not scan_runs:
            caveats.append("今天还没有采集记录；可在主界面手动开始一次只读采集。")
        else:
            caveats.append("今天没有采集到新消息。")
    return caveats


__all__ = [
    "build_caveats",
    "build_narrative",
    "format_day",
    "generate_daily_summary",
    "get_cached_summary",
    "message_from_dict",
    "message_to_dict",
    "summary_from_dict",
    "summary_to_dict",
    "todo_from_dict",
    "todo_to_dict",
    "today_local",
]
