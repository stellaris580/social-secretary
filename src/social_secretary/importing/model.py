"""导入聊天文本：数据模型（实验性 P1 原型）。

边界（与 ``docs/research/chat-text-import.md`` 一起阅读）
--------------------------------------------------------
1. 本包只处理**用户主动选择/粘贴**的文本文件内容：不读取 QQ 本地消息数据库、
   不调用 QQ 非官方协议、不抓取登录凭据、不联网。
2. 输出的是 :class:`social_secretary.contracts.RawMessage`，与 QQ UIA / IMAP 采集器
   的输出契约一致，未来可由核心层按现有去重/分类流水线入库。当前原型通过调用方
   **注入的** ``MessageSink`` 写入（见 :mod:`social_secretary.importing.importer`），
   未修改 ``core`` / ``contracts``。
3. 时间为**本地时区**的 aware :class:`datetime`。文本里明确给出绝对时间的标
   ``TimestampSource.UI``（契约枚举里最接近“消息自带时间”的取值），相对时间
   （刚刚/昨天/星期X）标 ``RELATIVE``，完全没有时间标 ``COLLECTED`` 并让
   ``timestamp=None``（绝不伪装成真实时间）。
4. 证据/日志只使用**长度 + SHA-256** 指纹，不落原文（见 :func:`text_fingerprint`
   与 :meth:`ImportResult.to_evidence_dict`）。

本模块只依赖标准库与 ``contracts``。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..contracts import ConversationKind, RawMessage, TimestampSource

#: 版本标记：本功能整体标为实验性，UI/文档必须展示。
EXPERIMENTAL = True
EXPERIMENTAL_NOTICE = (
    "实验性功能：聊天文本导入不保证兼容任意导出格式，需用户手动提供文本；"
    "只在本机解析与保存，不会自动同步，也不等于“自动读取 QQ 聊天记录”。"
)

#: 默认认为“己方”的发送者昵称。真实昵称无法可靠推断，见 SELF_SENDER_ASSUMED 警告。
DEFAULT_SELF_NAMES: tuple[str, ...] = ("我", "自己", "本人", "me", "Me", "ME", "myself")

#: 导入消息在 ``RawMessage.extras`` 中的固定标记，便于核心层识别来源。
EXTRA_SOURCE = "imported"
EXTRA_MARKER_KEY = "imported"


class ImportFormat(str, Enum):
    """支持的输入格式（``AUTO`` 表示自动识别）。"""

    AUTO = "auto"
    GENERIC = "generic"      # 通用行格式：时间 + 发送者 + 内容
    QQ_COPY = "qq_copy"      # QQ 复制/导出常见文本样式（启发式，未真机验证）
    JSON = "json"            # [{"sender","text","timestamp"}] 或 {"messages": [...]}
    CSV = "csv"              # sender,text,timestamp 列
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: "ImportFormat | str | None") -> "ImportFormat":
        """把字符串/枚举/None 统一成枚举；无法识别时返回 ``AUTO``。"""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.AUTO
        raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "auto": cls.AUTO,
            "auto_detect": cls.AUTO,
            "": cls.AUTO,
            "generic": cls.GENERIC,
            "generic_line": cls.GENERIC,
            "lines": cls.GENERIC,
            "plain": cls.GENERIC,
            "text": cls.GENERIC,
            "qq": cls.QQ_COPY,
            "qq_copy": cls.QQ_COPY,
            "qq_export": cls.QQ_COPY,
            "qqcopied": cls.QQ_COPY,
            "json": cls.JSON,
            "jsonl": cls.JSON,
            "csv": cls.CSV,
        }
        return aliases.get(raw, cls.AUTO)

    @property
    def label_zh(self) -> str:
        return {
            ImportFormat.AUTO: "自动识别",
            ImportFormat.GENERIC: "通用行格式",
            ImportFormat.QQ_COPY: "QQ 复制/导出文本（实验性启发式）",
            ImportFormat.JSON: "JSON",
            ImportFormat.CSV: "CSV",
            ImportFormat.UNKNOWN: "未知格式",
        }[self]


class WarningCode(str, Enum):
    """警告码（证据文件只记录码、数量与行号，不记录消息正文）。"""

    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LARGE = "input_too_large"
    MOJIBAKE_SUSPECTED = "mojibake_suspected"
    CONTROL_CHARS_STRIPPED = "control_chars_stripped"
    UNSUPPORTED_HTML = "unsupported_html"
    UNRECOGNIZED_LINE = "unrecognized_line"
    NO_MESSAGES = "no_messages"
    FORMAT_UNKNOWN = "format_unknown"
    JSON_PARSE_FAILED = "json_parse_failed"
    JSON_RECORD_INVALID = "json_record_invalid"
    CSV_PARSE_FAILED = "csv_parse_failed"
    CSV_HEADER_ASSUMED = "csv_header_assumed"
    INVALID_TIMESTAMP = "invalid_timestamp"
    MISSING_TIMESTAMP = "missing_timestamp"
    TIMESTAMP_ASSUMED_DATE = "timestamp_assumed_date"
    DUPLICATES_REMOVED = "duplicates_removed"
    MESSAGE_TOO_LONG = "message_too_long"
    MESSAGE_LIMIT_REACHED = "message_limit_reached"
    EMPTY_MESSAGE = "empty_message"
    SELF_SENDER_ASSUMED = "self_sender_assumed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ImportWarning:
    """一条导入警告。

    ``message`` 必须是**不含聊天正文**的简体中文说明；``line_numbers`` 只保留行号。
    """

    code: WarningCode
    message: str
    count: int = 1
    line_numbers: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "count": self.count,
            "line_numbers": list(self.line_numbers[:10]),
        }


@dataclass(frozen=True)
class ImportOptions:
    """解析选项（不可变；解析器不会修改调用方传入的对象）。"""

    format_hint: str = "auto"
    conversation_key: str = ""
    conversation_title: str = ""
    conversation_kind: ConversationKind = ConversationKind.PRIVATE
    account_label: str = "imported"
    source_name: str = ""
    self_sender_names: tuple[str, ...] = DEFAULT_SELF_NAMES
    max_input_chars: int = 2_000_000
    max_messages: int = 20_000
    max_message_chars: int = 8_000
    dedupe: bool = True
    now: datetime | None = None

    def __post_init__(self) -> None:
        if self.max_input_chars < 1:
            raise ValueError("max_input_chars 必须为正整数")
        if self.max_messages < 1:
            raise ValueError("max_messages 必须为正整数")
        if self.max_message_chars < 1:
            raise ValueError("max_message_chars 必须为正整数")
        if not isinstance(self.conversation_kind, ConversationKind):
            try:
                object.__setattr__(self, "conversation_kind", ConversationKind(self.conversation_kind))
            except ValueError as exc:  # pragma: no cover - 防御性
                raise ValueError(f"未知会话类型: {self.conversation_kind!r}") from exc

    def resolved_now(self) -> datetime:
        """返回带本地时区的“当前时间”；可注入固定时间便于测试。"""
        moment = self.now
        if moment is None:
            return datetime.now().astimezone()
        if moment.tzinfo is None:
            return moment.astimezone()
        return moment

    def resolved_format(self) -> ImportFormat:
        return ImportFormat.coerce(self.format_hint)

    def resolved_title(self) -> str:
        title = (self.conversation_title or "").strip()
        return title or "导入的聊天记录"

    def resolved_self_names(self) -> tuple[str, ...]:
        return tuple(
            name.strip() for name in self.self_sender_names if name and name.strip()
        )


@dataclass
class ImportStats:
    """解析统计（成功/失败/截断/去重计数）。"""

    format_detected: ImportFormat = ImportFormat.UNKNOWN
    input_chars: int = 0
    input_lines: int = 0
    parsed_messages: int = 0
    skipped_lines: int = 0
    duplicate_messages: int = 0
    self_messages: int = 0
    other_messages: int = 0
    sender_count: int = 0
    timestamped_messages: int = 0
    untimed_messages: int = 0
    invalid_timestamps: int = 0
    truncated_input: bool = False
    truncated_messages: bool = False
    truncated_texts: int = 0
    replaced_chars: int = 0
    elapsed_ms: int = 0

    @property
    def parse_success_rate(self) -> float:
        """已解析消息数 / (已解析消息数 + 跳过的行数)，0..1。"""
        total = self.parsed_messages + self.skipped_lines
        if total <= 0:
            return 0.0
        return round(self.parsed_messages / total, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_detected": self.format_detected.value,
            "format_label": self.format_detected.label_zh,
            "input_chars": self.input_chars,
            "input_lines": self.input_lines,
            "parsed_messages": self.parsed_messages,
            "skipped_lines": self.skipped_lines,
            "duplicate_messages": self.duplicate_messages,
            "self_messages": self.self_messages,
            "other_messages": self.other_messages,
            "sender_count": self.sender_count,
            "timestamped_messages": self.timestamped_messages,
            "untimed_messages": self.untimed_messages,
            "invalid_timestamps": self.invalid_timestamps,
            "truncated_input": self.truncated_input,
            "truncated_messages": self.truncated_messages,
            "truncated_texts": self.truncated_texts,
            "replaced_chars": self.replaced_chars,
            "elapsed_ms": self.elapsed_ms,
            "parse_success_rate": self.parse_success_rate,
        }


@dataclass(frozen=True)
class ImportedMessage:
    """一条已解析的导入消息（保留原始行号，便于用户定位问题）。"""

    raw: RawMessage
    line_no: int
    raw_time_text: str | None = None


@dataclass(frozen=True)
class ImportedConversation:
    """导入会话的元数据（不含消息正文）。"""

    conversation_key: str
    title: str
    kind: ConversationKind = ConversationKind.PRIVATE
    account_label: str = "imported"
    source_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_key": self.conversation_key,
            "title": self.title,
            "kind": self.kind.value,
            "account_label": self.account_label,
            "source_name": self.source_name,
        }

    def masked_dict(self) -> dict[str, Any]:
        """脱敏版本：标题/文件名只给长度 + 哈希前缀。"""
        return {
            "conversation_key": self.conversation_key,
            "title": text_fingerprint(self.title),
            "kind": self.kind.value,
            "account_label": self.account_label,
            "source_name": text_fingerprint(self.source_name),
        }


@dataclass(frozen=True)
class ImportResult:
    """一次 ``parse_chat_text`` 的完整结果（实验性）。"""

    format: ImportFormat
    conversation: ImportedConversation
    messages: tuple[ImportedMessage, ...]
    stats: ImportStats
    warnings: tuple[ImportWarning, ...]
    options: ImportOptions
    encoding: str = ""

    @property
    def ok(self) -> bool:
        return len(self.messages) > 0

    @property
    def raw_messages(self) -> tuple[RawMessage, ...]:
        return tuple(item.raw for item in self.messages)

    @property
    def warning_codes(self) -> tuple[WarningCode, ...]:
        return tuple(w.code for w in self.warnings)

    def warning_messages(self) -> tuple[str, ...]:
        return tuple(w.message for w in self.warnings)

    def warning_by_code(self, code: WarningCode) -> ImportWarning | None:
        for warning in self.warnings:
            if warning.code is code:
                return warning
        return None

    def to_evidence_dict(self, *, max_entries: int = 20, include_fingerprints: bool = True) -> dict[str, Any]:
        """生成可直接写入证据 JSON 的**脱敏**摘要（绝不含消息/发送者原文）。"""
        entries: list[dict[str, Any]] = []
        for item in self.messages[: max(0, max_entries)]:
            raw = item.raw
            entry: dict[str, Any] = {
                "line_no": item.line_no,
                "sender": text_fingerprint(raw.sender),
                "is_self": raw.is_self,
                "text": text_fingerprint(raw.text),
                "timestamp": raw.timestamp.isoformat() if raw.timestamp else None,
                "timestamp_source": raw.timestamp_source.value,
                "raw_time_text": text_fingerprint(item.raw_time_text) if item.raw_time_text else None,
                "source": text_fingerprint(self.conversation.source_name),
            }
            if not include_fingerprints:
                entry.pop("sender")
                entry.pop("text")
            entries.append(entry)
        return {
            "experimental": EXPERIMENTAL,
            "format": self.format.value,
            "format_label": self.format.label_zh,
            "encoding": self.encoding,
            "conversation": self.conversation.masked_dict(),
            "stats": self.stats.to_dict(),
            "warnings": [w.to_dict() for w in self.warnings],
            "entries_preview": entries,
            "entries_preview_truncated": len(self.messages) > max(0, max_entries),
            "message_count": len(self.messages),
        }


@dataclass
class ImportSummary:
    """导入后的本地摘要（实验性；默认只统计，不含正文）。"""

    message_count: int = 0
    sender_count: int = 0
    self_messages: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    todo_count: int = 0
    day_counts: dict[str, int] = field(default_factory=dict)
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_count": self.message_count,
            "sender_count": self.sender_count,
            "self_messages": self.self_messages,
            "category_counts": dict(self.category_counts),
            "todo_count": self.todo_count,
            "day_counts": dict(self.day_counts),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 脱敏工具
# ---------------------------------------------------------------------------

def fingerprint_hash(text: str) -> str:
    """返回 UTF-8 文本的 SHA-256 十六进制摘要（不可逆）。"""
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def text_fingerprint(text: str | None) -> dict[str, Any]:
    """文本指纹：长度 + 哈希前缀（用于证据/日志，不含原文）。"""
    value = text or ""
    return {
        "length": len(value),
        "sha256_16": fingerprint_hash(value)[:16],
    }


__all__ = [
    "DEFAULT_SELF_NAMES",
    "EXPERIMENTAL",
    "EXPERIMENTAL_NOTICE",
    "EXTRA_MARKER_KEY",
    "EXTRA_SOURCE",
    "ImportFormat",
    "ImportOptions",
    "ImportResult",
    "ImportStats",
    "ImportSummary",
    "ImportWarning",
    "ImportedConversation",
    "ImportedMessage",
    "WarningCode",
    "fingerprint_hash",
    "text_fingerprint",
]
