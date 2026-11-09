"""外部 AI 同意门控测试：未同意绝不调用/不联网；同意后才走注入的 fake provider。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from social_secretary.contracts import ConsentScope, DraftKind
from social_secretary.core.service import ServiceError
from social_secretary.security.consent import ConsentRequired
from social_secretary.security.credential_store import EXTERNAL_AI_KEY
from tests.core.fakes import FakeExternalProvider, assert_service_api_surface, make_service

API_KEY = "sk-testplaceholder1234567890"


@pytest.fixture()
def env():
    external = FakeExternalProvider()
    factory_calls: list[int] = []

    def provider_factory():
        factory_calls.append(1)
        return external

    service = make_service(external_provider_factory=provider_factory)
    service.load_demo_data()
    service.set_external_ai_key(API_KEY)
    try:
        yield SimpleNamespace(
            service=service,
            external=external,
            factory_calls=factory_calls,
            message_id="demo-msg-1",
        )
    finally:
        service.shutdown()


def test_local_drafts_never_touch_external_provider(env) -> None:
    drafts = env.service.generate_drafts(env.message_id)
    assert drafts
    assert all(draft.kind is DraftKind.LOCAL_RULES for draft in drafts)
    assert all(draft.requires_external_consent is False for draft in drafts)
    assert env.external.calls == 0
    assert env.factory_calls == []


def test_external_drafts_blocked_without_consent(env) -> None:
    with pytest.raises(ConsentRequired) as excinfo:
        env.service.generate_drafts(env.message_id, use_external=True)
    assert excinfo.value.scope is ConsentScope.EXTERNAL_AI
    assert "需要用户明确同意" in excinfo.value.user_message
    # 关键：未同意时连 provider 工厂都不能被调用（更不会联网）
    assert env.external.calls == 0
    assert env.factory_calls == []


def test_external_drafts_after_consent_use_fake_provider(env) -> None:
    env.service.grant_consent(ConsentScope.EXTERNAL_AI, confirmation_text="我同意使用外部 AI")
    drafts = env.service.generate_drafts(env.message_id, styles=["简洁"], use_external=True)
    assert drafts
    assert env.external.calls >= 1
    assert env.factory_calls == [1]
    assert all(draft.kind is DraftKind.EXTERNAL_LLM for draft in drafts)
    assert all(draft.requires_external_consent is True for draft in drafts)
    assert all(draft.text == "fake-external 草稿内容" for draft in drafts)

    # 草稿正文不得进入审计日志
    audit_blob = str(env.service.storage.list_audit())
    assert "fake-external 草稿内容" not in audit_blob

    # 落库并可编辑
    stored = env.service.list_drafts(env.message_id)
    assert stored
    env.service.save_draft_edit(stored[0].id, "用户编辑后的草稿")
    assert env.service.list_drafts(env.message_id)[0].edited_text == "用户编辑后的草稿"


def test_test_external_ai_blocked_without_consent(env) -> None:
    check = env.service.test_external_ai()
    assert check.ok is False
    assert "同意" in check.detail
    assert env.external.test_calls == 0
    assert env.factory_calls == []


def test_test_external_ai_with_consent_and_provider(env) -> None:
    env.service.grant_consent(ConsentScope.EXTERNAL_AI)
    check = env.service.test_external_ai()
    assert check.ok is True
    assert env.external.test_calls == 1


def test_test_external_ai_requires_saved_key(env) -> None:
    env.service.grant_consent(ConsentScope.EXTERNAL_AI)
    env.service.credential_store.delete_secret(EXTERNAL_AI_KEY)
    check = env.service.test_external_ai()
    assert check.ok is False
    assert "Key" in check.detail


def test_external_provider_unavailable_after_consent_raises() -> None:
    service = make_service()  # 未注入外部 provider，也未配置 base_url/model
    service.load_demo_data()
    service.set_external_ai_key(API_KEY)
    service.grant_consent(ConsentScope.EXTERNAL_AI)
    try:
        with pytest.raises(ServiceError):
            service.generate_drafts("demo-msg-1", use_external=True)
    finally:
        service.shutdown()


def test_external_ai_key_only_in_credential_store(env) -> None:
    # key 只进内存凭据库，不落任何数据文件；审计只记录长度
    assert env.service.credential_store.get_secret(EXTERNAL_AI_KEY) == API_KEY
    data_dir = env.service.paths.data_dir
    for path in data_dir.rglob("*"):
        if path.is_file():
            assert API_KEY.encode() not in path.read_bytes(), f"API Key 泄漏到文件：{path}"
    audit_blob = str(env.service.storage.list_audit())
    assert API_KEY not in audit_blob
    assert "external_ai_key_saved" in audit_blob


def test_service_implements_service_api(env) -> None:
    assert_service_api_surface(env.service)


def test_external_ai_consent_mirrored_in_settings(env) -> None:
    assert env.service.get_settings().external_ai_consent is False
    env.service.grant_consent(ConsentScope.EXTERNAL_AI)
    assert env.service.get_settings().external_ai_consent is True
    env.service.revoke_consent(ConsentScope.EXTERNAL_AI)
    assert env.service.get_settings().external_ai_consent is False
