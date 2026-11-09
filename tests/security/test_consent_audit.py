"""同意门控与操作审计测试。"""

from __future__ import annotations

import pytest

from social_secretary.contracts import ConsentScope
from social_secretary.core.storage import Storage
from social_secretary.security.audit import AuditLogger, sanitize_detail
from social_secretary.security.consent import (
    ConsentManager,
    ConsentRequired,
    InMemoryConsentLedger,
)

API_KEY = "sk-testplaceholder1234567890"


# ---------------------------------------------------------------------------
# 同意
# ---------------------------------------------------------------------------

def test_consent_required_grant_and_revoke() -> None:
    manager = ConsentManager(InMemoryConsentLedger())
    assert manager.is_granted(ConsentScope.EXTERNAL_AI) is False

    with pytest.raises(ConsentRequired) as excinfo:
        manager.require(ConsentScope.EXTERNAL_AI, action="生成外部 AI 草稿")
    assert excinfo.value.scope == ConsentScope.EXTERNAL_AI
    assert "需要用户明确同意" in excinfo.value.user_message
    assert "外部 AI" in excinfo.value.user_message

    manager.grant(ConsentScope.EXTERNAL_AI, confirmation_text="我明确同意")
    assert manager.is_granted(ConsentScope.EXTERNAL_AI) is True
    manager.require(ConsentScope.EXTERNAL_AI)  # 不抛异常

    history = manager.history()
    assert history[0]["action"] == "grant"
    assert history[0]["scope"] == "external_ai"
    # 用户输入的确认文本原文不落库，只记录长度
    assert "我明确同意" not in str(history)

    manager.revoke(ConsentScope.EXTERNAL_AI)
    assert manager.is_granted(ConsentScope.EXTERNAL_AI) is False
    with pytest.raises(ConsentRequired):
        manager.require(ConsentScope.EXTERNAL_AI)
    assert manager.history()[0]["action"] == "revoke"


def test_consent_persists_in_storage(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    manager = ConsentManager(storage)
    manager.grant(ConsentScope.QQ_READ)
    manager.grant(ConsentScope.MAIL_READ)

    # 新的 manager 实例仍能读到授权（落库）
    second = ConsentManager(storage)
    assert second.is_granted(ConsentScope.QQ_READ) is True
    assert second.is_granted(ConsentScope.MAIL_READ) is True
    assert second.is_granted(ConsentScope.EXTERNAL_AI) is False
    assert second.states() == {
        ConsentScope.QQ_READ: True,
        ConsentScope.MAIL_READ: True,
        ConsentScope.EXTERNAL_AI: False,
    }


def test_consent_accepts_string_scope() -> None:
    manager = ConsentManager(InMemoryConsentLedger())
    manager.grant("qq_read")
    assert manager.is_granted("qq_read") is True
    assert manager.is_granted(ConsentScope.QQ_READ) is True


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def test_sanitize_detail_redacts_content_and_secrets() -> None:
    cleaned = sanitize_detail(
        {
            "text": "私密消息正文",
            "draft": "草稿正文",
            "api_key": API_KEY,
            "password": "hunter2",
            "message_count": 7,
            "conversation_id": "conv-1",
            "nested": {"content": "嵌套正文", "ok": True},
        }
    )
    serialized = str(cleaned)
    assert "私密消息正文" not in serialized
    assert "草稿正文" not in serialized
    assert API_KEY not in serialized
    assert "hunter2" not in serialized
    assert "嵌套正文" not in serialized
    assert cleaned["message_count"] == 7
    assert cleaned["conversation_id"] == "conv-1"
    assert cleaned["nested"]["ok"] is True


def test_audit_logger_writes_sanitized_events(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    audit = AuditLogger(storage)
    audit.log(
        "drafts_generated",
        target_kind="message",
        target_id="m1",
        detail={"count": 2, "text": "原始消息正文", "api_key": API_KEY},
    )
    events = storage.list_audit()
    assert len(events) == 1
    assert events[0]["action"] == "drafts_generated"
    serialized = str(events[0])
    assert "原始消息正文" not in serialized
    assert API_KEY not in serialized
    assert events[0]["detail"]["count"] == 2


def test_storage_append_audit_is_defensive_against_untrusted_detail(tmp_path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    storage.append_audit(
        "suspicious",
        detail={"text": "即使调用方忘记脱敏也不落明文", "token": API_KEY},
    )
    raw = storage.list_audit()[0]
    assert "即使调用方忘记脱敏也不落明文" not in str(raw)
    assert API_KEY not in str(raw)
