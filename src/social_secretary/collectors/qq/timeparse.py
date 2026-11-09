"""QQ 界面时间文本解析（只读、无副作用）。

QQ NT 的气泡时间在 UIA 树里可能出现在「气泡内的 Text 节点」或「消息区里的独立
时间分隔节点」，且格式随今天/昨天/星期/日期而变化。真实可读性**未在真机验证**
（见 docs/feasibility/01_qq_uia.md），因此：

- 能解析成绝对时间的 -> ``TimestampSource.UI``
- 只能解析成相对时间（刚刚/N分钟前）的 -> ``TimestampSource.RELATIVE``
- 完全读不到时间的 -> 由上层标 ``TimestampSource.COLLECTED``（绝不伪装成真实时间）

所有时间均为带时区的本地时间。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...contracts import TimestampSource

# 未来时间容差：界面显示 HH:MM 但比当前时间略晚时，视为「昨天的同一时刻」。
_FUTURE_TOLERANCE = timedelta(seconds=120)

_DAY_PREFIX_RE = re.compile(r"^(今天|昨天|前天)\s*")
_WEEKDAY_RE = re.compile(r"^(?:星期|周)([一二三四五六日天])\s*")
_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_FULL_DATE_TIME_RE = re.compile(
    r"^(?P<year>\d{4})\s*[-/年.]\s*(?P<month>\d{1,2})\s*[-/月.]\s*(?P<day>\d{1,2})\s*日?"
    r"(?:\s*(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?:[:：](?P<second>\d{2}))?)?$"
)
_SHORT_DATE_TIME_RE = re.compile(
    r"^(?P<month>\d{1,2})\s*[-/月.]\s*(?P<day>\d{1,2})\s*日?"
    r"(?:\s*(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?:[:：](?P<second>\d{2}))?)?$"
)
_DATE_ONLY_RE = re.compile(r"^(?P<year>\d{4})\s*[-/年.]\s*(?P<month>\d{1,2})\s*[-/月.]\s*(?P<day>\d{1,2})\s*日?$")
_TIME_RE = re.compile(
    r"^(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?:[:：](?P<second>\d{2}))?$"
)
_RELATIVE_RES = (
    (re.compile(r"^刚刚$"), "just_now"),
    (re.compile(r"^(?P<n>\d{1,3})\s*秒前$"), "seconds_ago"),
    (re.compile(r"^(?P<n>\d{1,3})\s*分钟前$"), "minutes_ago"),
    (re.compile(r"^(?P<n>\d{1,3})\s*小时前$"), "hours_ago"),
    (re.compile(r"^(?P<n>\d{1,2})\s*天前$"), "days_ago"),
)


@dataclass(frozen=True)
class ParsedTimestamp:
    """一次时间文本解析结果。"""

    timestamp: datetime | None
    source: TimestampSource
    raw_text: str
    date_only: bool = False


def _ensure_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _combine(day: datetime, hour: int, minute: int, second: int, *, now: datetime) -> datetime:
    candidate = day.replace(
        hour=hour, minute=minute, second=second, microsecond=0, tzinfo=now.tzinfo
    )
    return candidate


def _apply_period(period: str | None, hour: int) -> int:
    if not period:
        return hour
    if period in ("凌晨", "早上", "上午"):
        if hour == 12:
            return 0
        return hour
    if period in ("中午",):
        return 12 if hour == 12 else hour
    if period in ("下午", "傍晚", "晚上"):
        if hour < 12:
            return hour + 12
        return hour
    return hour


def _time_from_parts(
    match: re.Match[str],
    *,
    now: datetime,
    day: datetime | None = None,
    tolerance: timedelta = _FUTURE_TOLERANCE,
) -> tuple[int, int, int]:
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second") or 0)
    hour = _apply_period(match.group("period"), hour)
    if day is not None and tolerance is not None:
        candidate = _combine(day, hour, minute, second, now=now)
        # 「HH:MM」比当前时间晚太多时，按昨天处理（QQ 对当天消息通常只显示 HH:MM）。
        if candidate > now + tolerance:
            candidate = candidate - timedelta(days=1)
            return candidate.hour, candidate.minute, candidate.second
        return hour, minute, second
    return hour, minute, second


def parse_time_text(
    raw: str,
    *,
    now: datetime,
    base_day: datetime | None = None,
) -> ParsedTimestamp | None:
    """尝试把一个 UIA 节点文本解析成时间。

    返回 ``None`` 表示「这段文本不是时间」，调用方不要把它当时间节点。
    ``base_day`` 用于处理消息区「日期分隔节点」之后的纯 ``HH:MM`` 气泡时间。
    """

    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > 48:
        return None
    now = _ensure_aware(now)

    # 1) 相对时间
    for pattern, kind in _RELATIVE_RES:
        match = pattern.match(text)
        if not match:
            continue
        if kind == "just_now":
            return ParsedTimestamp(now, TimestampSource.RELATIVE, text)
        n = int(match.group("n"))
        if kind == "seconds_ago":
            ts = now - timedelta(seconds=n)
        elif kind == "minutes_ago":
            ts = now - timedelta(minutes=n)
        elif kind == "hours_ago":
            ts = now - timedelta(hours=n)
        else:
            ts = now - timedelta(days=n)
        return ParsedTimestamp(ts, TimestampSource.RELATIVE, text)

    # 2) 今天/昨天/前天 + 时间
    day_match = _DAY_PREFIX_RE.match(text)
    if day_match:
        day_word = day_match.group(1)
        rest = text[day_match.end() :].strip()
        time_match = _TIME_RE.match(rest)
        if time_match:
            hour = _apply_period(time_match.group("period"), int(time_match.group("hour")))
            minute = int(time_match.group("minute"))
            second = int(time_match.group("second") or 0)
            days_back = {"今天": 0, "昨天": 1, "前天": 2}[day_word]
            day = (now - timedelta(days=days_back)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if days_back == 0:
                ts = _combine(day, hour, minute, second, now=now)
                if ts > now + _FUTURE_TOLERANCE:
                    ts -= timedelta(days=1)
            else:
                ts = _combine(day, hour, minute, second, now=now)
            return ParsedTimestamp(ts, TimestampSource.UI, text)

    # 3) 星期 + 时间
    week_match = _WEEKDAY_RE.match(text)
    if week_match:
        rest = text[week_match.end() :].strip()
        time_match = _TIME_RE.match(rest)
        if time_match:
            target = _WEEKDAY_MAP[week_match.group(1)]
            hour = _apply_period(time_match.group("period"), int(time_match.group("hour")))
            minute = int(time_match.group("minute"))
            second = int(time_match.group("second") or 0)
            delta = (now.weekday() - target) % 7
            day = (now - timedelta(days=delta)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            ts = _combine(day, hour, minute, second, now=now)
            if ts > now + _FUTURE_TOLERANCE:
                ts -= timedelta(days=7)
            return ParsedTimestamp(ts, TimestampSource.UI, text)

    # 4) 完整日期 [+ 时间]
    full = _FULL_DATE_TIME_RE.match(text)
    if full:
        year = int(full.group("year"))
        month = int(full.group("month"))
        day = int(full.group("day"))
        if full.group("hour") is None:
            return ParsedTimestamp(None, TimestampSource.UI, text, date_only=True)
        hour = _apply_period(full.group("period"), int(full.group("hour")))
        minute = int(full.group("minute"))
        second = int(full.group("second") or 0)
        try:
            ts = datetime(year, month, day, hour, minute, second, tzinfo=now.tzinfo)
        except ValueError:
            return None
        return ParsedTimestamp(ts, TimestampSource.UI, text)

    # 5) 月日 [+ 时间]（界面常见 "10月31日 12:30"）
    short = _SHORT_DATE_TIME_RE.match(text)
    if short:
        month = int(short.group("month"))
        day = int(short.group("day"))
        if short.group("hour") is None:
            return ParsedTimestamp(None, TimestampSource.UI, text, date_only=True)
        hour = _apply_period(short.group("period"), int(short.group("hour")))
        minute = int(short.group("minute"))
        second = int(short.group("second") or 0)
        year = now.year
        try:
            ts = datetime(year, month, day, hour, minute, second, tzinfo=now.tzinfo)
        except ValueError:
            return None
        if ts > now + timedelta(days=1):
            # 跨年聊天：界面上的 12月31日 在当前 1 月时应视为去年。
            try:
                ts = ts.replace(year=year - 1)
            except ValueError:
                return None
        return ParsedTimestamp(ts, TimestampSource.UI, text)

    # 6) 只有日期（日期分隔符）
    if _DATE_ONLY_RE.match(text):
        return ParsedTimestamp(None, TimestampSource.UI, text, date_only=True)

    # 7) 纯时间 HH:MM（可能属于 base_day 分隔符上下文）
    time_match = _TIME_RE.match(text)
    if time_match and (base_day is not None or len(text) <= 8):
        hour = _apply_period(time_match.group("period"), int(time_match.group("hour")))
        minute = int(time_match.group("minute"))
        second = int(time_match.group("second") or 0)
        day = base_day if base_day is not None else now
        ts = _combine(day, hour, minute, second, now=now)
        if base_day is None and ts > now + _FUTURE_TOLERANCE:
            ts -= timedelta(days=1)
        return ParsedTimestamp(ts, TimestampSource.UI, text)

    return None


__all__ = ["ParsedTimestamp", "parse_time_text"]
