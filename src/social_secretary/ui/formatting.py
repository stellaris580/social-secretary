"""用户可见文案格式化：时间、分类、状态、来源说明等。

所有函数只依赖标准库 + ``contracts`` 数据对象，不依赖 Qt，方便单元测试。
面向无编程基础用户：时间尽量用「今天 / 昨天 / 10月28日」而不是 ISO 字符串。
"""

from __future__ import annotations

from datetime import date, datetime

from ..contracts import (
    AccountKind,
    Category,
    ConversationKind,
    DraftKind,
    Priority,
    ScanStatus,
    TimestampSource,
    TodoStatus,
)

# 分类 → 中文名 / 说明色
CATEGORY_LABELS: dict[Category, str] = {
    Category.IMPORTANT: "重要",
    Category.NORMAL: "普通",
    Category.FYI: "仅供了解",
}

CATEGORY_HINTS: dict[Category, str] = {
    Category.IMPORTANT: "需要你尽快处理或回复的消息",
    Category.NORMAL: "普通交流，有空再看即可",
    Category.FYI: "群公告、通知、转发等，知道即可",
}

SCAN_STATUS_LABELS: dict[ScanStatus, str] = {
    ScanStatus.OK: "正常",
    ScanStatus.PARTIAL: "部分完成",
    ScanStatus.FAILED: "失败",
    ScanStatus.PAUSED: "已暂停",
    ScanStatus.BLOCKED: "环境不满足（可能未登录或窗口不可用）",
}

TIMESTAMP_SOURCE_LABELS: dict[TimestampSource, str] = {
    TimestampSource.UI: "界面 / 邮件头显示的时间",
    TimestampSource.RELATIVE: "界面只显示相对时间（已尽量换算）",
    TimestampSource.COLLECTED: "消息本身没有时间，使用读取时刻",
}

TIMESTAMP_SOURCE_NOTES: dict[TimestampSource, str] = {
    TimestampSource.UI: "时间来自 QQ 聊天界面或邮件头，可信度较高。",
    TimestampSource.RELATIVE: "QQ 界面只显示了「刚刚 / 昨天 09:12」这类相对时间，程序按读取时刻换算，可能有几分钟误差。",
    TimestampSource.COLLECTED: "这条消息在界面上没有可读时间，程序只能用读取它的时刻代替，不代表真实发送时间。",
}

DRAFT_KIND_LABELS: dict[DraftKind, str] = {
    DraftKind.LOCAL_RULES: "本机规则草稿",
    DraftKind.EXTERNAL_LLM: "外部 AI 草稿",
}

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.HIGH: "高",
    Priority.NORMAL: "普通",
    Priority.LOW: "低",
}

_ACCOUNT_KIND_LABELS: dict[AccountKind, str] = {
    AccountKind.QQ: "QQ",
    AccountKind.EMAIL: "QQ 邮箱",
}

_CONVERSATION_KIND_LABELS: dict[ConversationKind, str] = {
    ConversationKind.PRIVATE: "私聊",
    ConversationKind.GROUP: "群聊",
    ConversationKind.MAIL: "邮箱",
}

_TODO_STATUS_LABELS: dict[TodoStatus, str] = {
    TodoStatus.OPEN: "待处理",
    TodoStatus.DONE: "已完成",
    TodoStatus.DISMISSED: "已忽略",
}


def category_label(category: Category) -> str:
    return CATEGORY_LABELS.get(category, "普通")


def category_hint(category: Category) -> str:
    return CATEGORY_HINTS.get(category, "")


def account_kind_label(kind: AccountKind) -> str:
    return _ACCOUNT_KIND_LABELS.get(kind, "账号")


def conversation_kind_label(kind: ConversationKind) -> str:
    return _CONVERSATION_KIND_LABELS.get(kind, "")


def scan_status_label(status: ScanStatus) -> str:
    return SCAN_STATUS_LABELS.get(status, "未知状态")


def timestamp_source_label(source: TimestampSource) -> str:
    return TIMESTAMP_SOURCE_LABELS.get(source, "未知")


def timestamp_source_note(source: TimestampSource) -> str:
    return TIMESTAMP_SOURCE_NOTES.get(source, "")


def draft_kind_label(kind: DraftKind) -> str:
    return DRAFT_KIND_LABELS.get(kind, "草稿")


def priority_label(priority: Priority) -> str:
    return PRIORITY_LABELS.get(priority, "普通")


def todo_status_label(status: TodoStatus) -> str:
    return _TODO_STATUS_LABELS.get(status, "待处理")


def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone()


def format_datetime(dt: datetime | None, *, now: datetime | None = None, empty: str = "时间未知") -> str:
    """人性化时间：今天 14:05 / 昨天 09:12 / 10月28日 18:30 / 2025年12月1日 09:00。"""
    if dt is None:
        return empty
    local = _local(dt)
    current = _local(now) if now is not None else datetime.now().astimezone()
    if local.date() == current.date():
        return f"今天 {local:%H:%M}"
    delta_days = (current.date() - local.date()).days
    if delta_days == 1:
        return f"昨天 {local:%H:%M}"
    if delta_days == 2:
        return f"前天 {local:%H:%M}"
    if local.year == current.year:
        return f"{local.month}月{local.day}日 {local:%H:%M}"
    return f"{local.year}年{local.month}月{local.day}日 {local:%H:%M}"


def format_date(day: date, *, now: date | None = None) -> str:
    current = now if now is not None else date.today()
    if day == current:
        return f"今天（{day.year}年{day.month}月{day.day}日）"
    if day.year == current.year:
        return f"{day.month}月{day.day}日"
    return f"{day.year}年{day.month}月{day.day}日"


def format_due(dt: datetime | None, due_text: str | None = None, *, now: datetime | None = None) -> str:
    """待办截止时间文案，例如「今天 18:00（原话：明天下午六点前）」。"""
    if dt is None and not due_text:
        return "未识别到截止时间"
    if dt is None:
        return f"截止时间待确认（原话：{due_text}）"
    label = format_datetime(dt, now=now)
    if due_text and due_text.strip() and due_text.strip() not in label:
        return f"{label}（原话：{due_text.strip()}）"
    return label


def format_bytes(num: int | float | None) -> str:
    """字节数 → 人类可读（B / KB / MB / GB）。"""
    try:
        value = float(num or 0)
    except (TypeError, ValueError):
        return "0 B"
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def shorten(text: str | None, limit: int = 60) -> str:
    """截断长文本用于列表展示，保留可读性。"""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)] + "…"


def sender_label(sender: str, is_self: bool) -> str:
    """发送者展示名；自己发的消息统一显示「我」。"""
    if is_self:
        return "我"
    return sender or "对方"


__all__ = [
    "CATEGORY_LABELS",
    "SCAN_STATUS_LABELS",
    "TIMESTAMP_SOURCE_LABELS",
    "TIMESTAMP_SOURCE_NOTES",
    "account_kind_label",
    "category_hint",
    "category_label",
    "conversation_kind_label",
    "draft_kind_label",
    "format_bytes",
    "format_date",
    "format_datetime",
    "format_due",
    "priority_label",
    "scan_status_label",
    "sender_label",
    "shorten",
    "timestamp_source_label",
    "timestamp_source_note",
    "todo_status_label",
]
