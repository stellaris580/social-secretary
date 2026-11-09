"""每日摘要测试：按日/分类计数、待办、缓存、caveats、演示标记。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from social_secretary.contracts import (
    Category,
    CollectionResult,
    ScanStatus,
    TodoItem,
    TodoStatus,
)
from social_secretary.core.storage import Storage, now_local
from social_secretary.core.summary import (
    generate_daily_summary,
    get_cached_summary,
    summary_from_dict,
    summary_to_dict,
)
from tests.core.fakes import make_account, make_conversation, make_message


@pytest.fixture()
def env(tmp_path):
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account()
    conversation = make_conversation(account_id=account.id)
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    return SimpleNamespace(storage=storage, account=account, conversation=conversation)


def _insert(env, text, *, category=Category.NORMAL, is_self=False, timestamp=None, collected_at=None):
    message = make_message(
        env.conversation,
        text,
        category=category,
        is_self=is_self,
        timestamp=timestamp,
        collected_at=collected_at,
    )
    assert env.storage.insert_message(message) is True
    return message


def test_counts_and_categories(env) -> None:
    now = now_local()
    important = _insert(env, "明天上午 10 点项目评审，请参加", category=Category.IMPORTANT, timestamp=now)
    _insert(env, "我自己的一条重要消息", category=Category.IMPORTANT, is_self=True, timestamp=now)
    _insert(env, "周末团建报名", category=Category.NORMAL, timestamp=now)
    _insert(env, "邮件周报已更新", category=Category.FYI, timestamp=now)

    summary = generate_daily_summary(env.storage, day=now.date())
    assert summary.day == now.date()
    assert summary.counts["total"] == 3          # 己方消息不计入“收到的消息”
    assert summary.counts["important"] == 1
    assert summary.counts["normal"] == 1
    assert summary.counts["fyi"] == 1
    assert summary.counts["sent"] == 1
    assert summary.counts["conversations"] == 1
    assert [item.id for item in summary.important] == [important.id]
    assert len(summary.normal) == 1 and len(summary.fyi) == 1
    assert summary.conversations_touched == 1
    assert "3 条收到的消息" in summary.narrative
    assert "重要 1 条" in summary.narrative
    assert "不会自动发送任何消息" in summary.narrative


def test_day_filtering(env) -> None:
    now = now_local()
    yesterday = now - timedelta(days=1)
    _insert(env, "昨天的消息", timestamp=yesterday)
    _insert(env, "今天的消息", timestamp=now)
    summary = generate_daily_summary(env.storage, day=now.date())
    assert summary.counts["total"] == 1
    assert summary.normal[0].text == "今天的消息"

    yesterday_summary = generate_daily_summary(env.storage, day=yesterday.date())
    assert yesterday_summary.counts["total"] == 1
    assert yesterday_summary.normal[0].text == "昨天的消息"


def test_collected_timestamp_uses_collected_at_day(env) -> None:
    yesterday = now_local() - timedelta(days=1)
    _insert(env, "没有原始时间的消息", timestamp=None, collected_at=yesterday)
    summary = generate_daily_summary(env.storage, day=now_local().date())
    assert summary.counts["total"] == 0
    yesterday_summary = generate_daily_summary(env.storage, day=yesterday.date())
    assert yesterday_summary.counts["total"] == 1
    assert any("采集时间" in caveat for caveat in yesterday_summary.caveats)


def test_todos_in_summary(env) -> None:
    now = now_local()
    message = _insert(env, "请今天下班前确认预算表", category=Category.IMPORTANT, timestamp=now)
    todo = TodoItem(
        id="todo-1",
        message_id=message.id,
        conversation_id=env.conversation.id,
        conversation_title=env.conversation.title,
        text="确认预算表",
        due_text="今天下班前",
        confidence=0.8,
        created_at=now,
    )
    env.storage.insert_todo(todo)
    summary = generate_daily_summary(env.storage, day=now.date())
    assert len(summary.todos) == 1
    assert summary.counts["todos"] == 1
    assert "1 条待办" in summary.narrative

    env.storage.update_todo_status("todo-1", TodoStatus.DONE)
    summary2 = generate_daily_summary(env.storage, day=now.date())
    assert summary2.todos[0].status is TodoStatus.DONE
    assert summary2.counts["todos"] == 0


def test_cache_roundtrip(env) -> None:
    now = now_local()
    _insert(env, "缓存测试消息", timestamp=now)
    assert get_cached_summary(env.storage, now.date()) is None

    summary = generate_daily_summary(env.storage, day=now.date())
    cached = get_cached_summary(env.storage, now.date())
    assert cached is not None
    assert cached.day == summary.day
    assert cached.counts == summary.counts
    assert cached.narrative == summary.narrative
    assert cached.generated_at == summary.generated_at

    restored = summary_from_dict(summary_to_dict(summary))
    assert restored.counts == summary.counts
    assert restored.narrative == summary.narrative
    assert len(restored.normal) == 1

    env.storage.clear_summaries()
    assert get_cached_summary(env.storage, now.date()) is None


def test_caveats_for_failed_scan_and_foreground(env) -> None:
    now = now_local()
    _insert(env, "有消息但采集部分失败", timestamp=now)
    env.storage.insert_scan_run(
        CollectionResult(
            scan_id="scan-partial",
            account_id=env.account.id,
            status=ScanStatus.PARTIAL,
            error="某个会话失败",
            started_at=now,
            finished_at=now,
            foreground_used=True,
        )
    )
    summary = generate_daily_summary(env.storage, day=now.date())
    assert summary.scan_status is ScanStatus.PARTIAL
    joined = "；".join(summary.caveats)
    assert "采集存在失败" in joined
    assert "前台" in joined
    # QQ 可见范围 caveat（消息属于私聊会话）
    assert "可见" in joined


def test_empty_day_caveat(env) -> None:
    day = now_local().date()
    summary = generate_daily_summary(env.storage, day=day)
    assert summary.counts["total"] == 0
    assert "没有新的消息" in summary.narrative
    joined = "；".join(summary.caveats)
    assert "还没有采集记录" in joined


def test_demo_summary_marks_demo(env) -> None:
    now = now_local()
    _insert(env, "演示消息", timestamp=now)
    summary = generate_daily_summary(env.storage, day=now.date(), is_demo=True)
    assert summary.is_demo is True
    assert any("演示数据" in caveat for caveat in summary.caveats)
    cached = get_cached_summary(env.storage, now.date())
    assert cached is not None and cached.is_demo is True
