"""本地规则回复草稿（默认实现，零网络、零上传）。

设计原则
--------
1. 完全本地：只依赖标准库与 :mod:`social_secretary.contracts`，不 import 任何网络库。
2. 不把对方消息原文拼进草稿，只按“意图 + 会话名/称呼 + 风格”套用中文模板，
   避免把私密内容意外复制到剪贴板或截图里。
3. 每条草稿都是 2~3 个候选，用户可自行编辑；本模块不提供任何发送能力。

对外接口
--------
- ``LocalDraftProvider``：无参可构造，``kind = DraftKind.LOCAL_RULES``，
  实现 :class:`social_secretary.contracts.DraftProvider`。
- ``draft_styles()``：返回全部可用风格（简体中文）。
- ``detect_intent(text)``：意图识别（询问/请求/邀约/通知/催促/感谢/问候/其他），
  主要供测试与文档使用。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Callable

from ..contracts import DraftContext, DraftKind, Message, ReplyDraft

__all__ = [
    "DRAFT_STYLES",
    "draft_styles",
    "detect_intent",
    "LocalDraftProvider",
]

DRAFT_STYLES: tuple[str, ...] = ("简洁", "礼貌", "稍后回复", "确认收到")
DEFAULT_STYLE = "简洁"

_NAME_LIMIT = 16
_TITLE_LIMIT = 24


def draft_styles() -> tuple[str, ...]:
    """返回本地草稿支持的风格（简体中文元组，顺序固定）。"""
    return DRAFT_STYLES


# ---------------------------------------------------------------------------
# 意图识别（按顺序匹配，先命中者优先）
# ---------------------------------------------------------------------------

_INTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("thanks", re.compile(r"^(?:谢谢|感谢|多谢|辛苦了|辛苦啦|谢啦)")),
    ("urge", re.compile(r"催|尽快|赶紧|抓紧|急|马上|立刻|截止|ddl|deadline", re.IGNORECASE)),
    ("invite", re.compile(r"一起|要不要|约|聚餐|聚会|吃饭|看电影|周末|来不来|有空吗|参加吗")),
    ("request", re.compile(r"请|麻烦|帮忙|帮我|需要你|务必|提交|发我|回复|确认|安排|处理")),
    ("notice", re.compile(r"通知|公告|提醒|会议|开会|改期|变更|注意|系统|维护")),
    ("greeting", re.compile(r"^(?:在吗|在不在|你好|您好|哈喽|嗨|早上好|晚上好|晚安|早安)")),
    ("question", re.compile(r"[？?]|吗|呢|怎么|什么|如何|是否|能不能|几点|哪")),
)


def detect_intent(text: str) -> str:
    """识别消息意图，返回 ``question``/``request``/``invite``/``notice``/``urge``/``thanks``/``greeting``/``other``。"""
    normalized = (text or "").strip()
    if not normalized:
        return "other"
    for intent, pattern in _INTENT_RULES:
        if pattern.search(normalized):
            return intent
    return "other"


# ---------------------------------------------------------------------------
# 中文模板：风格 -> 意图 -> 候选草稿（2~3 条）
# {name} 为对方称呼，{title} 为会话名；模板不含消息正文。
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "简洁": {
        "question": (
            "收到，我确认一下，稍后回复你。",
            "这个我看看，晚点答复你。",
            "明白，我核对一下再告诉你。",
        ),
        "request": (
            "好的，我来处理，完成后告诉你。",
            "收到，我马上安排。",
            "没问题，我跟进一下。",
        ),
        "invite": (
            "好呀，我先看下时间，晚点回复你。",
            "可以，我确认下安排再联系你。",
            "收到，我看看能不能参加。",
        ),
        "notice": (
            "收到，关于「{title}」的通知我看到了，谢谢。",
            "明白了，我会留意。",
            "好的，我知道了。",
        ),
        "urge": (
            "收到，我尽快处理。",
            "好的，我马上跟进。",
            "明白，我抓紧办。",
        ),
        "thanks": (
            "不客气～",
            "客气啦，有事随时说。",
            "小事，别客气。",
        ),
        "greeting": (
            "在的，你说。",
            "在呢，有什么事吗？",
            "你好，我在。",
        ),
        "other": (
            "收到，我看一下。",
            "好的，明白了。",
            "嗯嗯，收到。",
        ),
    },
    "礼貌": {
        "question": (
            "{name}，你问的这件事我确认一下，稍后详细回复你。",
            "好的{name}，我核实之后尽快答复你。",
            "收到{name}，我先查一下，晚点告诉你结果。",
        ),
        "request": (
            "{name}，收到，我这边会尽快处理，完成后向您反馈。",
            "好的{name}，我马上安排，有进展第一时间告诉你。",
            "明白{name}，这件事我记下了，会按时跟进。",
        ),
        "invite": (
            "{name}，谢谢你想到我，我看一下时间安排，稍后回复你。",
            "好的{name}，我确认一下日程，尽快给你答复。",
            "收到邀请，{name}，我看看能否参加，晚点告诉你。",
        ),
        "notice": (
            "{name}，收到，谢谢你的通知，我这边会留意。",
            "好的{name}，信息我记下了，有疑问再向你请教。",
            "明白了，{name}，感谢告知。",
        ),
        "urge": (
            "{name}，收到，我这边会尽快处理，处理好后跟您同步。",
            "好的{name}，我马上跟进，尽快给你答复。",
            "明白{name}，我抓紧时间办，有进展马上告诉你。",
        ),
        "thanks": (
            "不客气{name}，有需要随时找我。",
            "应该的{name}，不用客气。",
            "很高兴能帮上忙，{name}。",
        ),
        "greeting": (
            "你好呀{name}，我在的，请说。",
            "{name}你好，我在，有什么需要帮忙的吗？",
            "在的{name}，有什么事你说。",
        ),
        "other": (
            "{name}，收到，我这边看一下，稍后回复你。",
            "好的{name}，我明白了，有需要再联系。",
            "收到{name}，谢谢你的消息。",
        ),
    },
    "稍后回复": {
        "question": (
            "收到，我稍后详细回复你。",
            "我现在不太方便细说，晚点答复你。",
        ),
        "request": (
            "收到，我稍后处理，完成后回复你。",
            "好的，我忙完手上这件事就来看，稍后给你答复。",
        ),
        "invite": (
            "收到，我先看下安排，晚点回复你能不能参加。",
            "谢谢你邀请，我确认一下时间再答复你。",
        ),
        "notice": (
            "收到，我先忙，稍后仔细看一下。",
            "好的，我晚点再确认细节。",
        ),
        "urge": (
            "收到，我现在有点忙，晚点集中处理，会尽快回复你。",
            "明白，我稍后处理，处理完告诉你。",
        ),
        "thanks": (
            "收到，谢谢！我先忙完手头的事，稍后再跟你细聊。",
            "不客气～我晚点再跟你说。",
        ),
        "greeting": (
            "在忙，稍后回你。",
            "我现在有点事，晚点联系你。",
        ),
        "other": (
            "收到，稍后回复你。",
            "我晚点再跟你说。",
        ),
    },
    "确认收到": {
        "question": (
            "收到，我确认后回复。",
            "收到，稍后答复。",
        ),
        "request": (
            "收到，我处理一下。",
            "收到，已记下，稍后完成。",
        ),
        "invite": (
            "收到，我会确认后回复你。",
            "收到，先记下这个安排。",
        ),
        "notice": (
            "收到，已了解。",
            "收到，明白。",
        ),
        "urge": (
            "收到，会尽快处理。",
            "收到，已记下，马上跟进。",
        ),
        "thanks": (
            "收到，不客气！",
            "收到，谢谢～",
        ),
        "greeting": (
            "收到，我在。",
            "收到，请讲。",
        ),
        "other": (
            "收到。",
            "收到，明白了。",
        ),
    },
}


def _normalize_style(style: str | None) -> str:
    candidate = (style or "").strip()
    return candidate if candidate in _TEMPLATES else DEFAULT_STYLE


def _clean_label(value: str, *, limit: int) -> str:
    text = re.sub(r"[\s@]+", "", value or "")
    return text[:limit]


def _display_name(message: Message, context: DraftContext) -> str:
    name = _clean_label(message.sender, limit=_NAME_LIMIT)
    if not name:
        name = _clean_label(context.conversation_title, limit=_NAME_LIMIT)
    return name or "你"


def _display_title(message: Message, context: DraftContext) -> str:
    title = (context.conversation_title or "").strip().replace("\n", " ")
    if not title:
        title = (message.conversation_title or "").strip().replace("\n", " ")
    return title[:_TITLE_LIMIT] or "当前会话"


class LocalDraftProvider:
    """本地规则草稿生成器（实现 ``contracts.DraftProvider``，kind=LOCAL_RULES）。

    用法::

        provider = LocalDraftProvider()          # 无参可构造
        drafts = provider.propose(message, DraftContext(conversation_title="项目群", style="礼貌"))

    ``context.user_note`` 目前只作为保留字段（预留给手动编辑/外部 AI），本地模板不直接引用，
    以免把用户备注泄漏进候选文本。
    """

    kind = DraftKind.LOCAL_RULES
    model_label = "本地规则"

    def __init__(self, *, now_provider: Callable[[], datetime] | None = None) -> None:
        self._now_provider = now_provider

    def _now(self) -> datetime:
        if self._now_provider is not None:
            moment = self._now_provider()
            return moment if moment.tzinfo is not None else moment.astimezone()
        return datetime.now().astimezone()

    # -- 实现 contracts.DraftProvider ----------------------------------
    def propose(self, message: Message, context: DraftContext) -> list[ReplyDraft]:
        style = _normalize_style(context.style)
        intent = detect_intent(message.text)
        variants = _TEMPLATES[style].get(intent) or _TEMPLATES[style]["other"]
        name = _display_name(message, context)
        title = _display_title(message, context)
        created_at = self._now()
        drafts: list[ReplyDraft] = []
        for template in variants:
            text = template.format(name=name, title=title).strip()
            if not text:
                continue
            drafts.append(
                ReplyDraft(
                    id=uuid.uuid4().hex,
                    message_id=message.id,
                    kind=DraftKind.LOCAL_RULES,
                    style=style,
                    text=text,
                    created_at=created_at,
                    model_label=self.model_label,
                    requires_external_consent=False,
                )
            )
        return drafts
