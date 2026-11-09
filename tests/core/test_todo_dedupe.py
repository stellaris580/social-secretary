"""v0.2.0 交付阻塞修复：待办重复创建（todo 去重）测试。

覆盖：
- v4→v5 迁移：旧重复待办按 (message_id, 规范化 text) 合并、保留最早行、合并 done/dismissed 状态；
- 唯一索引 + `insert_todo(s)` 重复时被忽略（返回 False/0），不覆盖用户状态；
- pipeline 重复扫描不新增待办；同消息不同待办保留；extractor 同文本只留一条；
- schema 版本更新、迁移幂等。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ExtractedTodo,
    ScanStatus,
    TodoItem,
    TodoStatus,
)
from social_secretary.core.storage import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Storage,
    normalize_todo_text,
    now_local,
)
from social_secretary.core.pipeline import ScanPipeline
from tests.core.fakes import (
    FakeMessageSource,
    FakeTodoExtractor,
    make_account,
    make_conversation,
    make_message,
    make_raw,
)


# ---------------------------------------------------------------------------
# 迁移 v4 -> v5：旧重复 de-dupe
# ---------------------------------------------------------------------------

def _make_legacy_v4_db(path) -> dict[str, str]:
    """构造 v4 库 + 一条消息 + 重复待办，返回 {占位名: todo_id}。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2, 3, 4):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (4, '2026-01-01T00:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO accounts(id, kind, display_name, enabled, identifier)"
            " VALUES ('acct-todo', 'email', '待办邮箱', 1, 'todo@example.com')"
        )
        conn.execute(
            "INSERT INTO conversations(id, account_id, kind, title, external_key, enabled)"
            " VALUES ('conv-todo', 'acct-todo', 'mail', 'INBOX', 'INBOX', 1)"
        )
        conn.execute(
            """
            INSERT INTO messages(
                id, account_id, conversation_id, conversation_title, conversation_kind,
                sender, is_self, text, collected_at, sort_ts, local_day, content_hash
            ) VALUES (
                'msg-todo', 'acct-todo', 'conv-todo', 'INBOX', 'mail',
                '同事', 0, '请明天提交周报', '2026-01-01T09:00:00+08:00', 0,
                '2026-01-01', 'hash-todo'
            )
            """
        )
        rows = [
            ("todo-open-1", "提交周报", "open"),
            ("todo-open-2", " 提交周报 ", "open"),  # 规范化后重复
            ("todo-done", "提交周报", "done"),  # 重复且已完成 → 保留行状态应为 done
            ("todo-other", "回复客户邮件", "open"),  # 不同待办保留
            ("todo-dismissed", "安排会议", "dismissed"),
            ("todo-dismissed-dup", "安排会议 ", "open"),
        ]
        for todo_id, text, status in rows:
            conn.execute(
                """
                INSERT INTO todos(
                    id, message_id, conversation_id, conversation_title, text,
                    due_at, due_text, confidence, status, created_at, local_day
                ) VALUES (?, 'msg-todo', 'conv-todo', 'INBOX', ?, NULL, '明天', 0.8, ?,
                          '2026-01-01T09:00:00+08:00', '2026-01-01')
                """,
                (todo_id, text, status),
            )
    return {
        "open_1": "todo-open-1",
        "open_2": "todo-open-2",
        "done": "todo-done",
        "other": "todo-other",
        "dismissed": "todo-dismissed",
        "dismissed_dup": "todo-dismissed-dup",
    }


def test_migration_v4_to_v5_dedupes_todos_and_merges_status(tmp_path) -> None:
    db_path = tmp_path / "legacy-v4-todos.db"
    ids = _make_legacy_v4_db(db_path)

    storage = Storage(db_path)
    storage.initialize()
    assert storage.schema_version == SCHEMA_VERSION == 5

    with sqlite3.connect(db_path) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(todos)")}
        rows = conn.execute(
            "SELECT id, text, status FROM todos ORDER BY rowid"
        ).fetchall()
    assert "idx_todos_message_text" in indexes
    # 去重后仅保留「提交周报」1 条 + 「回复客户邮件」1 条 + 「安排会议」1 条
    assert len(rows) == 3
    assert {row[1] for row in rows} == {"提交周报", "回复客户邮件", "安排会议"}
    status_by_text = {row[1]: row[2] for row in rows}
    assert status_by_text["提交周报"] == TodoStatus.DONE.value  # 合并 done
    assert status_by_text["安排会议"] == TodoStatus.DISMISSED.value  # 合并 dismissed
    assert status_by_text["回复客户邮件"] == TodoStatus.OPEN.value
    # 保留最早一行 id（open-1）
    assert ids["open_1"] in {row[0] for row in rows}
    assert ids["open_2"] not in {row[0] for row in rows}

    # 迁移幂等：再次 initialize 不报错、不重复
    storage.initialize()
    assert storage.schema_version == 5
    assert len(storage.list_todos(include_done=True)) == 2  # DISMISSED 默认隐藏
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    assert total == 3


