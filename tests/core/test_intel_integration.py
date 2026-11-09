"""intel 真实实现与核心层集成测试（不联网；intel 未就绪时 skip）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("social_secretary.intel.rules")
pytest.importorskip("social_secretary.intel.drafts")

from social_secretary.contracts import Category, DraftKind, TimestampSource  # noqa: E402
from social_secretary.core.pipeline import ScanPipeline  # noqa: E402
from social_secretary.core.storage import Storage, now_local  # noqa: E402
from social_secretary.intel.drafts import LocalDraftProvider  # noqa: E402
from social_secretary.intel.rules import RuleClassifier, RuleTodoExtractor  # noqa: E402
from tests.core.fakes import (  # noqa: E402
    FakeMessageSource,
    make_account,
    make_conversation,
    make_message,
    make_raw,
)

TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=TZ)


@pytest.fixture()
def env(tmp_path):
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account()
    conversation = make_conversation("conv-intel", account_id=account.id, title="张三", external_key="张三")
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    raw = make_raw(
        "张三",
        "请明天上午10点前提交周报，谢谢。",
        timestamp=FIXED_NOW,
        source_locator="row-1",
        external_id="ext-pipeline-1",
    )
    source = FakeMessageSource(conversations=[conversation], messages={"张三": [raw]})
    pipeline = ScanPipeline(
        storage,
        source_factory=lambda account: source,
        classifier=RuleClassifier(),
        todo_extractor=RuleTodoExtractor(now_provider=lambda: FIXED_NOW),
    )
    return SimpleNamespace(
        storage=storage,
        account=account,
        conversation=conversation,
        pipeline=pipeline,
    )


def test_real_rule_classifier_and_todo_extractor_in_pipeline(env) -> None:
    results = env.pipeline.run([env.account.id])
    assert results[0].status.value == "ok"
    assert results[0].messages_new == 1

    message = env.storage.list_messages()[0]
    assert message.category is Category.IMPORTANT
    assert message.category_reasons  # 可解释的理由
    assert any("动作" in reason or "请求" in reason or "截止" in reason for reason in message.category_reasons)

    todos = env.storage.list_todos()
    assert len(todos) == 1
    assert todos[0].due_at is not None
    expected_due = FIXED_NOW + timedelta(days=1)
    assert todos[0].due_at.hour == 10
    assert todos[0].due_at.date() == expected_due.date()
    assert todos[0].confidence > 0.5


def test_real_local_draft_provider_offline(env) -> None:
    message = make_message(
        env.conversation,
        "请问明天几点开会？",
        timestamp=FIXED_NOW,
        timestamp_source=TimestampSource.UI,
    )
    provider = LocalDraftProvider(now_provider=lambda: FIXED_NOW)
    from social_secretary.contracts import DraftContext

    drafts = provider.propose(
        message, DraftContext(conversation_title=env.conversation.title, style="礼貌")
    )
    assert drafts
    assert all(draft.kind is DraftKind.LOCAL_RULES for draft in drafts)
    assert all(draft.requires_external_consent is False for draft in drafts)
    assert all(draft.text.strip() for draft in drafts)
    # 本地草稿不会把对方消息原文整句复制进去（隐私设计）
    assert "请问明天几点开会" not in "".join(draft.text for draft in drafts)


def test_default_service_auto_imports_intel(env) -> None:
    from social_secretary.core.service import SocialSecretaryService
    from social_secretary.security.credential_store import InMemoryCredentialStore

    service = SocialSecretaryService(credential_store=InMemoryCredentialStore())
    try:
        assert isinstance(service._get_classifier(), RuleClassifier)
        assert isinstance(service._get_todo_extractor(), RuleTodoExtractor)
        assert isinstance(service._get_draft_provider(), LocalDraftProvider)
    finally:
        service.shutdown()
