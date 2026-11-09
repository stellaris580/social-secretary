"""采集流水线测试：编排/白名单/暂停/部分失败/去重/待办/异常兜底。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from social_secretary.contracts import (
    Category,
    ConversationKind,
    ExtractedTodo,
    ScanStatus,
)
from social_secretary.core.pipeline import ScanPipeline
from social_secretary.core.storage import Storage, now_local
from tests.core.fakes import (
    FakeClassifier,
    FakeMessageSource,
    FakeTodoExtractor,
    make_account,
    make_conversation,
    make_raw,
)


@pytest.fixture()
def env(tmp_path):
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account()
    storage.upsert_account(account)
    conv_private = make_conversation(
        "conv-private", account_id=account.id, title="张三", external_key="张三"
    )
    conv_group = make_conversation(
        "conv-group",
        account_id=account.id,
        title="项目群",
        external_key="项目群",
        kind=ConversationKind.GROUP,
    )
    for conversation in (conv_private, conv_group):
        storage.upsert_conversation(conversation)

    now = now_local()
    source = FakeMessageSource(
        conversations=[conv_private, conv_group],
        messages={
            "张三": [
                make_raw(
                    "张三",
                    "请明天上午十点前提交周报",
                    timestamp=now,
                    source_locator="row-1",
                    external_id="ext-1",
                ),
                make_raw("张三", "第二条没有时间的消息", source_locator="row-2", external_id="ext-2"),
            ],
            "项目群": [
                make_raw(
                    "项目群",
                    "群公告：明天下午开会",
                    timestamp=now,
                    source_locator="row-1",
                    external_id="ext-3",
                )
            ],
        },
    )
    classifier = FakeClassifier({"请明天上午十点前提交周报": Category.IMPORTANT})
    todo_extractor = FakeTodoExtractor(
        {
            "请明天上午十点前提交周报": [
                ExtractedTodo(text="提交周报", due_text="明天上午十点前", confidence=0.8)
            ]
        }
    )
    pipeline = ScanPipeline(
        storage,
        source_factory=lambda account: source,
        classifier=classifier,
        todo_extractor=todo_extractor,
    )
    return SimpleNamespace(
        storage=storage,
        account=account,
        conv_private=conv_private,
        conv_group=conv_group,
        source=source,
        classifier=classifier,
        todo_extractor=todo_extractor,
        pipeline=pipeline,
    )


def test_scan_ok_ingests_records_and_extracts(env) -> None:
    results = env.pipeline.run([env.account.id])
    assert len(results) == 1
    result = results[0]
    assert result.status is ScanStatus.OK
    assert result.messages_seen == 3
    assert result.messages_new == 3
    assert result.conversations_scanned == 2
    assert result.error is None
    assert result.foreground_used is False
    assert result.duration_ms >= 0
    assert env.source.closed is True

    assert env.storage.count_messages() == 3
    assert env.classifier.calls == 3
    assert env.todo_extractor.calls == 3
    todos = env.storage.list_todos()
    assert len(todos) == 1
    assert todos[0].text == "提交周报"
    assert todos[0].conversation_title == "张三"

    # 分类推理原因落库
    important = env.storage.list_messages(category=Category.IMPORTANT)
    assert len(important) == 1
    assert important[0].category_reasons == ("fake-rule:important",)

    runs = env.storage.list_scan_runs()
    assert len(runs) == 1
    assert runs[0].status is ScanStatus.OK
    audit_actions = {event["action"] for event in env.storage.list_audit()}
    assert "scan_account_finished" in audit_actions

    # 会话扫描时间已更新
    updated = env.storage.get_conversation(env.conv_private.id)
    assert updated is not None and updated.last_scanned_at is not None


def test_whitelist_only_enabled_conversations_are_fetched(env) -> None:
    env.storage.update_conversation(env.conv_group.id, enabled=False)
    results = env.pipeline.run([env.account.id])
    assert results[0].status is ScanStatus.OK
    assert env.source.fetch_calls == [env.conv_private.id]
    # 群消息没有被采集
    assert env.storage.count_messages() == 2
    titles = {message.conversation_title for message in env.storage.list_messages()}
    assert titles == {"张三"}


def test_paused_returns_paused_without_touching_source(env) -> None:
    pipeline = ScanPipeline(
        env.storage,
        source_factory=lambda account: env.source,
        is_paused=lambda: True,
    )
    results = pipeline.run([env.account.id])
    assert results[0].status is ScanStatus.PAUSED
    assert env.source.fetch_calls == []
    assert env.storage.count_scan_runs() == 0


def test_partial_failure_when_one_conversation_fails(env) -> None:
    env.source.fail_conversations = {env.conv_group.id}
    results = env.pipeline.run([env.account.id])
    result = results[0]
    assert result.status is ScanStatus.PARTIAL
    assert result.messages_new == 2
    assert result.conversations_scanned == 1
    assert result.error and "RuntimeError" in result.error
    assert any("项目群" in warning for warning in result.warnings)
    assert env.storage.scan_status_for_day(now_local().date()) is ScanStatus.PARTIAL


def test_source_unavailable_is_failed_not_crash(env) -> None:
    pipeline = ScanPipeline(env.storage, source_factory=lambda account: None)
    results = pipeline.run([env.account.id])
    assert results[0].status is ScanStatus.FAILED
    assert results[0].messages_new == 0
    assert "采集器不可用" in (results[0].error or "")
    assert env.storage.count_messages() == 0


def test_source_factory_exception_is_failed(env) -> None:
    def factory(account):
        raise RuntimeError("采集器初始化爆炸")

    pipeline = ScanPipeline(env.storage, source_factory=factory)
    results = pipeline.run([env.account.id])
    assert results[0].status is ScanStatus.FAILED
    assert "RuntimeError" in (results[0].error or "")
    assert env.storage.count_scan_runs() == 1


def test_second_scan_is_idempotent(env) -> None:
    first = env.pipeline.run([env.account.id])[0]
    assert first.messages_new == 3
    second = env.pipeline.run([env.account.id])[0]
    assert second.status is ScanStatus.OK
    assert second.messages_seen == 3
    assert second.messages_new == 0
    assert env.storage.count_messages() == 3


def test_classifier_exception_falls_back_to_normal(env) -> None:
    class BoomClassifier:
        def classify(self, *args, **kwargs):
            raise RuntimeError("分类器坏了")

    pipeline = ScanPipeline(
        env.storage,
        source_factory=lambda account: env.source,
        classifier=BoomClassifier(),
        todo_extractor=FakeTodoExtractor(),
    )
    result = pipeline.run([env.account.id])[0]
    assert result.status is ScanStatus.OK
    assert env.storage.count_messages() == 3
    assert all(
        message.category is Category.NORMAL for message in env.storage.list_messages()
    )
    assert any("分类失败" in warning for warning in result.warnings)


def test_consent_gate_blocks_scan(env) -> None:
    results = env.pipeline.run(
        [env.account.id], consent_gate=lambda account: "未授予读取同意"
    )
    assert results[0].status is ScanStatus.BLOCKED
    assert env.source.fetch_calls == []
    assert env.storage.count_messages() == 0
    assert env.storage.count_scan_runs() == 1


def test_unknown_conversation_messages_are_skipped(env) -> None:
    env.source.messages = {
        "张三": [make_raw("未授权的会话", "这条消息不该入库", source_locator="row-x")]
    }
    env.source.conversations = [env.conv_private]
    results = env.pipeline.run([env.account.id])
    assert results[0].status is ScanStatus.OK
    assert env.storage.count_messages() == 0
    assert any("忽略未知会话" in warning for warning in results[0].warnings)


def test_qq_foreground_ack_flag_recorded(env) -> None:
    results = env.pipeline.run([env.account.id], foreground_ack=True)
    assert results[0].foreground_used is True
    runs = env.storage.list_scan_runs()
    assert runs[0].foreground_used is True


def test_conversation_key_can_be_conversation_id_for_mail(env) -> None:
    """邮件采集器用 conversation.id 回填 conversation_key，也必须能落库。"""
    env.source.messages = {
        "张三": [
            make_raw(
                env.conv_private.id,
                "请明天提交周报",
                timestamp=now_local(),
                external_id="imap:1:1",
                source_locator="IMAP INBOX UID 1",
            )
        ]
    }
    env.source.conversations = [env.conv_private]
    results = env.pipeline.run([env.account.id])
    assert results[0].status is ScanStatus.OK
    assert results[0].messages_new == 1
    message = env.storage.list_messages()[0]
    assert message.conversation_id == env.conv_private.id