def test_migration_from_v1_goes_to_v5_and_creates_unique_index(tmp_path) -> None:
    db_path = tmp_path / "legacy-v1.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_meta (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        for statement in MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (1, '2026-01-01T00:00:00+08:00')"
        )
    storage = Storage(db_path)
    storage.initialize()
    assert storage.schema_version == 5
    with sqlite3.connect(db_path) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(todos)")}
    assert "idx_todos_message_text" in indexes


# ---------------------------------------------------------------------------
# 重复插入被忽略 / 不同待办保留 / 状态保持
# ---------------------------------------------------------------------------

def _storage_with_message(tmp_path) -> tuple[Storage, str, str]:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-todo", account_id=account.id, title="INBOX", external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    message = make_message(conversation, "请明天提交周报", timestamp=now_local())
    storage.insert_message(message)
    return storage, account.id, message.id


def _todo(message_id: str, text: str, *, todo_id: str, status=TodoStatus.OPEN) -> TodoItem:
    return TodoItem(
        id=todo_id,
        message_id=message_id,
        conversation_id="conv-todo",
        conversation_title="INBOX",
        text=text,
        confidence=0.8,
        status=status,
        created_at=now_local(),
    )


def test_insert_todo_duplicate_is_ignored_and_status_preserved(tmp_path) -> None:
    storage, _account_id, message_id = _storage_with_message(tmp_path)
    first = _todo(message_id, "提交周报", todo_id="todo-1")
    assert storage.insert_todo(first) is True
    assert storage.insert_todo(_todo(message_id, " 提交周报 ", todo_id="todo-2")) is False
    assert storage.insert_todo(_todo(message_id, "提交周报\n", todo_id="todo-3")) is False
    assert len(storage.list_todos()) == 1

    # 用户完成后再次采集同一待办：不新增、不覆盖为 open
    storage.update_todo_status("todo-1", TodoStatus.DONE)
    assert storage.insert_todo(_todo(message_id, "提交周报", todo_id="todo-4")) is False
    todos = storage.list_todos(include_done=True)
    assert len(todos) == 1
    assert todos[0].id == "todo-1"
    assert todos[0].status is TodoStatus.DONE


def test_insert_todos_keeps_different_texts_and_dedupes_batch(tmp_path) -> None:
    storage, _account_id, message_id = _storage_with_message(tmp_path)
    inserted = storage.insert_todos(
        [
            _todo(message_id, "提交周报", todo_id="todo-a"),
            _todo(message_id, "提交周报 ", todo_id="todo-b"),  # 同批重复
            _todo(message_id, "回复客户", todo_id="todo-c"),
        ]
    )
    assert inserted == 2
    assert {todo.text for todo in storage.list_todos()} == {"提交周报", "回复客户"}


def test_normalize_todo_text_rules() -> None:
    assert normalize_todo_text("  a \n\t b  ") == "a b"
    assert normalize_todo_text(None) == ""
    assert normalize_todo_text("提交周报") == "提交周报"


# ---------------------------------------------------------------------------
# pipeline：重复扫描不新增 / extractor 同文本只留一条
# ---------------------------------------------------------------------------

def _pipeline_storage(tmp_path) -> tuple[Storage, str, str, str]:
    storage = Storage(tmp_path / "db.sqlite")
    storage.initialize()
    account = make_account(kind=AccountKind.EMAIL)
    conversation = make_conversation(
        "conv-pipe", account_id=account.id, title="INBOX", external_key="INBOX"
    )
    storage.upsert_account(account)
    storage.upsert_conversation(conversation)
    return storage, account.id, conversation.id, "INBOX"


def test_pipeline_duplicate_scan_does_not_duplicate_todos(tmp_path) -> None:
    storage, account_id, conversation_id, external_key = _pipeline_storage(tmp_path)
    conversation = storage.get_conversation(conversation_id)
    assert conversation is not None
    extractor = FakeTodoExtractor(
        {
            "请明天提交周报": [
                ExtractedTodo(text="提交周报", due_text="明天", confidence=0.8),
                ExtractedTodo(text=" 提交周报 ", due_text="明天", confidence=0.8),
                ExtractedTodo(text="回复客户", due_text=None, confidence=0.6),
            ]
        }
    )
    raw = make_raw(
        external_key,
        "请明天提交周报",
        timestamp=now_local(),
        external_id="imap:1:1",
        source_locator="IMAP INBOX UID 1",
    )
    source = FakeMessageSource(
        kind=AccountKind.EMAIL,
        conversations=[conversation],
        messages={external_key: [raw]},
    )
    account = storage.get_account(account_id)
    assert account is not None
    pipeline = ScanPipeline(
        storage,
        source_factory=lambda item: source,
        todo_extractor=extractor,
    )

    first = pipeline.run([account_id])
    assert first[0].status is ScanStatus.OK
    todos = storage.list_todos()
    assert len(todos) == 2  # 同文本只留一条 + 不同文本保留
    assert {todo.text for todo in todos} == {"提交周报", "回复客户"}

    second = pipeline.run([account_id])
    assert second[0].messages_new == 0
    assert len(storage.list_todos()) == 2  # 重复扫描不新增
