"""端到端集成测试：core.Service 语义接口 ↔ intel.semantic（不联网）。

对应 v0.2.0 C 阶段：app-core 负责 Service 接线，本测试验证 intel 函数与
Service 返回结构契约对齐（本地规则 + 注入 fake external provider 的外部路径/回退）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("social_secretary.core.service")
pytest.importorskip("social_secretary.intel.semantic")

from social_secretary.contracts import (  # noqa: E402
    Account,
    AccountKind,
    ConsentScope,
    Conversation,
    ConversationKind,
    Message,
    Priority,
    TimestampSource,
)
from social_secretary.core.dedupe import compute_content_hash  # noqa: E402
from social_secretary.core.service import SocialSecretaryService  # noqa: E402
from social_secretary.security.credential_store import (  # noqa: E402
    EXTERNAL_AI_KEY,
    InMemoryCredentialStore,
)

TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 11, 3, 10, 0, tzinfo=TZ)


def _seed(service) -> list[Message]:
    now = FIXED_NOW
    day = now.date()
    account = Account(id="acc-qq", kind=AccountKind.QQ, display_name="合成账号", identifier="")
    conversation = Conversation(
        id="conv-1",
        account_id=account.id,
        kind=ConversationKind.PRIVATE,
        title="合成会话",
        external_key="synthetic:conv-1",
        priority=Priority.NORMAL,
    )
    service.storage.upsert_account(account)
    service.storage.upsert_conversation(conversation)
    specs = [
        ("m-1", "明天下午3点前能把测试报告发我吗？", False),
        ("m-2", "我会在明天上午10点前处理完。", True),
        ("m-3", "今天下午5点前提交周报。", False),
    ]
    messages: list[Message] = []
    for index, (message_id, text, is_self) in enumerate(specs):
        timestamp = now - timedelta(minutes=10 - index)
        sender = "我" if is_self else "合成联系人"
        content_hash = compute_content_hash(
            conversation.id, sender, is_self, text, timestamp, TimestampSource.UI
        )
        message = Message(
            id=message_id,
            account_id=account.id,
            conversation_id=conversation.id,
            conversation_title=conversation.title,
            conversation_kind=ConversationKind.PRIVATE,
            sender=sender,
            is_self=is_self,
            text=text,
            timestamp=timestamp,
            timestamp_source=TimestampSource.UI,
            collected_at=now,
            content_hash=content_hash,
        )
        service.storage.insert_message(message)
        messages.append(message)
    return messages


class _FakeCompleteClient:
    def __init__(self, payload: dict | str):
        self.payload = payload
        self.last_usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)


def _fake_provider(client):
    """模拟核心层 _make_external_provider 的返回：带 propose 的 provider，_client 有 complete。"""
    return SimpleNamespace(_client=client, propose=lambda *args, **kwargs: [])


@pytest.fixture()
def service():
    store = InMemoryCredentialStore()
    provider_holder: dict = {}

    def factory():
        return provider_holder["provider"]

    instance = SocialSecretaryService(
        credential_store=store, external_provider_factory=factory
    )
    try:
        _seed(instance)
        instance.provider_holder = provider_holder  # type: ignore[attr-defined]
        instance.test_credential_store = store  # type: ignore[attr-defined]
        yield instance
    finally:
        instance.shutdown()


def test_service_local_conversation_semantic(service):
    result = service.generate_conversation_semantic("conv-1")
    assert result["provider"] == "local_rules"
    assert result["ok"] is True
    assert result["items"]
    real_ids = {"m-1", "m-2", "m-3"}
    assert all(set(item["source_message_ids"]) <= real_ids for item in result["items"])
    for category in ("questions", "requests", "deadlines", "promises", "unresolved", "chitchat"):
        assert isinstance(result[category], list)
        for item in result[category]:
            assert item["source_message_ids"]
    assert isinstance(result["usage"], dict)


def test_service_local_daily_brief(service):
    result = service.generate_daily_brief(day=FIXED_NOW.date())
    assert result["provider"] == "local_rules"
    assert result["day"] == FIXED_NOW.date().isoformat()
    assert set(result["sections"]) == {"must_today", "soon", "batch_reply", "fyi"}
    assert result["citations"]
    assert all(isinstance(citation, str) for citation in result["citations"])


def test_service_local_semantic_draft(service):
    result = service.generate_semantic_draft("m-1", style="正式")
    assert result["provider"] == "local_rules"
    assert result["message_id"] == "m-1"
    assert result["style"] == "正式"
    assert result["text"].strip()
    assert "已发送" not in result["text"]


def test_service_external_conversation_semantic(service):
    service.grant_consent(ConsentScope.EXTERNAL_AI, confirmation_text="集成测试")
    service.test_credential_store.set_secret(EXTERNAL_AI_KEY, "test-only-key")
    payload = {
        "overview": "对方要求提交测试报告。",
        "items": [
            {
                "kind": "request",
                "text": "明天下午3点前提交测试报告",
                "source_indexes": [1],
                "uncertain": False,
                "due_hint": "明天下午3点前",
            }
        ],
        "uncertainty_notes": [],
    }
    service.provider_holder["provider"] = _fake_provider(
        _FakeCompleteClient(payload)
    )
    result = service.generate_conversation_semantic("conv-1", use_external=True)
    assert result["provider"] == "external_llm"
    assert result["items"][0]["source_message_ids"] == ["m-1"]
    assert result["usage"]["total_tokens"] == 16
    assert result["error"] is None


def test_service_external_invalid_json_falls_back(service):
    service.grant_consent(ConsentScope.EXTERNAL_AI, confirmation_text="集成测试")
    service.test_credential_store.set_secret(EXTERNAL_AI_KEY, "test-only-key")
    service.provider_holder["provider"] = _fake_provider(_FakeCompleteClient("not-json"))
    result = service.generate_conversation_semantic("conv-1", use_external=True)
    assert result["provider"] == "local_rules"
    assert result["error"]
    assert result["items"]
