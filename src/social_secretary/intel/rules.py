"""中文规则分类器与待办提取器（纯本地、零网络、可解释）。

设计原则
--------
1. 全部规则集中在模块级数据表（``IMPORTANT_SIGNAL_RULES`` / ``FYI_SIGNAL_RULES`` /
   ``_STRONG_ACTION`` / ``_REL_DAY_META`` 等）与配置 dataclass 中，便于测试与调参。
2. 不联网、不读写文件、不使用第三方库，只依赖标准库与 :mod:`social_secretary.contracts`。
3. 每条 ``CategoryDecision.reasons`` 都是简体中文，形如 ``"包含请求/委托（请，+1.5）"``，
   用户/日志可以解释“为什么这样分类”。
4. 待办提取采用「动作信号 + 时间/截止/紧急信号」组合判定；仅有一条普通聊天句子时不会
   生成待办，避免把闲聊误判为任务。

对外接口
--------
- ``RuleClassifier``：无参可构造，实现 :class:`social_secretary.contracts.Classifier`。
- ``RuleTodoExtractor``：无参可构造，实现 :class:`social_secretary.contracts.TodoExtractor`。
  为便于测试，支持两种 ``now`` 注入方式（后者优先）：

  * 构造时注入：``RuleTodoExtractor(now_provider=lambda: fixed_dt)``；
  * 调用时注入：``extractor.extract(raw, now=fixed_dt)``。

  两者都不传时使用 ``datetime.now().astimezone()``（本地时区）。``now`` 可以是
  aware 或 naive datetime；naive 会被当作本地时间。
- ``parse_due_text``：把一段中文文本解析成 ``ParsedDue``（测试/调试/诊断用）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable

from ..contracts import (
    Category,
    CategoryDecision,
    ConversationKind,
    ExtractedTodo,
    Priority,
    RawMessage,
)

__all__ = [
    "ClassifierConfig",
    "RuleClassifier",
    "TodoConfig",
    "RuleTodoExtractor",
    "ParsedDue",
    "parse_due_text",
    "IMPORTANT_SIGNAL_RULES",
    "FYI_SIGNAL_RULES",
    "SignalRule",
]


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _ensure_aware(moment: datetime) -> datetime:
    """naive datetime 按本地时区补全时区；aware 原样返回。"""
    if moment.tzinfo is None:
        return moment.astimezone()
    return moment


def _format_weight(weight: float) -> str:
    return f"{weight:+.1f}"


# ---------------------------------------------------------------------------
# 消息分类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalRule:
    """一条可解释的文本信号规则。"""

    name: str
    pattern: re.Pattern[str]
    weight: float
    reason: str


def _rule(name: str, pattern: str, weight: float, reason: str) -> SignalRule:
    return SignalRule(name=name, pattern=re.compile(pattern), weight=weight, reason=reason)


# 重要信号：命中后累加分数（权重见 docs/design/intel.md 规则表）
IMPORTANT_SIGNAL_RULES: tuple[SignalRule, ...] = (
    _rule("direct_at", r"@\s*我", 3.0, "消息直接 @ 到我"),
    _rule("request", r"请|麻烦|劳烦|拜托", 1.5, "包含请求/委托词"),
    _rule(
        "instruction",
        r"需要你|需要您|希望你|请你|请您|务必|必须|一定要|千万",
        2.0,
        "明确要求我处理",
    ),
    _rule(
        "urgency",
        r"尽快|马上|立刻|立即|赶紧|抓紧|急用|加急|速回",
        2.0,
        "包含紧急/时效要求",
    ),
    _rule(
        "action",
        r"回复|答复|确认|回我|回一下|回个话|告诉我|通知我|发我|发给我|提交|交一下"
        r"|安排|准备|参加|报名|开会|会议|面试|考试|答辩|汇报|出差|联系我|打电话|处理|完成",
        1.2,
        "包含需要我跟进的动作",
    ),
    _rule(
        "deadline",
        r"截止|ddl|deadline|(?<=[0-9点号日天周月晚早午底末班一二三四五六])前",
        1.5,
        "包含截止时间",
    ),
    _rule(
        "time",
        r"今天|明天|后天|大后天|本周|这周|下周|周[一二三四五六日天]|星期[一二三四五六日天]"
        r"|礼拜[一二三四五六日天]|\d{1,2}月\d{1,2}[日号]|\d{1,2}点|\d{1,2}[:：]\d{2}"
        r"|上午|下午|晚上|中午|早上|今晚|明晚",
        1.0,
        "包含具体时间信息",
    ),
    _rule(
        "question",
        r"[？?]|(?:吗|呢)\s*$|怎么|如何|是否|能不能|可以吗|行不行|几点|什么时候"
        r"|什么|哪里|哪个|谁|多少",
        0.8,
        "包含问句，可能在等待回复",
    ),
)

# 低价值信号：命中后减分（公告/广告/闲聊等）
FYI_SIGNAL_RULES: tuple[SignalRule, ...] = (
    _rule(
        "broadcast",
        r"@\s*所有人|所有人|全体成员|全员|各位群友",
        -1.5,
        "群发/面向所有人，并非专门找我",
    ),
    _rule(
        "announcement",
        r"群公告|公告|【通知】|【公告】|系统消息|转发|转自|分享",
        -1.5,
        "公告/转发类内容",
    ),
    _rule("notice_colon", r"(?:重要)?通知\s*[:：]", -1.5, "公告/通知类内容"),
    _rule("link", r"https?://|www\.|[A-Za-z0-9-]+\.(?:com|cn|net|org)", -1.2, "包含外部链接"),
    _rule(
        "marketing",
        r"抽奖|中奖|领奖|红包|拼单|拼团|砍价|砍一刀|接龙|投票|点赞|助力|优惠|促销"
        r"|折扣|团购|广告|推广|入驻|招聘|兼职|课程|限时|秒杀|领取|福利",
        -2.5,
        "营销/抽奖等低价值内容",
    ),
    _rule(
        "chat_noise",
        r"哈哈|嘿嘿|嘻嘻|233|😂|🤣|😄|😁|\[图片\]|\[表情\]|\[视频\]|\[语音\]",
        -2.5,
        "闲聊/表情/非文本内容",
    ),
    _rule(
        "greeting_probe",
        r"在吗|在不在|吃了吗|睡了没|忙不忙|忙吗",
        -3.0,
        "寒暄试探，没有实质信息",
    ),
    _rule(
        "greeting",
        r"晚安|早安|早上好|晚上好|生日快乐|节日快乐|新年快乐",
        -1.0,
        "问候祝福",
    ),
)

# 简短确认/寒暄：整条消息只由这些词和标点组成时判定为“仅了解”
_ACK_TOKEN = (
    r"(?:好的?|收到|了解|知道了|明白|谢谢|感谢|辛苦了|嗯+|哦+|OK|ok|行|可以|没问题"
    r"|晚安|早安|早上好|晚上好|生日快乐)"
)
_SHORT_ACK = re.compile(
    rf"^[\s，,、。.!！~～]*{_ACK_TOKEN}(?:[\s，，,、。.!！~～]*{_ACK_TOKEN})*[\s，,、。.!！~～]*$"
)


@dataclass(frozen=True)
class ClassifierConfig:
    """分类阈值与上下文权重（集中配置，便于测试调参）。"""

    important_threshold: float = 4.0
    fyi_threshold: float = -2.0
    self_message_weight: float = -3.0
    high_priority_weight: float = 2.5
    low_priority_weight: float = -2.0
    private_chat_weight: float = 0.5
    group_chat_weight: float = -0.3
    mail_chat_weight: float = 0.0
    self_name_weight: float = 3.0
    max_reasons: int = 10


class RuleClassifier:
    """纯本地中文规则分类器（实现 ``contracts.Classifier``）。

    用法::

        classifier = RuleClassifier()                       # 无参可构造
        classifier = RuleClassifier(self_names=["张三"])     # 可选：直接点名我的名字
        decision = classifier.classify(
            raw,
            conversation_title="项目群",
            conversation_kind=ConversationKind.GROUP,
            conversation_priority=Priority.HIGH,
        )
    """

    def __init__(
        self,
        config: ClassifierConfig | None = None,
        *,
        self_names: Iterable[str] = (),
    ) -> None:
        self.config = config or ClassifierConfig()
        self.self_names = tuple(name.strip() for name in self_names if name and name.strip())
        self._self_name_rules = tuple(
            SignalRule(
                name=f"self_name:{name}",
                pattern=re.compile(re.escape(name)),
                weight=self.config.self_name_weight,
                reason=f"消息点名我（{name}）",
            )
            for name in self.self_names
        )

    # -- 实现 contracts.Classifier -------------------------------------
    def classify(
        self,
        raw: RawMessage,
        *,
        conversation_title: str,
        conversation_kind: ConversationKind,
        conversation_priority: Priority = Priority.NORMAL,
    ) -> CategoryDecision:
        text = (raw.text or "").strip()
        reasons: list[str] = []
        if not text:
            return CategoryDecision(
                category=Category.NORMAL,
                reasons=("消息没有文本内容（可能是图片/表情/文件），默认普通",),
                score=0.0,
            )

        score = 0.0
        for rule in IMPORTANT_SIGNAL_RULES:
            match = rule.pattern.search(text)
            if match is not None:
                score += rule.weight
                reasons.append(self._explain(rule, match.group(0)))
        for rule in self._self_name_rules:
            match = rule.pattern.search(text)
            if match is not None:
                score += rule.weight
                reasons.append(self._explain(rule, match.group(0)))
        for rule in FYI_SIGNAL_RULES:
            match = rule.pattern.search(text)
            if match is not None:
                score += rule.weight
                reasons.append(self._explain(rule, match.group(0)))

        if _SHORT_ACK.match(text):
            score -= 2.5
            reasons.append("整条消息仅为简短确认/寒暄（-2.5）")

        # 上下文信号：己方消息 / 会话类型 / 会话优先级
        if raw.is_self:
            score += self.config.self_message_weight
            reasons.append(
                f"己方发送的消息仅作上下文，予以降级（{_format_weight(self.config.self_message_weight)}）"
            )
        if conversation_kind is ConversationKind.GROUP:
            score += self.config.group_chat_weight
            reasons.append(f"群聊消息（{_format_weight(self.config.group_chat_weight)}）")
        elif conversation_kind is ConversationKind.PRIVATE:
            score += self.config.private_chat_weight
            reasons.append(f"私聊消息（{_format_weight(self.config.private_chat_weight)}）")
        elif conversation_kind is ConversationKind.MAIL:
            score += self.config.mail_chat_weight
            reasons.append(f"邮件消息（{_format_weight(self.config.mail_chat_weight)}）")

        if conversation_priority is Priority.HIGH:
            score += self.config.high_priority_weight
            reasons.append(
                f"该会话被标记为高优先级（{_format_weight(self.config.high_priority_weight)}）"
            )
        elif conversation_priority is Priority.LOW:
            score += self.config.low_priority_weight
            reasons.append(
                f"该会话被标记为低优先级（{_format_weight(self.config.low_priority_weight)}）"
            )

        if score >= self.config.important_threshold:
            category = Category.IMPORTANT
        elif score <= self.config.fyi_threshold:
            category = Category.FYI
        else:
            category = Category.NORMAL

        if raw.is_self and category is Category.IMPORTANT:
            category = Category.NORMAL
            reasons.append("己方消息最高只标记为「普通」，不当作需要我处理的重要消息")

        if not reasons:
            reasons.append("未命中任何分类规则，默认普通")
        return CategoryDecision(
            category=category,
            reasons=tuple(reasons[: self.config.max_reasons]),
            score=round(score, 2),
        )

    @staticmethod
    def _explain(rule: SignalRule, keyword: str) -> str:
        shown = keyword.strip()
        if len(shown) > 12:
            shown = shown[:12] + "…"
        return f"{rule.reason}（{shown}，{_format_weight(rule.weight)}）"


# ---------------------------------------------------------------------------
# 中文时间解析
# ---------------------------------------------------------------------------

_CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_NUM = r"(?:\d{1,3}|[零一二两三四五六七八九十]{1,3})"

_REL_DAY = re.compile(r"(?P<word>大后天|后天|明天|明日|明早|明晚|今天|今日|今晚|今早)")
_REL_DAY_META: dict[str, tuple[int, str | None]] = {
    "大后天": (3, None),
    "后天": (2, None),
    "明天": (1, None),
    "明日": (1, None),
    "明早": (1, "早上"),
    "明晚": (1, "晚上"),
    "今天": (0, None),
    "今日": (0, None),
    "今晚": (0, "晚上"),
    "今早": (0, "早上"),
}

_WEEKDAY = re.compile(r"(?P<prefix>本|这|下下|下)?(?P<unit>周|星期|礼拜)(?P<day>[一二三四五六日天])")
_WEEKDAY_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

_ABS_DATE = re.compile(r"(?:(?P<year>\d{4})\s*年)?(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]")
_DAY_AFTER = re.compile(rf"(?P<n>{_NUM})\s*天\s*(?:之?后|以后)")
_HALF_HOUR_AFTER = re.compile(r"半\s*个?\s*小时\s*(?:之?后|以后)")
_HOUR_AFTER = re.compile(rf"(?P<n>{_NUM})\s*个?\s*(?:小时|钟头)\s*(?:之?后|以后)")
_MIN_AFTER = re.compile(rf"(?P<n>{_NUM})\s*分钟\s*(?:之?后|以后)")

_CLOCK = re.compile(
    r"(?:"
    r"(?P<h1>\d{1,2})\s*[:：]\s*(?P<m1>\d{2})(?:\s*[:：]\s*\d{2})?"
    r"|"
    r"(?P<h2>\d{1,2})\s*点\s*(?:(?P<half>半)|(?P<m2>\d{1,2})\s*分?)?"
    r")"
)
_PERIOD = re.compile(r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜间)")
_PERIOD_DEFAULT_HOUR: dict[str, int] = {
    "凌晨": 1,
    "早上": 8,
    "早晨": 8,
    "上午": 9,
    "中午": 12,
    "下午": 14,
    "傍晚": 18,
    "晚上": 20,
    "夜里": 22,
    "夜间": 22,
}

_DEADLINE = re.compile(
    r"(?:截止|ddl|deadline|之前|以前|(?<=[0-9点号日天周月晚早午底末秒班一二三四五六])前)",
    re.IGNORECASE,
)
_URGENT = re.compile(r"(?:尽快|尽早|马上|立刻|立即|赶紧|抓紧|急用|加急|速回|速办)")

# 待办动作信号：与时间/截止/紧急信号组合后才生成待办
_STRONG_ACTION = re.compile(
    r"(?:截止|ddl|deadline|提交|交一下|交材料|交作业|交报告|交房租|发我|发给我|回复|答复"
    r"|回我|回个|确认|安排|准备|参加|报名|开会|会议|面试|考试|答辩|汇报|出差|联系|处理|完成"
    r"|做一下|看一下|检查|修改|审核|签字|盖章|付款|转账|缴费|还钱|帮忙|帮我|需要你|请你"
    r"|务必|必须|尽快|马上|抓紧|催|打电话|回电话|有会|开会|会议|例会|晨会|聚餐|聚会|活动|吃饭"
    r"|见面|碰头|集合|出发|签到|看电影|观影|提醒我|叫我|喊我)",
    re.IGNORECASE,
)
_REMINDER = re.compile(r"(?:记得|别忘记|别忘了|提醒|叫我|喊我)")
_CORE_ACTION = re.compile(
    r"(?:带|买|拿|取|交|发|回|写|填|做|准备|参加|提交|确认|回复|安排|检查|修改|审核"
    r"|处理|完成|联系|打电话|回电话|还|付|转|缴|报|定|订|预约|开会|会议|面试|考试"
    r"|答辩|汇报|出差|体检|聚餐|聚会)"
)
_QUESTION_TAIL = re.compile(r"(?:吗|呢|么)\s*$|[？?]\s*$")
_OBLIGATION = re.compile(r"(?:务必|必须|一定要|请你|请您|记得|截止|ddl|deadline)", re.IGNORECASE)

_CLAUSE_SPLIT = re.compile(r"[。！？!?；;\n\r]+")
_LIST_PREFIX = re.compile(r"^[\s\-*•·]+|^\s*\d+[.、)）]\s*")
_WHITESPACE = re.compile(r"\s+")


def _parse_number(raw: str) -> int | None:
    """解析阿拉伯数字或简单中文数字（一~九十九）。"""
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if "十" in raw:
        left, _, right = raw.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones
    if len(raw) == 1 and raw in _CN_DIGITS:
        return _CN_DIGITS[raw]
    return None


def _apply_period(hour: int, period: str | None) -> int:
    """把“下午 3 点”这类 12 小时制时间换算成 24 小时制。"""
    if period in ("下午", "傍晚", "晚上", "夜里", "夜间") and hour < 12:
        return hour + 12
    if period == "中午" and hour in (1, 2):
        return hour + 12
    if period in ("凌晨", "早上", "早晨", "上午") and hour == 12:
        return 0
    return hour


def _resolve_clock(clock_match: re.Match[str] | None, period: str | None) -> tuple[int, int] | None:
    """解析“3点半/15:30/晚上8点”为 (hour, minute)。"""
    if clock_match is None:
        if period is None:
            return None
        return _PERIOD_DEFAULT_HOUR.get(period, 9), 0

    if clock_match.group("h1") is not None:
        hour = int(clock_match.group("h1"))
        minute = int(clock_match.group("m1"))
    else:
        hour = int(clock_match.group("h2"))
        if clock_match.group("half"):
            minute = 30
        elif clock_match.group("m2"):
            minute = int(clock_match.group("m2"))
        else:
            minute = 0

    hour = _apply_period(hour, period)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _resolve_date_match(match: re.Match[str], now: datetime) -> date | None:
    """把日期类匹配（相对日/星期/月日）解析成具体日期。"""
    if match.re is _REL_DAY:
        days, _period = _REL_DAY_META[match.group("word")]
        return now.date() + timedelta(days=days)

    if match.re is _ABS_DATE:
        year_text = match.group("year")
        month = int(match.group("month"))
        day = int(match.group("day"))
        year = int(year_text) if year_text else now.year
        try:
            resolved = date(year, month, day)
        except ValueError:
            return None
        if year_text is None and resolved < now.date():
            # 未写年份且已经过去：按“下一次”处理（例如 12 月说“1 月 5 日”）
            try:
                resolved = resolved.replace(year=year + 1)
            except ValueError:
                return None
        return resolved

    if match.re is _WEEKDAY:
        target = _WEEKDAY_NUM[match.group("day")]
        today = now.date()
        monday = today - timedelta(days=today.isoweekday() - 1)
        prefix = match.group("prefix") or ""
        if prefix in ("本", "这"):
            return monday + timedelta(days=target - 1)
        if prefix == "下":
            return monday + timedelta(days=7 + target - 1)
        if prefix == "下下":
            return monday + timedelta(days=14 + target - 1)
        # 不写“本周/下周”时按“下一次该星期几”处理（今天就是该星期几则为今天）
        return today + timedelta(days=(target - today.isoweekday()) % 7)

    return None


def _forced_period(match: re.Match[str]) -> str | None:
    """“今晚/明早”这类相对日自己带时段。"""
    if match.re is _REL_DAY:
        return _REL_DAY_META[match.group("word")][1]
    return None


def _deadline_display_text(sentence: str, match: re.Match[str]) -> str:
    """把单独的“前/之前/截止”扩展成可读的原文片段（如“月底前”）。"""
    text = match.group(0)
    if text.strip() != "前":
        return text
    prefix = re.search(r"([0-9一二三四五六七八九十点号日天周月晚早午底末秒]{1,4})前$", sentence[: match.end()])
    if prefix is None:
        return text
    return prefix.group(0)


def _extend_signal_text(sentence: str, start: int, end: int) -> str:
    """把起止位置扩展为包含“截止/前/之前”的完整原文片段。"""
    prefix = re.search(r"(?:截止|ddl|deadline)\s*$", sentence[:start], re.IGNORECASE)
    if prefix is not None:
        start = prefix.start()
    suffix = re.match(
        r"(?:之前|以前|前|截止|ddl|deadline)", sentence[end:], re.IGNORECASE
    )
    if suffix is not None:
        end += suffix.end()
    return sentence[start:end].strip()


@dataclass(frozen=True)
class _DueHit:
    """内部解析结果（带信号标记，用于置信度与文档说明）。"""

    due_at: datetime | None
    due_text: str | None
    has_date: bool = False
    has_clock: bool = False
    deadline: bool = False
    urgent: bool = False

    @property
    def has_signal(self) -> bool:
        return self.due_at is not None or self.deadline or self.urgent


@dataclass(frozen=True)
class ParsedDue:
    """``parse_due_text`` 的返回值。"""

    due_at: datetime | None
    due_text: str
    has_date: bool = False
    has_clock: bool = False
    is_deadline: bool = False
    is_urgent: bool = False


@dataclass(frozen=True)
class TodoConfig:
    """待办提取配置。"""

    date_only_hour: int = 18  # 只有日期没有时刻时，默认截止到当天 18:00（下班前）
    base_confidence: float = 0.55
    max_todos: int = 3
    max_todo_text_chars: int = 100


def _resolve_due(sentence: str, now: datetime, config: TodoConfig) -> _DueHit:
    """解析一个句子中最可能的一个截止时间/时效信号。"""
    deadline_match = _DEADLINE.search(sentence)
    urgent_match = _URGENT.search(sentence)
    deadline = deadline_match is not None
    urgent = urgent_match is not None

    # 1) 相对小时/分钟（“2 小时后”“半小时后”“30 分钟后”）
    match = _HALF_HOUR_AFTER.search(sentence)
    if match is not None:
        return _DueHit(
            due_at=now + timedelta(minutes=30),
            due_text=_extend_signal_text(sentence, match.start(), match.end()),
            has_clock=True,
            deadline=deadline,
            urgent=urgent,
        )
    match = _MIN_AFTER.search(sentence)
    if match is not None:
        minutes = _parse_number(match.group("n"))
        if minutes is not None:
            return _DueHit(
                due_at=now + timedelta(minutes=minutes),
                due_text=_extend_signal_text(sentence, match.start(), match.end()),
                has_clock=True,
                deadline=deadline,
                urgent=urgent,
            )
    match = _HOUR_AFTER.search(sentence)
    if match is not None:
        hours = _parse_number(match.group("n"))
        if hours is not None:
            return _DueHit(
                due_at=now + timedelta(hours=hours),
                due_text=_extend_signal_text(sentence, match.start(), match.end()),
                has_clock=True,
                deadline=deadline,
                urgent=urgent,
            )

    # 2) 日期部分
    date_match: re.Match[str] | None = None
    base_date: date | None = None
    period: str | None = None

    day_after = _DAY_AFTER.search(sentence)
    if day_after is not None:
        days = _parse_number(day_after.group("n"))
        if days is not None:
            date_match = day_after
            base_date = now.date() + timedelta(days=days)
    else:
        candidates = [
            m
            for m in (_REL_DAY.search(sentence), _ABS_DATE.search(sentence), _WEEKDAY.search(sentence))
            if m is not None
        ]
        if candidates:
            date_match = min(candidates, key=lambda m: m.start())
            base_date = _resolve_date_match(date_match, now)
            if base_date is None:
                date_match = None
            else:
                period = _forced_period(date_match)

    # 3) 时刻部分（时段 + 钟点）
    clock_match = _CLOCK.search(sentence)
    period_match = _PERIOD.search(sentence)
    if period is None and period_match is not None:
        period = period_match.group("period")
    clock = _resolve_clock(clock_match, period)

    # 4) 组合
    if base_date is not None:
        hour, minute = clock if clock is not None else (config.date_only_hour, 0)
        due_at = datetime.combine(base_date, time(hour, minute), tzinfo=now.tzinfo)
        has_date = True
        has_clock = clock is not None
    elif clock is not None:
        due_at = datetime.combine(now.date(), time(clock[0], clock[1]), tzinfo=now.tzinfo)
        if due_at <= now:
            due_at += timedelta(days=1)  # 只说“3 点”且已过点：按明天算
        has_date = False
        has_clock = True
    else:
        due_at = None
        has_date = False
        has_clock = False

    # 5) 可读的原文时间片段
    due_text: str | None = None
    if due_at is not None:
        spans = [m for m in (date_match, clock_match) if m is not None]
        if spans:
            start = min(m.start() for m in spans)
            end = max(m.end() for m in spans)
            if deadline_match is not None and start <= deadline_match.start() <= end + 4:
                end = max(end, deadline_match.end())
            due_text = _extend_signal_text(sentence, start, end)
    if due_text is None:
        if deadline_match is not None:
            due_text = _deadline_display_text(sentence, deadline_match)
        elif urgent_match is not None:
            due_text = urgent_match.group(0)

    return _DueHit(
        due_at=due_at,
        due_text=due_text,
        has_date=has_date,
        has_clock=has_clock,
        deadline=deadline,
        urgent=urgent,
    )


def parse_due_text(text: str, *, now: datetime | None = None) -> ParsedDue | None:
    """解析一段中文文本中最可能的一个时间表达；没有时间信号时返回 ``None``。"""
    moment = _ensure_aware(now) if now is not None else datetime.now().astimezone()
    hit = _resolve_due(text.strip(), moment, TodoConfig())
    if not hit.has_signal:
        return None
    return ParsedDue(
        due_at=hit.due_at,
        due_text=hit.due_text or "",
        has_date=hit.has_date,
        has_clock=hit.has_clock,
        is_deadline=hit.deadline,
        is_urgent=hit.urgent,
    )


# ---------------------------------------------------------------------------
# 待办提取
# ---------------------------------------------------------------------------

class RuleTodoExtractor:
    """纯本地中文规则待办提取器（实现 ``contracts.TodoExtractor``）。

    判定思路：句子必须同时满足「行动/提醒信号」和「时间/截止/紧急信号」才生成待办；
    己方消息永远不生成待办。``now`` 注入方式见模块 docstring。
    """

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
        config: TodoConfig | None = None,
    ) -> None:
        self._now_provider = now_provider
        self.config = config or TodoConfig()

    # -- 实现 contracts.TodoExtractor ----------------------------------
    def extract(self, raw: RawMessage, *, now: datetime | None = None) -> list[ExtractedTodo]:
        if raw.is_self:
            return []  # 己方消息不生成待办
        text = (raw.text or "").strip()
        if not text:
            return []

        moment = self._resolve_now(now)
        todos: list[ExtractedTodo] = []
        seen: set[str] = set()
        for sentence in self._split_sentences(text):
            cleaned = _WHITESPACE.sub(" ", _LIST_PREFIX.sub("", sentence)).strip()
            if len(cleaned) < 3:
                continue
            hit = _resolve_due(cleaned, moment, self.config)
            if not self._has_task_signal(cleaned, hit):
                continue
            if self._is_open_question(cleaned, hit):
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            todos.append(
                ExtractedTodo(
                    text=self._todo_text(cleaned),
                    due_at=hit.due_at,
                    due_text=hit.due_text,
                    confidence=self._confidence(hit),
                )
            )
            if len(todos) >= self.config.max_todos:
                break
        return todos

    # -- 内部工具 -------------------------------------------------------
    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return _ensure_aware(now)
        if self._now_provider is not None:
            return _ensure_aware(self._now_provider())
        return datetime.now().astimezone()

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        return [part for part in _CLAUSE_SPLIT.split(text) if part.strip()]

    @staticmethod
    def _has_task_signal(sentence: str, hit: _DueHit) -> bool:
        reminder = _REMINDER.search(sentence)
        if reminder is not None and _CORE_ACTION.search(sentence[reminder.end():]):
            return True  # 提醒句：「记得带伞」「别忘了交作业」
        if _STRONG_ACTION.search(sentence) and hit.has_signal:
            return True  # 动作句 + 时间/截止/紧急信号
        return False

    @staticmethod
    def _is_open_question(sentence: str, hit: _DueHit) -> bool:
        """问句（“明天有空吗”“明天能来吗”）通常是征求意愿，不当作待办。

        例外：带明确截止/义务词（“请务必…吗”）或带“截止/DDL”的句子仍会提取。
        """
        if hit.deadline or _OBLIGATION.search(sentence):
            return False
        return _QUESTION_TAIL.search(sentence) is not None

    def _confidence(self, hit: _DueHit) -> float:
        confidence = self.config.base_confidence
        if hit.has_date:
            confidence += 0.15
        if hit.has_clock:
            confidence += 0.10
        if hit.deadline:
            confidence += 0.10
        if hit.urgent:
            confidence += 0.05
        return round(min(confidence, 0.95), 2)

    def _todo_text(self, sentence: str) -> str:
        limit = self.config.max_todo_text_chars
        if len(sentence) <= limit:
            return sentence
        return sentence[: limit - 1] + "…"
