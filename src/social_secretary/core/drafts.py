"""回复草稿实现。

- :class:`TemplateDraftProvider`：**本地模板**草稿。intel 的 ``LocalDraftProvider`` 未就绪
  时的安全兜底；永不联网、永不发送。
- :class:`ExternalDraftAdapter`：把 intel 的 ``ExternalLLMClient``（接口尚未定稿）适配成
  ``contracts.DraftProvider``，并强制标记 ``kind=EXTERNAL_LLM`` /
  ``requires_external_consent=True``。**只有 service 层确认用户已授权 EXTERNAL_AI 后才会
  创建本适配器**；这里不负责门控。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from ..contracts import DraftContext, DraftKind, Message, ReplyDraft
from .storage import now_local

_DEFAULT_STYLE = "简洁"


class TemplateDraftProvider:
    """本地规则模板草稿（零上传、零网络）。"""

    kind = DraftKind.LOCAL_RULES

    def __init__(self, *, model_label: str = "本地模板") -> None:
        self.model_label = model_label

    def propose(self, message: Message, context: DraftContext) -> list[ReplyDraft]:
        style = (context.style or _DEFAULT_STYLE).strip() or _DEFAULT_STYLE
        text = self._compose(message, style)
        return [
            ReplyDraft(
                id=uuid.uuid4().hex,
                message_id=message.id,
                kind=DraftKind.LOCAL_RULES,
                style=style,
                text=text,
                created_at=now_local(),
                model_label=self.model_label,
                requires_external_consent=False,
            )
        ]

    @staticmethod
    def _compose(message: Message, style: str) -> str:
        text = message.text or ""
        topics: list[str] = []
        if any(keyword in text for keyword in ("会议", "评审", "开会", "日程")):
            topics.append("会议安排")
        if any(keyword in text for keyword in ("?", "？", "请问", "确认", "回复", "答复")):
            topics.append("需要确认的事项")
        if any(keyword in text for keyword in ("截止", "今天", "明天", "之前", "尽快")):
            topics.append("时间节点")
        if not topics:
            topics.append("你提到的事情")
        topic = "、".join(topics)

        templates = {
            "简洁": f"收到，关于{topic}我稍后回复你。",
            "礼貌": f"您好，已收到关于{topic}的消息，我会尽快处理并回复您，谢谢。",
            "详细": (
                f"您好，已收到您关于{topic}的消息。"
                "我会先核对相关信息，稍后给您完整回复；如有紧急情况请直接告知。"
            ),
            "感谢": f"谢谢提醒，关于{topic}我知道了，稍后回复你。",
        }
        return templates.get(style, templates[_DEFAULT_STYLE])


class ExternalDraftAdapter:
    """把外部 AI 客户端（intel 提供）适配为 DraftProvider。

    兼容多种潜在接口：``propose(message, context)`` / ``generate_drafts(...)`` /
    ``generate(...)``，返回值支持 ``ReplyDraft``、``str`` 或 ``{"text": ...}``。
    """

    kind = DraftKind.EXTERNAL_LLM

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def model_label(self) -> str:
        for attribute in ("model_label", "model", "model_name"):
            value = getattr(self._client, attribute, "")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "external-llm"

    def propose(self, message: Message, context: DraftContext) -> list[ReplyDraft]:
        result: Any = None
        for method_name in ("propose", "generate_drafts", "generate"):
            method = getattr(self._client, method_name, None)
            if callable(method):
                result = method(message, context)
                break
        if result is None:
            raise RuntimeError("外部 AI 客户端接口不兼容（缺少 propose/generate_drafts）")
        if hasattr(result, "__await__"):
            raise RuntimeError("外部 AI 客户端返回协程，核心层只支持同步接口")

        drafts: list[ReplyDraft] = []
        for item in list(result):
            drafts.append(self._to_reply_draft(item, message, context))
        return drafts

    def test_connection(self) -> Any:
        checker = getattr(self._client, "test_connection", None)
        if callable(checker):
            return checker()
        return True

    def _to_reply_draft(self, item: Any, message: Message, context: DraftContext) -> ReplyDraft:
        style = (context.style or _DEFAULT_STYLE).strip() or _DEFAULT_STYLE
        if isinstance(item, ReplyDraft):
            return replace(
                item,
                id=item.id or uuid.uuid4().hex,
                message_id=message.id,
                kind=DraftKind.EXTERNAL_LLM,
                style=item.style or style,
                created_at=item.created_at or now_local(),
                model_label=item.model_label or self.model_label,
                requires_external_consent=True,
            )
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("content") or "")
        else:
            text = str(item)
        text = text.strip()
        if not text:
            raise RuntimeError("外部 AI 返回了空草稿")
        return ReplyDraft(
            id=uuid.uuid4().hex,
            message_id=message.id,
            kind=DraftKind.EXTERNAL_LLM,
            style=style,
            text=text,
            created_at=now_local(),
            model_label=self.model_label,
            requires_external_consent=True,
        )


__all__ = ["ExternalDraftAdapter", "TemplateDraftProvider"]
