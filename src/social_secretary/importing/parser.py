"""聊天文本解析器（实验性 P1 原型）。

支持三类输入（全部由用户主动提供，不联网、不读取 QQ 本地数据）
----------------------------------------------------------------
1. **通用行格式**（自动识别 / ``format_hint="generic"``）::

       2024-01-01 12:30:00 张三: 你好
       [2024-01-01 12:31] 李四: 收到
       2024年1月1日 12:32 王五: 多行消息第一行
       这是第二行（没有时间/发送者的行视为上一条消息的续行）
       12:33 张三: 只给时分时沿用最近的日期分隔

2. **QQ 复制/导出常见文本**（``format_hint="qq_copy"``，启发式、未真机验证）::

       消息记录（与 张三 的聊天记录）
       2024-01-01 12:30:00 张三
       你好
       2024-01-01 12:30:10 李四
       收到

3. **简易 JSON**（``format_hint="json"``）::

       [{"sender": "张三", "text": "你好", "timestamp": "2024-01-01 12:30:00"}]
       # 也接受 {"messages": [...]}、JSONL（每行一个对象）、Unix 秒/毫秒时间戳

另外提供 ``format_hint="csv"`` 的简单 CSV（``sender,text,timestamp`` 列，带/不带表头）。

输出
----
``ImportResult`` 里是 :class:`social_secretary.contracts.RawMessage`：时间为带本地时区
的 aware datetime；文本里明确给出的绝对时间 → ``TimestampSource.UI``，相对时间
（刚刚/昨天/星期X）→ ``RELATIVE``，完全没有时间 → ``COLLECTED`` 且 ``timestamp=None``
（绝不伪装成真实时间）。

健壮性
------
- 超大输入按 ``max_input_chars`` 截断（在行边界处），并给 ``input_too_large`` 警告；
- 二进制/乱码字符（U+FFFD、孤立代理项、NUL）不崩溃，给 ``mojibake_suspected`` 等警告；
- 无法识别的行、无效时间、JSON 语法错误都转成 ``ImportWarning``，不抛异常；
- 兜底的 ``try/except`` 保证异常输入只返回空结果 + ``internal_error`` 警告；
- 去重：同会话 + 同发送者 + 同文本 + 同时间（仅对有真实时间的消息做，避免误删
  无时间的真实重复消息）；
- 消息条数上限 ``max_messages``、单条长度上限 ``max_message_chars``。

本模块只依赖标准库与 ``contracts``。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from ..contracts import RawMessage, TimestampSource
from .model import (
    EXTRA_MARKER_KEY,
    ImportFormat,
    ImportOptions,
    ImportResult,
    ImportStats,
    ImportedConversation,
    ImportedMessage,
    ImportWarning,
    WarningCode,
)

__all__ = [
    "MAX_INPUT_CHARS",
    "DecodedChatText",
    "decode_text_bytes",
    "parse_chat_bytes",
    "parse_chat_text",
]

#: 默认单次导入的字符上限（约 2 M 字符，足以覆盖常见的多年度聊天复制文本）。
MAX_INPUT_CHARS = 2_000_000

#: 只有 HH:MM 且没有日期上下文时，比现在晚不超过该容差视为“今天”。
_FUTURE_TOLERANCE = timedelta(seconds=120)

#: 没有任何可识别结构时，最多在警告里保留多少个“无法识别的行号”。
_UNRECOGNIZED_SAMPLE = 10

# ---------------------------------------------------------------------------
# 正则片段
# ---------------------------------------------------------------------------

_DATE = r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
_MONTH_DAY = r"\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
_PERIOD = r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上)?"
_CLOCK = rf"{_PERIOD}\s*\d{{1,2}}\s*[:：]\s*\d{{2}}(?:\s*[:：]\s*\d{{2}})?"
_DATETIME = rf"{_DATE}(?:\s*[T ]+\s*{_CLOCK})?"
_DATETIME_MD = rf"{_MONTH_DAY}(?:\s*[T ]+\s*{_CLOCK})?"
_DATETIME_WITH_CLOCK = rf"{_DATE}\s*[T ]+\s*{_CLOCK}"
_MD_WITH_CLOCK = rf"{_MONTH_DAY}\s+{_CLOCK}"
_RELATIVE_WHEN = (
    r"(?:刚刚|\d{1,3}\s*(?:秒|分钟|小时|天)前"
    r"|今天|昨天|前天"
    r"|(?:星期|周)[一二三四五六日天])(?:\s*\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?)?"
)
_ISO_WHEN = r"\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:(?:Z|[+-]\d{2}:?\d{2}))?"
_WHEN = rf"(?:{_RELATIVE_WHEN}|{_ISO_WHEN}|{_DATETIME}|{_DATETIME_MD}|{_CLOCK})"

_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_G_TS_SENDER_COLON = re.compile(
    rf"^\[?\s*(?P<when>{_WHEN})\s*\]?\s+(?P<sender>[^:：\[\]{{}}]{{1,40}}?)\s*[:：]\s?(?P<text>.*)$"
)
_G_BRACKET_WHEN_SENDER_COLON = re.compile(
    r"^\[(?P<when>[^\]]{2,40})\]\s*(?P<sender>[^:：]{1,40}?)\s*[:：]\s?(?P<text>.*)$"
)
_G_SENDER_TS_HEADER = re.compile(
    rf"^(?P<sender>[^:：\[\]{{}}]{{1,40}}?)\s+[\[(（]?\s*(?P<when>{_WHEN})\s*[\])）]?\s*$"
)
_G_TS_SENDER_HEADER = re.compile(
    rf"^[\[(（]?\s*(?P<when>{_WHEN})\s*[\])）]?\s+(?P<sender>[^:：\s\[\]{{}}]{{1,40}})\s*$"
)
_G_SENDER_COLON = re.compile(
    r"^(?P<sender>[^:：\s\[\]]{1,40}(?:[ ][^:：\[\]]{0,20})?)\s*[:：]\s*(?P<text>.+)$"
)

_QQ_BANNER_RE = re.compile(
    r"^[=\-—*_\s]*(?P<banner>消息记录|聊天记录|QQ\s*聊天记录|消息管理器)"
    r"[（(【\[]?\s*(?P<rest>[^）)】\]]*?)\s*[）)】\]]?[=\-—*_\s]*$"
)
_QQ_DATE_SEP = re.compile(
    rf"^[=\-—*_\s]*(?P<when>{_DATE}|{_DATETIME_MD}|今天|昨天|前天)"
    rf"\s*(?:[（(]?\s*(?:星期|周)[一二三四五六日天]?\s*[）)]?)?[=\-—*_\s]*$"
)
_QQ_SENDER_TIME_HEADER = re.compile(
    rf"^(?P<sender>[^\s:：\[\]{{}}]{{1,32}})\s+\[?(?P<when>{_DATETIME}|{_DATETIME_MD}|{_CLOCK})\]?\s*$"
)
_QQ_TIME_SENDER_HEADER = re.compile(
    rf"^\[?(?P<when>{_DATETIME}|{_DATETIME_MD}|{_CLOCK})\]?\s+(?P<sender>[^\s:：\[\]{{}}]{{1,32}})\s*$"
)
_QQ_TITLE_RE = re.compile(
    r"(?:与\s*(?P<with>[^\s的]{1,40})\s*的(?:聊天|消息)记录"
    r"|(?:群聊|群|私聊|会话|联系人|好友|聊天对象)\s*[：:\s]\s*(?P<group>[^\n]{1,40}))"
)
_QQ_WITH_TITLE_RE = re.compile(r"^与\s*(?P<title>[^\s的]{1,40})\s*的(?:聊天|消息)记录$")

_DATE_SEP_GENERIC = re.compile(
    rf"^[\[(（]?\s*(?P<when>{_DATE}|{_DATETIME_MD}|今天|昨天|前天)\s*[（(]?"
    rf"\s*(?:星期|周)[一二三四五六日天]?\s*[）)]?\s*$"
)
_DATE_SEP_ANY = re.compile(
    rf"^[\[(（]?\s*(?P<when>{_DATE}|{_DATETIME_MD}|今天|昨天|前天)\s*[）)]?\s*$"
)
_PENDING_WHEN_RE = re.compile(rf"^(?:{_CLOCK}|{_DATETIME_WITH_CLOCK}|{_MD_WITH_CLOCK})$")
_CLOCK_ONLY_RE = re.compile(
    rf"^(?P<period>{_PERIOD})\s*(?P<hour>\d{{1,2}})\s*[:：]\s*(?P<minute>\d{{2}})"
    rf"(?:\s*[:：]\s*(?P<second>\d{{2}}))?$"
)
_FULL_DT_RE = re.compile(
    rf"^(?P<year>\d{{4}})\s*[-/.年]\s*(?P<month>\d{{1,2}})\s*[-/.月]\s*(?P<day>\d{{1,2}})\s*日?"
    rf"(?:\s*[T ]+\s*(?P<period>{_PERIOD})\s*(?P<hour>\d{{1,2}})\s*[:：]\s*(?P<minute>\d{{2}})"
    rf"(?:\s*[:：]\s*(?P<second>\d{{2}}))?)?$"
)
_MD_DT_RE = re.compile(
    rf"^(?P<month>\d{{1,2}})\s*[-/.月]\s*(?P<day>\d{{1,2}})\s*日?"
    rf"(?:\s*[T ]+\s*(?P<period>{_PERIOD})\s*(?P<hour>\d{{1,2}})\s*[:：]\s*(?P<minute>\d{{2}})"
    rf"(?:\s*[:：]\s*(?P<second>\d{{2}}))?)?$"
)
_FULL_DATE_ONLY_RE = re.compile(
    r"^(?P<year>\d{4})\s*[-/.年]\s*(?P<month>\d{1,2})\s*[-/.月]\s*(?P<day>\d{1,2})\s*日?$"
)
_MD_ONLY_RE = re.compile(r"^(?P<month>\d{1,2})\s*[-/.月]\s*(?P<day>\d{1,2})\s*日?$")
_RELATIVE_MINUTES_RE = re.compile(r"^(?P<n>\d{1,4})\s*(?P<unit>秒|分钟|小时|天)前$")
_DAY_WORD_RE = re.compile(rf"^(?P<word>今天|昨天|前天)\s*(?P<clock>{_CLOCK})?$")
_WEEKDAY_ONLY_RE = re.compile(
    rf"^(?:星期|周)(?P<day>[一二三四五六日天])\s*(?P<clock>{_CLOCK})?$"
)

_SKIP_LINE_RE = re.compile(
    r"^(?:以上是(?:聊天|消息)记录|导出时间|导出人|导出日期|消息管理器|"
    r"第\s*\d+\s*页|page\s*\d+|——\s*以上|={5,}|-{5,}|"
    r"(?:消息记录|聊天记录|QQ\s*聊天记录)\s*(?:[（(【\[].*[）)】\]]|[：:].*)?)$",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"^</?[a-zA-Z][a-zA-Z0-9_-]*(?:\s[^>]*)?/?>$")
_HTML_DOC_RE = re.compile(r"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]", re.IGNORECASE)
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "sender": ("sender", "from", "nickname", "name", "sendername", "发送者", "发送人", "昵称", "名字", "用户"),
    "text": ("text", "content", "message", "msg", "body", "内容", "消息", "正文", "文字"),
    "timestamp": ("timestamp", "time", "date", "datetime", "sendtime", "时间", "日期", "发送时间"),
    "is_self": ("is_self", "isself", "self", "from_self", "outgoing", "是否己方", "己方"),
}
_JSON_TEXT_KEYS = _COLUMN_ALIASES["text"]
_JSON_SENDER_KEYS = _COLUMN_ALIASES["sender"]
_JSON_TIME_KEYS = _COLUMN_ALIASES["timestamp"]
_JSON_SELF_KEYS = _COLUMN_ALIASES["is_self"]
_JSON_CONTAINER_KEYS = ("messages", "data", "records", "list", "items", "chat", "history", "消息")
_JSON_TITLE_KEYS = ("title", "conversation", "conversation_title", "name", "chat_name", "群名", "会话")
_JSON_KIND_KEYS = ("kind", "type", "conversation_kind")
_KNOWN_HINTS = {"auto", "generic", "qq_copy", "qq", "qq_export", "json", "jsonl", "csv", "lines", "plain", "text"}
_URL_SENDER_WORDS = {"http", "https", "ftp", "file", "mailto", "tel", "www"}


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------

class _WarningCollector:
    """按警告码聚合，避免逐行重复刷屏（行号只保留前 10 个）。"""

    def __init__(self) -> None:
        self._items: dict[WarningCode, ImportWarning] = {}

    def add(
        self,
        code: WarningCode,
        message: str,
        *,
        count: int = 1,
        line_no: int | None = None,
    ) -> None:
        existing = self._items.get(code)
        if existing is None:
            numbers: tuple[int, ...] = (line_no,) if line_no is not None else ()
            self._items[code] = ImportWarning(code=code, message=message, count=max(1, count), line_numbers=numbers)
            return
        numbers = existing.line_numbers
        if line_no is not None and line_no not in numbers and len(numbers) < _UNRECOGNIZED_SAMPLE:
            numbers = numbers + (line_no,)
        self._items[code] = replace(existing, count=existing.count + count, line_numbers=numbers)

    def has(self, code: WarningCode) -> bool:
        return code in self._items

    def to_tuple(self) -> tuple[ImportWarning, ...]:
        return tuple(self._items.values())


@dataclass
class _Record:
    """解析过程中的一条消息（尚未转成 RawMessage）。"""

    sender: str
    text: str
    line_no: int
    timestamp: datetime | None = None
    timestamp_source: TimestampSource = TimestampSource.COLLECTED
    raw_time_text: str | None = None
    is_self_override: bool | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _WhenResult:
    timestamp: datetime | None
    source: TimestampSource
    date_only: bool = False
    parsed_date: date | None = None
    invalid: bool = False
    assumed_date: bool = False
    relative: bool = False


@dataclass(frozen=True)
class _HeaderMatch:
    when_raw: str | None
    sender: str
    text: str
    kind: str


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def parse_chat_text(
    text: str | None,
    *,
    format_hint: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """解析用户提供的聊天文本，返回 :class:`ImportResult`（不会因输入异常崩溃）。

    参数
    ----
    text:
        已解码的聊天文本（``str``）。``None``/空串返回带 ``empty_input`` 警告的空结果。
    format_hint:
        ``"auto"``（默认）/ ``"generic"`` / ``"qq_copy"`` / ``"json"`` / ``"csv"``。
    options:
        :class:`ImportOptions`；未提供时使用默认值。``format_hint`` 参数优先。
    """
    opts = _resolve_options(options, format_hint)
    stats = ImportStats()
    warnings = _WarningCollector()
    started = time.perf_counter()

    if text is None:
        return _empty_result(
            opts, ImportFormat.UNKNOWN, stats, warnings, started,
            warning=(WarningCode.EMPTY_INPUT, "输入为空（没有可供解析的文本）"),
        )
    if not isinstance(text, str):
        raise TypeError(f"parse_chat_text 需要 str，收到 {type(text).__name__}")

    try:
        return _parse_impl(text, opts, stats, warnings, started)
    except Exception as exc:  # noqa: BLE001 - 解析器对任意输入绝不崩溃
        warnings.add(
            WarningCode.INTERNAL_ERROR,
            f"解析器内部异常（{type(exc).__name__}），已返回空结果；该输入格式可能不受支持。",
        )
        return _empty_result(opts, ImportFormat.UNKNOWN, stats, warnings, started)


def parse_chat_bytes(
    data: bytes,
    *,
    encoding: str | None = None,
    format_hint: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """解码字节内容后解析（用于工具读取用户选择的 .txt/.json 文件）。"""
    decoded = decode_text_bytes(data, preferred=encoding)
    result = parse_chat_text(decoded.text, format_hint=format_hint, options=options)
    return replace(result, encoding=decoded.encoding)


@dataclass(frozen=True)
class DecodedChatText:
    """字节解码结果（编码名 + 替换字符数，便于工具/证据展示）。"""

    text: str
    encoding: str
    replaced_chars: int = 0
    tried: tuple[str, ...] = ()


def decode_text_bytes(data: bytes, *, preferred: str | None = None) -> DecodedChatText:
    """按常见中文编码尝试解码用户文件，全部失败时用 ``utf-8`` + replacement 兜底。"""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("decode_text_bytes 需要 bytes")
    raw = bytes(data)
    tried: list[str] = []

    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.extend(["utf-16", "utf-16-le", "utf-16-be"])
    candidates.extend(["utf-8-sig", "utf-8", "gb18030", "gbk", "big5"])

    seen: set[str] = set()
    for name in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        tried.append(name)
        try:
            return DecodedChatText(raw.decode(name), name, 0, tuple(tried))
        except (UnicodeDecodeError, LookupError):
            continue

    text = raw.decode("utf-8", errors="replace")
    return DecodedChatText(text, "utf-8(replace)", text.count("\ufffd"), tuple(tried))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _resolve_options(options: ImportOptions | None, format_hint: str | None) -> ImportOptions:
    if options is None:
        return ImportOptions(format_hint=format_hint if format_hint is not None else "auto")
    if format_hint is not None:
        return replace(options, format_hint=format_hint)
    return options


def _parse_impl(
    raw_text: str,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
    started: float,
) -> ImportResult:
    text = _sanitize_text(raw_text, stats, warnings)
    stats.input_chars = len(raw_text)
    limit = opts.max_input_chars
    if len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        text = text[:cut]
        stats.truncated_input = True
        warnings.add(
            WarningCode.INPUT_TOO_LARGE,
            f"输入过大，已在约 {limit} 字符处（行边界）截断；仅解析截断后的内容。",
        )
    stats.input_lines = text.count("\n") + (1 if text else 0)

    if not text.strip():
        return _empty_result(
            opts, ImportFormat.UNKNOWN, stats, warnings, started,
            warning=(WarningCode.EMPTY_INPUT, "输入为空（清洗后没有可解析内容）"),
        )

    hint = opts.resolved_format()
    if not _hint_is_known(opts.format_hint):
        warnings.add(
            WarningCode.FORMAT_UNKNOWN,
            f"未知的格式提示 {opts.format_hint!r}，已自动识别。",
        )
        fmt, _confident = _detect_format(text)
    elif hint is ImportFormat.AUTO:
        fmt, confident = _detect_format(text)
        if not confident:
            warnings.add(
                WarningCode.FORMAT_UNKNOWN,
                "未能可靠识别文本格式，已按通用行格式尽力解析；建议手动选择格式。",
            )
    else:
        fmt = hint

    stats.format_detected = fmt

    if fmt is ImportFormat.JSON:
        records, title_hint = _parse_json_text(text, opts, stats, warnings)
    elif fmt is ImportFormat.CSV:
        records, title_hint = _parse_csv_text(text, opts, stats, warnings)
    else:
        records, title_hint = _parse_plaintext(text, fmt, opts, stats, warnings)

    return _finalize(
        records,
        fmt=fmt,
        opts=opts,
        stats=stats,
        warnings=warnings,
        title_hint=title_hint,
        started=started,
    )


def _sanitize_text(raw: str, stats: ImportStats, warnings: _WarningCollector) -> str:
    text = raw.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    controls = len(_CONTROL_RE.findall(text))
    if controls:
        text = _CONTROL_RE.sub("", text)
        warnings.add(
            WarningCode.CONTROL_CHARS_STRIPPED,
            f"文本包含 {controls} 个不可见控制字符，已移除。",
        )

    surrogates = len(_SURROGATE_RE.findall(text))
    if surrogates:
        text = _SURROGATE_RE.sub("\ufffd", text)

    stats.replaced_chars = text.count("\ufffd")
    if stats.replaced_chars:
        warnings.add(
            WarningCode.MOJIBAKE_SUSPECTED,
            f"文本包含 {stats.replaced_chars} 个替换字符（可能编码/乱码），已尽力解析；"
            "如结果异常请用 UTF-8 重新保存文件。",
        )
    return text


def _empty_result(
    opts: ImportOptions,
    fmt: ImportFormat,
    stats: ImportStats,
    warnings: _WarningCollector,
    started: float,
    *,
    warning: tuple[WarningCode, str] | None = None,
) -> ImportResult:
    if warning is not None:
        warnings.add(*warning)
    stats.format_detected = fmt
    stats.elapsed_ms = _elapsed_ms(started)
    title = opts.resolved_title()
    conversation = ImportedConversation(
        conversation_key=opts.conversation_key or _derive_conversation_key(title, opts.source_name),
        title=title,
        kind=opts.conversation_kind,
        account_label=opts.account_label,
        source_name=opts.source_name,
    )
    return ImportResult(
        format=fmt,
        conversation=conversation,
        messages=(),
        stats=stats,
        warnings=warnings.to_tuple(),
        options=opts,
    )


def _derive_conversation_key(title: str, source_name: str) -> str:
    """由会话标题 + 文件名派生稳定 key；不含正文，可安全写日志。"""
    stem = (source_name or "").strip().lower()
    digest = hashlib.sha256(f"{title}|{stem}".encode("utf-8")).hexdigest()[:16]
    return f"import:{digest}"


# ---------------------------------------------------------------------------
# 格式识别
# ---------------------------------------------------------------------------

def _hint_is_known(hint: str) -> bool:
    raw = str(hint or "").strip().lower().replace("-", "_")
    return raw in _KNOWN_HINTS or ImportFormat.coerce(hint) is not ImportFormat.AUTO


def _detect_format(text: str) -> tuple[ImportFormat, bool]:
    stripped = text.strip()
    if stripped[:1] in ("[", "{"):
        try:
            json.loads(stripped)
            return ImportFormat.JSON, True
        except (json.JSONDecodeError, ValueError):
            pass
        if _looks_like_json_lines(text):
            return ImportFormat.JSON, True

    lines = [line.strip() for line in text.split("\n") if line.strip()][:150]
    if _looks_like_csv(lines):
        return ImportFormat.CSV, True
    if any(
        _QQ_BANNER_RE.match(line)
        or line.startswith("消息记录")
        or line.startswith("聊天记录")
        or line.startswith("以上是")
        for line in lines
    ):
        return ImportFormat.QQ_COPY, True
    if any(
        _QQ_DATE_SEP.match(line) or _QQ_SENDER_TIME_HEADER.match(line) or _QQ_TIME_SENDER_HEADER.match(line)
        for line in lines
    ):
        return ImportFormat.QQ_COPY, True
    header_hits = sum(1 for line in lines if _match_header(line, ImportFormat.GENERIC) is not None)
    if header_hits:
        return ImportFormat.GENERIC, True
    return ImportFormat.GENERIC, False


def _looks_like_json_lines(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    checked = 0
    for line in lines[:20]:
        if not line.startswith("{"):
            return False
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(obj, dict):
            return False
        checked += 1
    return checked >= 2


def _looks_like_csv(lines: list[str]) -> bool:
    if not lines or "," not in lines[0]:
        return False
    cells = [cell.strip().strip('"').strip("'").lower() for cell in lines[0].split(",")]
    hits = 0
    for group in _COLUMN_ALIASES.values():
        if any(cell in group for cell in cells):
            hits += 1
    return hits >= 2


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------

def _clean_when(when_raw: str | None) -> str:
    if not when_raw:
        return ""
    text = str(when_raw).strip()
    text = text.strip("[]()（）【】")
    text = text.replace("：", ":").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _apply_period(period: str | None, hour: int) -> int:
    if not period:
        return hour
    if period in ("凌晨", "早上", "上午"):
        return 0 if hour == 12 else hour
    if period == "中午":
        return 12 if hour == 12 else hour
    if period in ("下午", "傍晚", "晚上"):
        return hour + 12 if hour < 12 else hour
    return hour


def _make_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    *,
    period: str | None,
    tz: Any,
) -> datetime | None:
    try:
        return datetime(
            year, month, day, _apply_period(period, hour), minute, second, tzinfo=tz
        )
    except ValueError:
        return None


def _parse_date_alone(when_raw: str | None, *, now: datetime) -> date | None:
    text = _clean_when(when_raw)
    if not text:
        return None
    if text in ("今天", "昨天", "前天"):
        offset = {"今天": 0, "昨天": 1, "前天": 2}[text]
        return (now - timedelta(days=offset)).date()
    match = _FULL_DATE_ONLY_RE.match(text)
    if match:
        try:
            return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except ValueError:
            return None
    match = _MD_ONLY_RE.match(text)
    if match:
        try:
            candidate = date(now.year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            return None
        if candidate > now.date() + timedelta(days=1):
            try:
                candidate = candidate.replace(year=now.year - 1)
            except ValueError:
                return None
        return candidate
    return None


def _parse_when(
    when_raw: str | None,
    *,
    now: datetime,
    current_date: date | None = None,
) -> _WhenResult:
    """把一段时间文本解析为本地时区的 aware datetime（或仅日期/无效）。"""
    text = _clean_when(when_raw)
    if not text:
        return _WhenResult(None, TimestampSource.COLLECTED, invalid=True)

    tz = now.tzinfo or datetime.now().astimezone().tzinfo

    if text == "刚刚":
        return _WhenResult(now, TimestampSource.RELATIVE, relative=True)

    relative = _RELATIVE_MINUTES_RE.match(text)
    if relative:
        kind = relative.group("unit")
        amount = int(relative.group("n"))
        delta = {
            "秒": timedelta(seconds=amount),
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
        }[kind]
        return _WhenResult(now - delta, TimestampSource.RELATIVE, relative=True)

    day_word = _DAY_WORD_RE.match(text)
    if day_word:
        offset = {"今天": 0, "昨天": 1, "前天": 2}[day_word.group("word")]
        day = (now - timedelta(days=offset)).date()
        clock = day_word.group("clock")
        if not clock:
            return _WhenResult(None, TimestampSource.RELATIVE, date_only=True, parsed_date=day, relative=True)
        stamp = _parse_clock(clock, day=day, tz=tz)
        if stamp is None:
            return _WhenResult(None, TimestampSource.RELATIVE, invalid=True)
        return _WhenResult(stamp, TimestampSource.RELATIVE, relative=True)

    weekday = _WEEKDAY_ONLY_RE.match(text)
    if weekday:
        target = _WEEKDAY_MAP[weekday.group("day")]
        delta = (now.weekday() - target) % 7
        day = (now - timedelta(days=delta)).date()
        clock = weekday.group("clock")
        if not clock:
            return _WhenResult(None, TimestampSource.RELATIVE, date_only=True, parsed_date=day, relative=True)
        stamp = _parse_clock(clock, day=day, tz=tz)
        if stamp is None:
            return _WhenResult(None, TimestampSource.RELATIVE, invalid=True)
        return _WhenResult(stamp, TimestampSource.RELATIVE, relative=True)

    full = _FULL_DT_RE.match(text)
    if full:
        if not full.group("hour"):
            parsed = _parse_date_alone(text, now=now)
            return _WhenResult(None, TimestampSource.UI, date_only=True, parsed_date=parsed)
        stamp = _make_datetime(
            int(full.group("year")), int(full.group("month")), int(full.group("day")),
            int(full.group("hour")), int(full.group("minute")), int(full.group("second") or 0),
            period=full.group("period"), tz=tz,
        )
        if stamp is None:
            return _WhenResult(None, TimestampSource.UI, invalid=True)
        return _WhenResult(stamp, TimestampSource.UI)

    short = _MD_DT_RE.match(text)
    if short:
        month, day = int(short.group("month")), int(short.group("day"))
        if not short.group("hour"):
            parsed = _parse_date_alone(text, now=now)
            return _WhenResult(None, TimestampSource.UI, date_only=True, parsed_date=parsed)
        try:
            year = now.year
            base = date(year, month, day)
            if base > now.date() + timedelta(days=1):
                base = base.replace(year=year - 1)
        except ValueError:
            return _WhenResult(None, TimestampSource.UI, invalid=True)
        stamp = _make_datetime(
            base.year, base.month, base.day,
            int(short.group("hour")), int(short.group("minute")), int(short.group("second") or 0),
            period=short.group("period"), tz=tz,
        )
        if stamp is None:
            return _WhenResult(None, TimestampSource.UI, invalid=True)
        return _WhenResult(stamp, TimestampSource.UI)

    full_date = _FULL_DATE_ONLY_RE.match(text)
    if full_date:
        parsed = _parse_date_alone(text, now=now)
        return _WhenResult(None, TimestampSource.UI, date_only=True, parsed_date=parsed)

    md_only = _MD_ONLY_RE.match(text)
    if md_only:
        parsed = _parse_date_alone(text, now=now)
        return _WhenResult(None, TimestampSource.UI, date_only=True, parsed_date=parsed)

    clock_only = _CLOCK_ONLY_RE.match(text)
    if clock_only:
        assumed = current_date is None
        base_date = current_date or now.date()
        stamp = _parse_clock(clock_only.group(0), day=base_date, tz=tz)
        if stamp is None:
            return _WhenResult(None, TimestampSource.UI, invalid=True)
        if assumed and stamp > now + _FUTURE_TOLERANCE:
            stamp = stamp - timedelta(days=1)
        return _WhenResult(stamp, TimestampSource.UI, assumed_date=assumed)

    # ISO8601（含时区 offset）等标准库可识别格式
    normalized = text.replace("/", "-")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return _WhenResult(None, TimestampSource.UI, invalid=True)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    else:
        parsed = parsed.astimezone(tz)
    return _WhenResult(parsed, TimestampSource.UI)


def _parse_clock(clock_text: str, *, day: date, tz: Any) -> datetime | None:
    match = _CLOCK_ONLY_RE.match(_clean_when(clock_text))
    if not match:
        return None
    return _make_datetime(
        day.year, day.month, day.day,
        int(match.group("hour")), int(match.group("minute")), int(match.group("second") or 0),
        period=match.group("period"), tz=tz,
    )


def _resolve_header_time(
    when_raw: str | None,
    *,
    opts: ImportOptions,
    current_date: date | None,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> tuple[datetime | None, TimestampSource, str | None]:
    if not when_raw:
        return None, TimestampSource.COLLECTED, None
    now = opts.resolved_now()
    result = _parse_when(when_raw, now=now, current_date=current_date)
    if result.invalid:
        stats.invalid_timestamps += 1
        warnings.add(
            WarningCode.INVALID_TIMESTAMP,
            "部分消息的时间无法解析，已按“无时间”处理（不伪装成真实时间）。",
        )
        return None, TimestampSource.COLLECTED, _clean_when(when_raw)
    if result.assumed_date:
        warnings.add(
            WarningCode.TIMESTAMP_ASSUMED_DATE,
            "部分消息只有时分、没有日期，已按最近日期推断（跨天聊天可能不准确）。",
        )
    if result.date_only:
        warnings.add(
            WarningCode.MISSING_TIMESTAMP,
            "部分消息只有日期（没有时分）或没有时间，已标为无时间（COLLECTED）。",
        )
        return None, TimestampSource.COLLECTED, _clean_when(when_raw)
    return result.timestamp, result.source, _clean_when(when_raw)


# ---------------------------------------------------------------------------
# 行格式解析（通用 / QQ 复制导出）
# ---------------------------------------------------------------------------

def _extract_title_hint(text: str, fmt: ImportFormat) -> str | None:
    for raw_line in text.split("\n")[:10]:
        line = raw_line.strip()
        if not line:
            continue
        banner = _QQ_BANNER_RE.match(line)
        if banner:
            rest = (banner.group("rest") or "").strip()
            candidate = _title_from_text(rest)
            if candidate:
                return candidate
        candidate = _title_from_text(line)
        if candidate:
            return candidate
    return None


def _title_from_text(text: str) -> str | None:
    if not text:
        return None
    with_match = _QQ_WITH_TITLE_RE.match(text.strip())
    if with_match:
        return with_match.group("title")[:40]
    match = _QQ_TITLE_RE.search(text)
    if match:
        candidate = match.group("with") or match.group("group") or ""
    else:
        candidate = ""
    candidate = candidate.strip().strip("「」“”\"'（）()[]【】 ")
    if not candidate or candidate in ("消息记录", "聊天记录", "消息管理器"):
        return None
    return candidate[:40]


def _is_skip_line(line: str, warnings: _WarningCollector) -> bool:
    if _QQ_BANNER_RE.match(line) or _QQ_WITH_TITLE_RE.match(line):
        return True
    if _SKIP_LINE_RE.match(line):
        return True
    if _HTML_TAG_RE.match(line):
        return True
    if _HTML_DOC_RE.search(line):
        warnings.add(
            WarningCode.UNSUPPORTED_HTML,
            "输入看起来是 HTML 网页文本，当前实验性解析器不支持 HTML；"
            "建议先另存为纯文本（.txt）再导入。",
        )
        return True
    return False


def _match_date_separator(line: str, *, now: datetime) -> date | None:
    for pattern in (_QQ_DATE_SEP, _DATE_SEP_GENERIC, _DATE_SEP_ANY):
        match = pattern.match(line)
        if match:
            return _parse_date_alone(match.group("when"), now=now)
    return None


def _match_header(line: str, fmt: ImportFormat) -> _HeaderMatch | None:
    if fmt is ImportFormat.QQ_COPY:
        match = _QQ_SENDER_TIME_HEADER.match(line)
        if match:
            return _HeaderMatch(_clean_when(match.group("when")), match.group("sender").strip(), "", "qq_sender_time")
        match = _QQ_TIME_SENDER_HEADER.match(line)
        if match:
            return _HeaderMatch(_clean_when(match.group("when")), match.group("sender").strip(), "", "qq_time_sender")

    for pattern, kind in (
        (_G_TS_SENDER_COLON, "ts_sender_colon"),
        (_G_BRACKET_WHEN_SENDER_COLON, "bracket_when_sender_colon"),
        (_G_TS_SENDER_HEADER, "ts_sender_header"),
        (_G_SENDER_TS_HEADER, "sender_ts_header"),
    ):
        match = pattern.match(line)
        if not match:
            continue
        sender = _clean_sender(match.group("sender"))
        if not sender:
            continue
        raw_time = _clean_when(match.groupdict().get("when"))
        if pattern is _G_TS_SENDER_HEADER or pattern is _G_SENDER_TS_HEADER:
            inline_text = ""
        else:
            inline_text = (match.groupdict().get("text") or "").strip()
        return _HeaderMatch(raw_time or None, sender, inline_text, kind)

    match = _G_SENDER_COLON.match(line)
    if match:
        sender = _clean_sender(match.group("sender"))
        if sender and sender.lower() not in _URL_SENDER_WORDS:
            return _HeaderMatch(None, sender, (match.group("text") or "").strip(), "sender_colon")
    return None


def _clean_sender(sender: str | None) -> str:
    value = (sender or "").strip().strip("「」“”\"'[]【】<> ")
    value = re.sub(r"\s{2,}", " ", value)
    return value[:80]


def _parse_plaintext(
    text: str,
    fmt: ImportFormat,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> tuple[list[_Record], str | None]:
    now = opts.resolved_now()
    title_hint = _extract_title_hint(text, fmt)
    records: list[_Record] = []
    current_date: date | None = None
    pending_when: str | None = None
    last_idx: int | None = None

    for line_no, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.strip()
        if not line:
            last_idx = None
            continue
        if _is_skip_line(line, warnings):
            continue

        separator_date = _match_date_separator(line, now=now)
        if separator_date is not None:
            current_date = separator_date
            pending_when = None
            continue

        if _PENDING_WHEN_RE.match(line):
            pending_when = line
            continue

        header = _match_header(line, fmt)
        if header is not None:
            if len(records) >= opts.max_messages:
                stats.truncated_messages = True
                warnings.add(
                    WarningCode.MESSAGE_LIMIT_REACHED,
                    f"消息数超过上限 {opts.max_messages}，其余内容未解析。",
                )
                break
            when_raw = header.when_raw or pending_when
            pending_when = None
            timestamp, source, raw_when = _resolve_header_time(
                when_raw, opts=opts, current_date=current_date, stats=stats, warnings=warnings
            )
            if timestamp is not None:
                current_date = timestamp.date()
            elif when_raw:
                date_context = _parse_date_alone(when_raw, now=now)
                if date_context is not None:
                    current_date = date_context
            inline_text, too_long = _cap_text(header.text, opts)
            if too_long:
                stats.truncated_texts += 1
                warnings.add(
                    WarningCode.MESSAGE_TOO_LONG,
                    f"单条消息超过 {opts.max_message_chars} 字符，已截断。",
                )
            record = _Record(
                sender=header.sender,
                text=inline_text,
                line_no=line_no,
                timestamp=timestamp,
                timestamp_source=source,
                raw_time_text=raw_when,
            )
            if header.kind.startswith("qq_"):
                record.extras["qq_style"] = header.kind
            records.append(record)
            last_idx = len(records) - 1
            continue

        if last_idx is not None:
            _append_continuation(
                records[last_idx],
                raw_line.rstrip(),
                opts=opts,
                stats=stats,
                warnings=warnings,
            )
            continue

        stats.skipped_lines += 1
        warnings.add(
            WarningCode.UNRECOGNIZED_LINE,
            "部分行无法识别（既不是时间/发送者格式，也没有可归属的上一条消息），已跳过；"
            "如内容缺失请检查格式或手动选择格式。",
            line_no=line_no,
        )
        if stats.skipped_lines >= opts.max_messages and last_idx is None and not records:
            warnings.add(
                WarningCode.NO_MESSAGES,
                "连续大量行都无法识别，可能不是受支持的聊天文本格式。",
            )
            break

    return records, title_hint


def _cap_text(text: str, opts: ImportOptions) -> tuple[str, bool]:
    if len(text) > opts.max_message_chars:
        return text[: opts.max_message_chars], True
    return text, False


def _append_continuation(
    record: _Record,
    line: str,
    *,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> None:
    if record.extras.get("_content_capped"):
        return
    candidate = f"{record.text}\n{line}" if record.text else line
    if len(candidate) > opts.max_message_chars:
        record.extras["_content_capped"] = True
        record.text = candidate[: opts.max_message_chars]
        stats.truncated_texts += 1
        warnings.add(
            WarningCode.MESSAGE_TOO_LONG,
            f"单条消息超过 {opts.max_message_chars} 字符，已截断。",
        )
        return
    record.text = candidate


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------

_LOAD_FAILED = object()


def _parse_json_text(
    text: str,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> tuple[list[_Record], str | None]:
    payload = _load_json_payload(text, warnings)
    if payload is _LOAD_FAILED:
        return [], None

    title_hint: str | None = None
    entries: Iterable[Any]
    if isinstance(payload, dict):
        title_hint = _nested_title(payload)
        container: list[Any] | None = None
        for key in _JSON_CONTAINER_KEYS:
            value = _json_get(payload, (key,))
            if isinstance(value, list):
                container = value
                break
        if container is None:
            container = [payload]
        entries = container
    elif isinstance(payload, list):
        entries = payload
    else:
        warnings.add(
            WarningCode.JSON_PARSE_FAILED,
            "JSON 顶层既不是数组也不是对象，无法解析。",
        )
        return [], None

    records: list[_Record] = []
    for index, entry in enumerate(entries):
        if len(records) >= opts.max_messages:
            stats.truncated_messages = True
            warnings.add(
                WarningCode.MESSAGE_LIMIT_REACHED,
                f"消息数超过上限 {opts.max_messages}，其余内容未解析。",
            )
            break
        record = _record_from_json_entry(entry, index, opts, stats, warnings)
        if record is not None:
            records.append(record)
    return records, title_hint


def _load_json_payload(text: str, warnings: _WarningCollector) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    objects: list[Any] = []
    for line in stripped.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if not candidate.startswith("{"):
            objects = []
            break
        try:
            objects.append(json.loads(candidate))
        except (json.JSONDecodeError, ValueError):
            objects = []
            break
    if objects:
        return objects
    warnings.add(
        WarningCode.JSON_PARSE_FAILED,
        "JSON 语法错误，无法解析（未崩溃）；请检查文件是否为有效 JSON。",
    )
    return _LOAD_FAILED


def _nested_title(payload: dict[str, Any]) -> str | None:
    for key in _JSON_TITLE_KEYS:
        value = _json_get(payload, (key,))
        if isinstance(value, dict):
            nested = _json_get(value, ("title", "name", "昵称", "名称"))
            if isinstance(nested, str) and nested.strip():
                return nested.strip()[:40]
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return None


def _record_from_json_entry(
    entry: Any,
    index: int,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> _Record | None:
    if not isinstance(entry, dict):
        stats.skipped_lines += 1
        warnings.add(
            WarningCode.JSON_RECORD_INVALID,
            "JSON 中存在非对象元素，已跳过（未崩溃）。",
        )
        return None

    text = _json_message_text(_json_get(entry, _JSON_TEXT_KEYS))
    if not text.strip():
        stats.skipped_lines += 1
        warnings.add(
            WarningCode.JSON_RECORD_INVALID,
            "JSON 中存在缺少 text/content 或内容为空的消息，已跳过。",
        )
        return None

    sender_value = _json_get(entry, _JSON_SENDER_KEYS)
    sender = _json_message_text(sender_value).strip()
    if not sender:
        sender = "未知"
        warnings.add(
            WarningCode.JSON_RECORD_INVALID,
            "JSON 中有消息缺少发送者，已用“未知”代替。",
        )

    timestamp_value = _json_get(entry, _JSON_TIME_KEYS)
    timestamp, source, raw_time = _timestamp_from_json_value(
        timestamp_value, opts=opts, stats=stats, warnings=warnings
    )

    is_self: bool | None = None
    for key in _JSON_SELF_KEYS:
        value = _json_get(entry, (key,))
        if value is not None:
            is_self = _coerce_bool(value)
            break

    text, too_long = _cap_text(text.strip(), opts)
    if too_long:
        stats.truncated_texts += 1
        warnings.add(
            WarningCode.MESSAGE_TOO_LONG,
            f"单条消息超过 {opts.max_message_chars} 字符，已截断。",
        )

    return _Record(
        sender=sender,
        text=text,
        line_no=index + 1,
        timestamp=timestamp,
        timestamp_source=source,
        raw_time_text=raw_time,
        is_self_override=is_self,
        extras={"json_index": index},
    )


def _timestamp_from_json_value(
    value: Any,
    *,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> tuple[datetime | None, TimestampSource, str | None]:
    if value is None or value == "":
        return None, TimestampSource.COLLECTED, None
    now = opts.resolved_now()
    tz = now.tzinfo or datetime.now().astimezone().tzinfo

    if isinstance(value, bool):
        stats.invalid_timestamps += 1
        warnings.add(WarningCode.INVALID_TIMESTAMP, "JSON 中存在无法解析的时间字段，已按无时间处理。")
        return None, TimestampSource.COLLECTED, None

    if isinstance(value, (int, float)):
        stamp = _epoch_to_datetime(float(value), tz)
        if stamp is None:
            stats.invalid_timestamps += 1
            warnings.add(WarningCode.INVALID_TIMESTAMP, "JSON 中的 Unix 时间戳超出合理范围，已按无时间处理。")
            return None, TimestampSource.COLLECTED, str(value)
        return stamp, TimestampSource.UI, str(value)

    if isinstance(value, dict):
        for key in ("seconds", "sec", "ms", "milliseconds", "value", "time", "timestamp", "iso"):
            nested = _json_get(value, (key,))
            if nested is not None and nested is not value:
                return _timestamp_from_json_value(nested, opts=opts, stats=stats, warnings=warnings)
        stats.invalid_timestamps += 1
        warnings.add(WarningCode.INVALID_TIMESTAMP, "JSON 中存在无法解析的时间对象，已按无时间处理。")
        return None, TimestampSource.COLLECTED, None

    if isinstance(value, str):
        result = _parse_when(value, now=now)
        raw = _clean_when(value)
        if result.invalid:
            stats.invalid_timestamps += 1
            warnings.add(WarningCode.INVALID_TIMESTAMP, "JSON 中存在无法解析的时间文本，已按无时间处理。")
            return None, TimestampSource.COLLECTED, raw
        if result.date_only:
            warnings.add(
                WarningCode.MISSING_TIMESTAMP,
                "JSON 中部分消息只有日期没有时分，已标为无时间（COLLECTED）。",
            )
            return None, TimestampSource.COLLECTED, raw
        if result.assumed_date:
            warnings.add(
                WarningCode.TIMESTAMP_ASSUMED_DATE,
                "JSON 中部分消息只有时分，已按最近日期推断。",
            )
        return result.timestamp, result.source, raw

    stats.invalid_timestamps += 1
    warnings.add(WarningCode.INVALID_TIMESTAMP, "JSON 中存在不支持的时间类型，已按无时间处理。")
    return None, TimestampSource.COLLECTED, None


def _epoch_to_datetime(value: float, tz: Any) -> datetime | None:
    seconds = value / 1000.0 if abs(value) > 1e12 else value
    try:
        stamp = datetime.fromtimestamp(seconds, tz=tz)
    except (OverflowError, OSError, ValueError):
        return None
    if not 1970 <= stamp.year <= 2100:
        return None
    return stamp


def _json_get(obj: dict[str, Any], keys: Iterable[str]) -> Any:
    if not isinstance(obj, dict):
        return None
    lowered = {str(key).lower(): value for key, value in obj.items()}
    for key in keys:
        if key in obj:
            return obj[key]
        value = lowered.get(key.lower())
        if value is not None:
            return value
    return None


def _json_message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                nested = _json_get(item, ("text", "content", "name", "nickname", "data"))
                if isinstance(nested, str) and nested:
                    parts.append(nested)
        return "".join(parts)
    if isinstance(value, dict):
        nested = _json_get(value, ("text", "content", "name", "nickname", "title", "value"))
        return _json_message_text(nested)
    return ""


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "是", "自己", "self", "己方"):
            return True
        if normalized in ("false", "0", "no", "n", "否", "对方", "other"):
            return False
    return None


# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------

def _parse_csv_text(
    text: str,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
) -> tuple[list[_Record], str | None]:
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        warnings.add(
            WarningCode.CSV_PARSE_FAILED,
            f"CSV 解析失败（{type(exc).__name__}），无法解析该文件。",
        )
        return [], None

    cleaned_rows = [
        [cell.strip() for cell in row]
        for row in rows
        if any((cell or "").strip() for cell in row)
    ]
    if not cleaned_rows:
        warnings.add(WarningCode.CSV_PARSE_FAILED, "CSV 没有可解析的行。")
        return [], None

    header_map = _match_csv_header(cleaned_rows[0])
    if header_map is not None:
        data_rows = cleaned_rows[1:]
    else:
        warnings.add(
            WarningCode.CSV_HEADER_ASSUMED,
            "CSV 未检测到表头，已按列顺序（时间,发送者,内容 或 发送者,内容）尽力解析。",
        )
        header_map = _assume_csv_columns(cleaned_rows[0])
        data_rows = cleaned_rows
        if header_map is None:
            warnings.add(
                WarningCode.CSV_PARSE_FAILED,
                "CSV 列数/列顺序无法识别，解析失败（未崩溃）。",
            )
            return [], None

    sender_idx = header_map.get("sender")
    text_idx = header_map.get("text")
    time_idx = header_map.get("timestamp")
    self_idx = header_map.get("is_self")
    if sender_idx is None or text_idx is None:
        warnings.add(WarningCode.CSV_PARSE_FAILED, "CSV 缺少发送者/内容列，解析失败（未崩溃）。")
        return [], None

    now = opts.resolved_now()
    records: list[_Record] = []
    for index, row in enumerate(data_rows):
        if len(records) >= opts.max_messages:
            stats.truncated_messages = True
            warnings.add(
                WarningCode.MESSAGE_LIMIT_REACHED,
                f"消息数超过上限 {opts.max_messages}，其余内容未解析。",
            )
            break
        def cell(column: int | None) -> str:
            if column is None or column >= len(row):
                return ""
            return row[column]

        body = cell(text_idx).strip()
        sender = cell(sender_idx).strip() or "未知"
        if not body:
            stats.skipped_lines += 1
            warnings.add(WarningCode.JSON_RECORD_INVALID, "CSV 中存在内容为空的行，已跳过。")
            continue
        timestamp: datetime | None = None
        source = TimestampSource.COLLECTED
        raw_time: str | None = None
        raw_time_value = cell(time_idx)
        if raw_time_value:
            timestamp, source, raw_time = _timestamp_from_json_value(
                raw_time_value, opts=opts, stats=stats, warnings=warnings
            )
        is_self = _coerce_bool(cell(self_idx)) if self_idx is not None else None
        body, too_long = _cap_text(body, opts)
        if too_long:
            stats.truncated_texts += 1
            warnings.add(
                WarningCode.MESSAGE_TOO_LONG,
                f"单条消息超过 {opts.max_message_chars} 字符，已截断。",
            )
        records.append(
            _Record(
                sender=sender,
                text=body,
                line_no=index + 1,
                timestamp=timestamp,
                timestamp_source=source if timestamp is not None else TimestampSource.COLLECTED,
                raw_time_text=raw_time,
                is_self_override=is_self,
                extras={"csv_row": index + 1},
            )
        )
    return records, None


def _match_csv_header(row: list[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(row):
        normalized = cell.strip().lower()
        for canonical, aliases in _COLUMN_ALIASES.items():
            if canonical in mapping:
                continue
            if normalized in aliases:
                mapping[canonical] = index
    if "sender" in mapping and "text" in mapping:
        return mapping
    return None


def _assume_csv_columns(row: list[str]) -> dict[str, int] | None:
    if len(row) >= 3 and _parse_when(row[0], now=datetime.now().astimezone()).timestamp is not None:
        return {"timestamp": 0, "sender": 1, "text": 2}
    if len(row) >= 3 and _parse_when(row[0], now=datetime.now().astimezone()).parsed_date is not None:
        return {"timestamp": 0, "sender": 1, "text": 2}
    if len(row) >= 2:
        return {"sender": 0, "text": 1}
    return None


# ---------------------------------------------------------------------------
# 汇总：RawMessage 构建 / 去重 / 统计
# ---------------------------------------------------------------------------

def _dedupe_bucket(timestamp: datetime | None, source: TimestampSource) -> str | None:
    """去重时间桶：与 core.dedupe.compute_content_hash 的 timestamp_bucket 一致。"""
    if timestamp is None or source is TimestampSource.COLLECTED:
        return None
    aware = timestamp if timestamp.tzinfo is not None else timestamp.astimezone()
    return aware.strftime("%Y-%m-%dT%H:%M")


def dedupe_key(
    conversation_key: str,
    sender: str,
    is_self: bool,
    text: str,
    bucket: str,
) -> str:
    """去重键：sha256(conversation_key|sender|is_self|text|timestamp_bucket)。

    与 ``social_secretary.core.dedupe.compute_content_hash`` 的公式一致（便于未来
    接入核心层流水线）；仅在消息有真实时间时使用。
    """
    payload = "|".join(
        [str(conversation_key), str(sender), "1" if is_self else "0", str(text), str(bucket)]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _finalize(
    records: list[_Record],
    *,
    fmt: ImportFormat,
    opts: ImportOptions,
    stats: ImportStats,
    warnings: _WarningCollector,
    title_hint: str | None,
    started: float,
) -> ImportResult:
    title = opts.conversation_title.strip() or (title_hint or "").strip() or "导入的聊天记录"
    conversation_key = opts.conversation_key or _derive_conversation_key(title, opts.source_name)
    self_names = opts.resolved_self_names()
    self_names_lower = {name.lower() for name in self_names}
    seen: set[str] = set()
    messages: list[ImportedMessage] = []
    senders: set[str] = set()
    timestamped = 0
    self_count = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for record in records:
        if len(messages) >= opts.max_messages:
            stats.truncated_messages = True
            warnings.add(
                WarningCode.MESSAGE_LIMIT_REACHED,
                f"消息数超过上限 {opts.max_messages}，其余内容未解析。",
            )
            break
        body = (record.text or "").strip()
        if not body:
            stats.skipped_lines += 1
            warnings.add(
                WarningCode.EMPTY_MESSAGE,
                "存在没有内容的消息（可能只有昵称/时间或图片占位），已跳过。",
            )
            continue
        if len(body) > opts.max_message_chars:
            body = body[: opts.max_message_chars]
            stats.truncated_texts += 1
            warnings.add(
                WarningCode.MESSAGE_TOO_LONG,
                f"单条消息超过 {opts.max_message_chars} 字符，已截断。",
            )

        sender = (record.sender or "").strip() or "未知"
        is_self = record.is_self_override
        if is_self is None:
            is_self = sender in self_names or sender.lower() in self_names_lower

        bucket = _dedupe_bucket(record.timestamp, record.timestamp_source)
        if opts.dedupe and bucket is not None:
            key = dedupe_key(conversation_key, sender, is_self, body, bucket)
            if key in seen:
                stats.duplicate_messages += 1
                continue
            seen.add(key)

        extras: dict[str, Any] = {
            EXTRA_MARKER_KEY: True,
            "import_format": fmt.value,
            "import_source_name": opts.source_name,
            "import_line_no": record.line_no,
            "import_raw_time": record.raw_time_text,
            "import_experimental": True,
        }
        extras.update(record.extras)
        locator = f"import:{(opts.source_name or 'text')}:{record.line_no}"
        raw = RawMessage(
            conversation_key=conversation_key,
            sender=sender,
            is_self=is_self,
            text=body,
            timestamp=record.timestamp,
            timestamp_source=record.timestamp_source,
            raw_time_text=record.raw_time_text,
            external_id=None,
            source_locator=locator,
            extras=extras,
        )
        messages.append(ImportedMessage(raw=raw, line_no=record.line_no, raw_time_text=record.raw_time_text))
        senders.add(sender)
        if raw.timestamp is not None:
            timestamped += 1
            first_ts = raw.timestamp if first_ts is None or raw.timestamp < first_ts else first_ts
            last_ts = raw.timestamp if last_ts is None or raw.timestamp > last_ts else last_ts
        if is_self:
            self_count += 1

    stats.parsed_messages = len(messages)
    stats.self_messages = self_count
    stats.other_messages = len(messages) - self_count
    stats.sender_count = len(senders)
    stats.timestamped_messages = timestamped
    stats.untimed_messages = len(messages) - timestamped
    stats.format_detected = fmt
    stats.elapsed_ms = _elapsed_ms(started)

    if stats.duplicate_messages:
        warnings.add(
            WarningCode.DUPLICATES_REMOVED,
            f"已移除 {stats.duplicate_messages} 条重复消息（同会话 + 同发送者 + 同文本 + 同时间）。",
        )
    if stats.untimed_messages:
        warnings.add(
            WarningCode.MISSING_TIMESTAMP,
            f"{stats.untimed_messages} 条消息没有可用时间，已标为无时间（COLLECTED，不会伪装成真实时间）。",
        )
    if messages:
        warnings.add(
            WarningCode.SELF_SENDER_ASSUMED,
            "无法可靠判断哪条昵称是“你”：仅把昵称等于 "
            f"{'/'.join(self_names) or '（空）'} 的消息标为己方；如不正确，请在选项里填写自己的昵称。",
        )
    else:
        warnings.add(
            WarningCode.NO_MESSAGES,
            "没有解析出任何消息：请检查文件格式，或手动选择格式后重试。",
        )

    conversation = ImportedConversation(
        conversation_key=conversation_key,
        title=title,
        kind=opts.conversation_kind,
        account_label=opts.account_label,
        source_name=opts.source_name,
    )
    return ImportResult(
        format=fmt,
        conversation=conversation,
        messages=tuple(messages),
        stats=stats,
        warnings=warnings.to_tuple(),
        options=opts,
    )
