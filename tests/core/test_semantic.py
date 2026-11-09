"""P2 B/C：语义分析（会话/每日简报/草稿）测试。

全部离线：本地模式走核心层最小规则回退；外部模式只使用注入的 fake provider.complete，
不发起任何真实 HTTP 请求。验证门控、降级回退、审计脱敏与契约结构。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from social_secretary.contracts import (
    Category,
    ConsentScope,
    MessageWorkStatus,
)
from social_secretary.core.service import NotFoundError, ServiceError
from social_secretary.core.storage import now_local
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import FakeExternalProvider, make_message, make_service

API_KEY = "sk-testplaceholder1234567890"

TEXT_REQUEST = "今天下午3点前提交周报（私密标记SEM9Q7）"
TEXT_QUESTION = "这个方案可行吗？"
TEXT_PROMISE = "我会稍后把资料发你"
TEXT_FYI = "周末聚餐通知"


def seed_messages(service, *, extra: int = 0):
    account = service.add_mail_account("semantic@example.com", "语义邮箱")
    conversation = service.storage.list_conversations(account.id)[0]
    base = now_local()
    specs = [
        (TEXT_REQUEST, Category.IMPORTANT, 0),
        (TEXT_QUESTION, Category.NORMAL, 1),
        (TEXT_PROMISE, Category.NORMAL, 2),
        (TEXT_FYI, Category.FYI, 3),
    ]
    messages = []
    for text, category, minute in specs:
        message = make_message(
            conversation,
            text,
            timestamp=base + timedelta(minutes=minute),
            category=category,
        )
        service.storage.insert_message(message)
        messages.append(message)
    for index in range(extra):
        message = make_message(
            conversation,
            f"补充消息{index}",
            timestamp=base + timedelta(minutes=10 + index),
        )
        service.storage.insert_message(message)
        messages.append(message)
    return account, conversation, messages


def local_service():
    service = make_service(credential_store=InMemoryCredentialStore())
    account, conversation, messages = seed_messages(service)
    return service, account, conversation, messages


class FakeCompleteProvider:
    """注入 external_provider_factory 的 fake：只实现 complete，不联网。"""

    def __init__(self, response=None, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail
        self.calls = 0
        self.last_prompt = ""
        self.last_usage: dict[str, int | None] | None = None

    def complete(self, system_prompt: str = "", user_prompt: str = "", **_kwargs):
        self.calls += 1
        self.last_prompt = f"{system_prompt}\n{user_prompt}"
        if self.fail:
            raise RuntimeError("fake complete failed")
        return self.response


def external_context(provider: FakeCompleteProvider, *, consent=True, key=True):
    factory_calls: list[int] = []

    def factory():
        factory_calls.append(1)
        return provider

    service = make_service(
        credential_store=InMemoryCredentialStore(),
        external_provider_factory=factory,
    )
    _account, conversation, messages = seed_messages(service)
    if key:
        service.set_external_ai_key(API_KEY)
    if consent:
        service.grant_consent(ConsentScope.EXTERNAL_AI, confirmation_text="测试授权")
    return service, factory_calls, conversation, messages


def audit_text(service) -> str:
    return json.dumps(service.storage.list_audit(), ensure_ascii=False)


def semantic_audit_entries(service, action: str) -> list[dict]:
    return [
        entry
        for entry in service.storage.list_audit()
        if entry.get("action") == action
    ]


# ---------------------------------------------------------------------------
# 本地模式
# ---------------------------------------------------------------------------

def test_local_conversation_semantic_structure_and_sources() -> None:
    service, _account, conversation, messages = local_service()
    try:
        result = service.generate_conversation_semantic(conversation.id)
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["error"] is None
        assert result["conversation_title"] == conversation.title
        assert result["overview"]
        assert result["items"], result
        valid_ids = {
            message.id
            for message in service.storage.list_messages(
                conversation_id=conversation.id, limit=20
            )
        }
        for item in result["items"]:
            assert item["source_message_ids"]
            assert set(item["source_message_ids"]) <= valid_ids
            assert isinstance(item["uncertain"], bool)
        for key in (
            "questions",
            "requests",
            "deadlines",
            "promises",
            "unresolved",
            "chitchat",
        ):
            assert isinstance(result[key], list), key
        assert result["requests"] or result["questions"] or result["deadlines"]
        assert any(item.get("due_hint") for item in result["items"])
        assert isinstance(result["uncertainty_notes"], list)
        assert set(result["usage"]) == {
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost",
        }
        # 审计只记 provider/count，不含正文/标题
        entries = semantic_audit_entries(service, "semantic_conversation_generated")
        assert entries and entries[-1]["detail"]["provider"] == "local_rules"
        assert set(entries[-1]["detail"]) == {"provider", "count"}
        assert "SEM9Q7" not in audit_text(service)
        assert conversation.title not in audit_text(service)
    finally:
        service.shutdown()


def test_local_conversation_uses_only_latest_20_messages() -> None:
    service = make_service(credential_store=InMemoryCredentialStore())
    try:
        _account, conversation, messages = seed_messages(service, extra=25)
        oldest = messages[0].id
        result = service.generate_conversation_semantic(conversation.id)
        allowed = {
            message.id
            for message in service.storage.list_messages(
                conversation_id=conversation.id, limit=20
            )
        }
        referenced = {
            source_id
            for item in result["items"]
            for source_id in item["source_message_ids"]
        }
        assert referenced
        assert referenced <= allowed
        assert oldest not in referenced
    finally:
        service.shutdown()


def test_local_daily_brief_sections_and_citations() -> None:
    service, _account, _conversation, messages = local_service()
    try:
        result = service.generate_daily_brief()
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["error"] is None
        assert result["day"] == now_local().date().isoformat()
        assert set(result["sections"]) == {"must_today", "soon", "batch_reply", "fyi"}
        assert result["sections"]["must_today"], "重要消息应进 must_today"
        assert result["sections"]["must_today"][0]["source_message_ids"] == [
            messages[0].id
        ]
        assert result["sections"]["fyi"], "FYI 消息应进 fyi"
        valid_ids = {message.id for message in messages}
        assert set(result["citations"]) <= valid_ids
        assert result["caveats"]
        assert "SEM9Q7" not in audit_text(service)
        assert set(result["usage"]) == {
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost",
        }
    finally:
        service.shutdown()


def test_local_daily_brief_empty_day_is_safe() -> None:
    service, _account, _conversation, _messages = local_service()
    try:
        result = service.generate_daily_brief(day=now_local().date() - timedelta(days=365))
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["sections"] == {
            "must_today": [],
            "soon": [],
            "batch_reply": [],
            "fyi": [],
        }
        assert result["citations"] == []
        assert result["caveats"]
    finally:
        service.shutdown()


def test_local_semantic_draft_three_styles() -> None:
    service, _account, conversation, messages = local_service()
    try:
        target = messages[0]
        texts = []
        for style in ("友好", "正式", "简洁"):
            result = service.generate_semantic_draft(target.id, style=style)
            assert result["ok"] is True
            assert result["provider"] == "local_rules"
            assert result["style"] == style
            assert result["message_id"] == target.id
            assert result["text"]
            assert result["error"] is None
            assert set(result["context_message_ids"]) <= {
                message.id for message in messages
            }
            assert result["uncertainty_notes"]
            texts.append(result["text"])
        assert len(set(texts)) == 3
        # 语义草稿不落库、不发送
        assert service.storage.list_drafts(target.id) == []
        assert "SEM9Q7" not in audit_text(service)
        entries = semantic_audit_entries(service, "semantic_draft_generated")
        assert entries and set(entries[-1]["detail"]) == {"provider", "count"}
    finally:
        service.shutdown()


def test_local_semantic_invalid_ids() -> None:
    service, _account, _conversation, _messages = local_service()
    try:
        with pytest.raises(NotFoundError):
            service.generate_conversation_semantic("missing-conversation")
        with pytest.raises(NotFoundError):
            service.generate_semantic_draft("missing-message")
        with pytest.raises(ServiceError):
            service.generate_daily_brief(day="2026-01-01")  # type: ignore[arg-type]
    finally:
        service.shutdown()


def test_local_mode_never_calls_external_factory() -> None:
    calls: list[int] = []

    def factory():
        calls.append(1)
        return FakeCompleteProvider(response={"text": "不应被调用"})

    service = make_service(credential_store=InMemoryCredentialStore(), external_provider_factory=factory)
    try:
        _account, conversation, _messages = seed_messages(service)
        service.generate_conversation_semantic(conversation.id)
        service.generate_daily_brief()
        service.generate_semantic_draft(service.list_messages()[0].id)
        assert calls == []
    finally:
        service.shutdown()


def test_local_fallback_when_intel_semantic_missing(monkeypatch) -> None:
    """intel.semantic 未就绪时必须走核心层最小回退，不得崩溃。"""
    import sys

    service, _account, conversation, messages = local_service()
    try:
        monkeypatch.setitem(sys.modules, "social_secretary.intel.semantic", None)
        conversation_result = service.generate_conversation_semantic(conversation.id)
        assert conversation_result["ok"] is True
        assert conversation_result["provider"] == "local_rules"
        assert conversation_result["items"]

        daily_result = service.generate_daily_brief()
        assert daily_result["ok"] is True
        assert daily_result["provider"] == "local_rules"
        assert set(daily_result["sections"]) == {
            "must_today",
            "soon",
            "batch_reply",
            "fyi",
        }

        draft_result = service.generate_semantic_draft(messages[0].id)
        assert draft_result["ok"] is True
        assert draft_result["provider"] == "local_rules"
        assert draft_result["text"]
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 外部模式：成功
# ---------------------------------------------------------------------------

def test_external_conversation_success_filters_ids_and_usage() -> None:
    provider = FakeCompleteProvider()
    service, factory_calls, conversation, messages = external_context(provider)
    try:
        provider.last_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        provider.response = json.dumps(
            {
                "overview": "外部概览内容",
                "items": [
                    {
                        "kind": "request",
                        "text": "外部请求",
                        "source_indexes": [1],
                        "uncertain": False,
                        "due_hint": "明天",
                    }
                ],
                "uncertainty_notes": ["外部模型可能不准确"],
            },
            ensure_ascii=False,
        )
        result = service.generate_conversation_semantic(
            conversation.id, use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "external_llm"
        assert result["overview"] == "外部概览内容"
        assert result["error"] is None
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 5
        assert result["usage"]["total_tokens"] == 15
        assert "estimated_cost" in result["usage"]
        assert result["items"][0]["source_message_ids"] == [messages[0].id]
        assert provider.calls == 1
        assert factory_calls == [1]
        assert "外部概览内容" not in audit_text(service)
        assert "SEM9Q7" not in audit_text(service)
        entries = semantic_audit_entries(service, "semantic_conversation_generated")
        assert entries[-1]["detail"]["provider"] == "external_llm"
        assert set(entries[-1]["detail"]) == {"provider", "count"}
    finally:
        service.shutdown()


def test_external_daily_brief_success() -> None:
    provider = FakeCompleteProvider()
    service, _calls, _conversation, messages = external_context(provider)
    try:
        provider.last_usage = {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
        }
        provider.response = json.dumps(
            {
                "sections": {
                    "must_today": [
                        {
                            "kind": "request",
                            "text": "外部待办",
                            "source_indexes": [1],
                            "uncertain": False,
                        }
                    ],
                    "soon": [],
                    "batch_reply": [],
                    "fyi": [],
                },
                "caveats": ["外部说明"],
            },
            ensure_ascii=False,
        )
        result = service.generate_daily_brief(use_external=True)
        assert result["ok"] is True
        assert result["provider"] == "external_llm"
        assert result["sections"]["must_today"][0]["text"] == "外部待办"
        assert result["citations"] == [messages[0].id]
        assert result["usage"]["total_tokens"] == 28
        assert result["error"] is None
    finally:
        service.shutdown()


def test_external_semantic_draft_success() -> None:
    provider = FakeCompleteProvider()
    service, _calls, _conversation, messages = external_context(provider)
    try:
        provider.response = json.dumps(
            {"text": "外部 AI 草稿文本"}, ensure_ascii=False
        )
        result = service.generate_semantic_draft(
            messages[0].id, style="正式", use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "external_llm"
        assert result["text"] == "外部 AI 草稿文本"
        assert result["style"] == "正式"
        assert set(result["context_message_ids"]) <= {
            message.id for message in messages
        }
        assert provider.calls == 1
        assert service.storage.list_drafts(messages[0].id) == []
        assert "外部 AI 草稿文本" not in audit_text(service)
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 外部模式：门控/失败回退（绝不假成功）
# ---------------------------------------------------------------------------

def test_external_requires_consent_and_falls_back_without_calling() -> None:
    provider = FakeCompleteProvider(response={"overview": "不应出现", "items": []})
    service, factory_calls, conversation, _messages = external_context(
        provider, consent=False
    )
    try:
        result = service.generate_conversation_semantic(
            conversation.id, use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["error"] and "授权" in result["error"]
        assert "不应出现" not in json.dumps(result, ensure_ascii=False)
        assert provider.calls == 0
        assert factory_calls == []
    finally:
        service.shutdown()


def test_external_without_key_falls_back_without_calling() -> None:
    provider = FakeCompleteProvider(response={"overview": "不应出现", "items": []})
    service, factory_calls, conversation, _messages = external_context(
        provider, key=False
    )
    try:
        result = service.generate_conversation_semantic(
            conversation.id, use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["error"] and ("Key" in result["error"] or "Key" in result["error"])
        assert provider.calls == 0
        assert factory_calls == []
    finally:
        service.shutdown()


def test_external_provider_without_complete_falls_back() -> None:
    provider = FakeExternalProvider()  # 只有 propose，无 complete
    service = make_service(
        credential_store=InMemoryCredentialStore(),
        external_provider_factory=lambda: provider,
    )
    try:
        _account, conversation, _messages = seed_messages(service)
        service.set_external_ai_key(API_KEY)
        service.grant_consent(ConsentScope.EXTERNAL_AI, confirmation_text="测试授权")
        result = service.generate_conversation_semantic(
            conversation.id, use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["error"] and "complete" in result["error"]
    finally:
        service.shutdown()


def test_external_failure_falls_back_to_local_with_error() -> None:
    provider = FakeCompleteProvider(fail=True)
    service, _calls, conversation, _messages = external_context(provider)
    try:
        result = service.generate_conversation_semantic(
            conversation.id, use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["items"], "回退后仍应返回本地规则结果"
        assert result["error"] and "已回退本地规则" in result["error"]
        assert provider.calls == 1
        assert "fake complete failed" in result["error"]
        assert "SEM9Q7" not in audit_text(service)
        entries = semantic_audit_entries(service, "semantic_conversation_generated")
        assert entries[-1]["detail"]["provider"] == "local_rules"
    finally:
        service.shutdown()


def test_external_unparseable_draft_falls_back() -> None:
    provider = FakeCompleteProvider(response="not-json-and-empty")
    service, _calls, _conversation, messages = external_context(provider)
    try:
        # 纯文本会被当作 text（可解析）；这里用空响应触发回退
        provider.response = "   "
        result = service.generate_semantic_draft(
            messages[0].id, use_external=True
        )
        assert result["ok"] is True
        assert result["provider"] == "local_rules"
        assert result["error"] and "已回退本地规则" in result["error"]
        assert result["text"]
    finally:
        service.shutdown()
