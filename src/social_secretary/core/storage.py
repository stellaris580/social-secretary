"""SQLite 存储层：schema/迁移、仓储方法、事务、WAL、数据删除。

设计要点
--------
- 一个数据库文件（``<data_dir>/data/secretary.db``），WAL 模式，外键开启。
- 每次操作使用独立连接（短连接），写操作显式 ``BEGIN IMMEDIATE`` 事务；
  这样 UI 线程 / 后台扫描线程可以安全共存，避免长连接跨线程问题。
- 所有时间戳以 ``ISO8601（带时区）`` 字符串落库；另外保存 ``sort_ts``（unix 秒）
  用于稳定排序，避免不同时区偏移的 ISO 字符串字典序不等于时间序。
- 每张表有 ``local_day``（本地时区的日期）列，摘要按「用户本地日期」聚合，
  不受 UTC 偏移影响。
- 消息正文只存本地数据库，不进日志；审计 detail 写入前再次脱敏。

本模块只依赖标准库与 contracts/security，不依赖采集器、intel、UI。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from ..contracts import (
    Account,
    AccountKind,
    Category,
    CollectionResult,
    Conversation,
    ConversationKind,
    DraftKind,
    Message,
    MessageWorkStatus,
    Priority,
    ReplyDraft,
    ScanStatus,
    TimestampSource,
    TodoItem,
    TodoStatus,
)
from ..security.audit import sanitize_detail

SCHEMA_VERSION = 5

#: 演示/合成数据的账号 id 前缀（is_demo 由账号 id 判定，重启后仍可回填）。
DEMO_ACCOUNT_ID_PREFIX = "demo-"


def is_demo_account_id(account_id: str) -> bool:
    """账号 id 是否属于演示/合成账号（真实数据必须显示 is_demo=False）。"""
    return str(account_id or "").startswith(DEMO_ACCOUNT_ID_PREFIX)


def normalize_todo_text(text: Any) -> str:
    """待办判重用的文本规范化：去除首尾空白并把连续空白折叠为单个空格。

    仅用于判重/落库；不改变语义，避免同一消息因换行/重复空格生成重复待办。
    """
    return " ".join(str(text or "").split())

# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    """当前本地时间（带时区）。全项目统一使用这个函数取「现在」。"""
    return datetime.now().astimezone()


def _aware(value: datetime) -> datetime:
    """把可能不带时区的 datetime 视为本地时间。"""
    return value if value.tzinfo is not None else value.astimezone()


def to_iso(value: datetime | None) -> str | None:
    """序列化为带时区的 ISO8601 字符串（保留微秒，保证读写往返完全一致）。"""
    if value is None:
        return None
    return _aware(value).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def local_day_of(value: datetime) -> str:
    return _aware(value).date().isoformat()


def _ts_of(value: datetime) -> float:
    return _aware(value).timestamp()


def _loads_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return list(data) if isinstance(data, list) else []


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# schema / 迁移
# ---------------------------------------------------------------------------

# 每个版本一个 SQL 语句元组；从旧版本升级时按顺序执行，并在 schema_meta 记录版本。
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            display_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            identifier TEXT NOT NULL DEFAULT '',
            deleted_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            external_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority TEXT NOT NULL DEFAULT 'normal',
            last_scanned_at TEXT,
            last_message_at TEXT,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE (account_id, external_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_conversations_account ON conversations(account_id)",
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            conversation_title TEXT NOT NULL,
            conversation_kind TEXT NOT NULL,
            sender TEXT NOT NULL,
            is_self INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL,
            timestamp TEXT,
            timestamp_source TEXT NOT NULL DEFAULT 'collected',
            collected_at TEXT NOT NULL,
            sort_ts REAL NOT NULL DEFAULT 0,
            local_day TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'normal',
            category_reasons TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL,
            source_locator TEXT,
            external_id TEXT,
            raw_time_text TEXT
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_content_hash ON messages(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(local_day, category)",
        "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, sort_ts)",
        "CREATE INDEX IF NOT EXISTS idx_messages_external ON messages(conversation_id, external_id)",
        """
        CREATE TABLE IF NOT EXISTS scan_runs (
            scan_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            messages_seen INTEGER NOT NULL DEFAULT 0,
            messages_new INTEGER NOT NULL DEFAULT 0,
            conversations_scanned INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            warnings TEXT NOT NULL DEFAULT '[]',
            started_at TEXT,
            finished_at TEXT,
            local_day TEXT NOT NULL,
            foreground_used INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            trigger TEXT NOT NULL DEFAULT 'manual'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scan_runs_account ON scan_runs(account_id, local_day)",
        """
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL,
            conversation_title TEXT NOT NULL,
            text TEXT NOT NULL,
            due_at TEXT,
            due_text TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT,
            local_day TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_todos_day ON todos(local_day, status)",
        "CREATE INDEX IF NOT EXISTS idx_todos_message ON todos(message_id)",
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            style TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT,
            model_label TEXT NOT NULL DEFAULT '',
            edited_text TEXT,
            requires_external_consent INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_drafts_message ON drafts(message_id)",
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS consent_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            granted INTEGER NOT NULL,
            at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_consent_scope ON consent_log(scope, id)",
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            action TEXT NOT NULL,
            target_kind TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at)",
        """
        CREATE TABLE IF NOT EXISTS summaries (
            day TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            is_demo INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL
        )
        """,
    ),
    # v2：邮箱收件箱需要保留邮件主题（正文仍在 text 中；旧库升级后 subject 默认为空）。
    2: (
        "ALTER TABLE messages ADD COLUMN subject TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_messages_account_kind ON messages(account_id, conversation_kind, sort_ts)",
    ),
    # v3：邮件附件文件名（仅名称；不下载/不保存附件内容；JSON 数组字符串）。
    3: (
        "ALTER TABLE messages ADD COLUMN attachment_names_json TEXT NOT NULL DEFAULT '[]'",
    ),
    # v4：用户对消息的处理状态（本地手动维护；旧数据/新消息默认 unhandled）。
    4: (
        "ALTER TABLE messages ADD COLUMN work_status TEXT NOT NULL DEFAULT 'unhandled'",
        "CREATE INDEX IF NOT EXISTS idx_messages_work_status ON messages(work_status)",
    ),
    # v5：同一消息的同一待办不重复创建（先由 Python hook 去重旧数据，再建唯一索引）。
    5: (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_message_text ON todos(message_id, text)",
    ),
}


def _dedupe_todos_conn(conn: sqlite3.Connection) -> None:
    """v5 迁移：按 (message_id, 规范化 text) 合并旧重复待办。

    - 保留最早一行（rowid 最小）的字段；
    - 若重复行中存在 DONE/DISMISSED，把该状态保留到保留行（DONE 优先于 DISMISSED）；
    - 保留行文本更新为规范化文本，其余重复行删除；
    - 之后由 v5 SQL 创建 UNIQUE(message_id, text) 索引。
    """
    rows = conn.execute(
        "SELECT rowid, id, message_id, text, status FROM todos ORDER BY rowid ASC"
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        normalized = normalize_todo_text(row["text"])
        groups.setdefault((str(row["message_id"]), normalized), []).append(row)
    for (_message_id, normalized), items in groups.items():
        if not items:
            continue
        keep = items[0]
        statuses = {str(item["status"]) for item in items}
        if TodoStatus.DONE.value in statuses:
            status = TodoStatus.DONE.value
        elif TodoStatus.DISMISSED.value in statuses:
            status = TodoStatus.DISMISSED.value
        else:
            status = str(keep["status"])
        conn.execute(
            "UPDATE todos SET text = ?, status = ? WHERE id = ?",
            (normalized, status, keep["id"]),
        )
        for duplicate in items[1:]:
            conn.execute("DELETE FROM todos WHERE id = ?", (duplicate["id"],))


#: 需要 Python 逻辑的迁移（在对应版本 SQL 之前、同一事务内执行）。
_PYTHON_MIGRATIONS: dict[int, Any] = {
    5: _dedupe_todos_conn,
}


# ---------------------------------------------------------------------------
# 行 -> 数据对象
# ---------------------------------------------------------------------------

def _account_from_row(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        kind=AccountKind(row["kind"]),
        display_name=row["display_name"],
        enabled=bool(row["enabled"]),
        created_at=from_iso(row["created_at"]),
        identifier=row["identifier"] or "",
    )


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        account_id=row["account_id"],
        kind=ConversationKind(row["kind"]),
        title=row["title"],
        external_key=row["external_key"],
        enabled=bool(row["enabled"]),
        priority=Priority(row["priority"]),
        last_scanned_at=from_iso(row["last_scanned_at"]),
        last_message_at=from_iso(row["last_message_at"]),
        note=row["note"] or "",
    )


def _work_status(value: Any) -> MessageWorkStatus:
    """容错解析处理状态；损坏/未知值一律回退 unhandled（旧库兼容）。"""
    try:
        return MessageWorkStatus(str(value or MessageWorkStatus.UNHANDLED.value))
    except ValueError:
        return MessageWorkStatus.UNHANDLED


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        account_id=row["account_id"],
        conversation_id=row["conversation_id"],
        conversation_title=row["conversation_title"],
        conversation_kind=ConversationKind(row["conversation_kind"]),
        sender=row["sender"],
        is_self=bool(row["is_self"]),
        text=row["text"],
        timestamp=from_iso(row["timestamp"]),
        timestamp_source=TimestampSource(row["timestamp_source"]),
        collected_at=from_iso(row["collected_at"]) or now_local(),
        category=Category(row["category"]),
        category_reasons=tuple(str(item) for item in _loads_list(row["category_reasons"])),
        work_status=_work_status(row["work_status"]),
        content_hash=row["content_hash"],
        source_locator=row["source_locator"],
        external_id=row["external_id"],
        subject=str(row["subject"] or ""),
        is_demo=is_demo_account_id(row["account_id"]),
        attachment_names=tuple(
            str(item)
            for item in _loads_list(row["attachment_names_json"])
            if isinstance(item, str)
        ),
    )


def _todo_from_row(row: sqlite3.Row) -> TodoItem:
    return TodoItem(
        id=row["id"],
        message_id=row["message_id"],
        conversation_id=row["conversation_id"],
        conversation_title=row["conversation_title"],
        text=row["text"],
        due_at=from_iso(row["due_at"]),
        due_text=row["due_text"],
        confidence=float(row["confidence"]),
        status=TodoStatus(row["status"]),
        created_at=from_iso(row["created_at"]),
    )


def _draft_from_row(row: sqlite3.Row) -> ReplyDraft:
    return ReplyDraft(
        id=row["id"],
        message_id=row["message_id"],
        kind=DraftKind(row["kind"]),
        style=row["style"],
        text=row["text"],
        created_at=from_iso(row["created_at"]),
        model_label=row["model_label"] or "",
        edited_text=row["edited_text"],
        requires_external_consent=bool(row["requires_external_consent"]),
    )


def _scan_run_from_row(row: sqlite3.Row) -> CollectionResult:
    return CollectionResult(
        scan_id=row["scan_id"],
        account_id=row["account_id"],
        status=ScanStatus(row["status"]),
        messages_seen=int(row["messages_seen"]),
        messages_new=int(row["messages_new"]),
        conversations_scanned=int(row["conversations_scanned"]),
        error=row["error"],
        warnings=tuple(str(item) for item in _loads_list(row["warnings"])),
        started_at=from_iso(row["started_at"]),
        finished_at=from_iso(row["finished_at"]),
        foreground_used=bool(row["foreground_used"]),
        duration_ms=int(row["duration_ms"]),
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Storage:
    """SQLite 仓储。构造后需调用 :meth:`initialize`（幂等）。"""

    def __init__(self, db_path: Path | str, *, timeout: float = 30.0) -> None:
        self._db_path = Path(db_path)
        self._timeout = float(timeout)

    # -- 基础 ---------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self._db_path), timeout=self._timeout, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")

    def initialize(self) -> None:
        """创建目录与表结构，并升级到 :data:`SCHEMA_VERSION`（幂等）。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta ("
                "version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
            )
            row = conn.execute("SELECT MAX(version) AS version FROM schema_meta").fetchone()
            current = int(row["version"]) if row and row["version"] is not None else 0
            for version in range(current + 1, SCHEMA_VERSION + 1):
                statements = MIGRATIONS.get(version)
                if not statements:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    hook = _PYTHON_MIGRATIONS.get(version)
                    if hook is not None:
                        hook(conn)
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                        (version, to_iso(now_local())),
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise

    @property
    def schema_version(self) -> int:
        with self._connect() as conn:
            try:
                row = conn.execute("SELECT MAX(version) AS v FROM schema_meta").fetchone()
            except sqlite3.OperationalError:
                return 0
            return int(row["v"]) if row and row["v"] is not None else 0

    @property
    def journal_mode(self) -> str:
        with self._connect() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]) if row else ""

    # -- 账号 ---------------------------------------------------------------

    def upsert_account(self, account: Account) -> None:
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO accounts(id, kind, display_name, enabled, created_at, identifier)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    display_name = excluded.display_name,
                    enabled = excluded.enabled,
                    identifier = excluded.identifier
                """,
                (
                    account.id,
                    account.kind.value,
                    account.display_name,
                    int(account.enabled),
                    to_iso(account.created_at),
                    account.identifier or "",
                ),
            )

    def get_account(self, account_id: str, *, include_deleted: bool = False) -> Account | None:
        sql = "SELECT * FROM accounts WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self._connect() as conn:
            row = conn.execute(sql, (account_id,)).fetchone()
        return _account_from_row(row) if row else None

    def list_accounts(self, *, include_disabled: bool = True) -> list[Account]:
        sql = "SELECT * FROM accounts WHERE deleted_at IS NULL"
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at, id"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_account_from_row(row) for row in rows]

    def find_account_by_identifier(
        self, kind: AccountKind, identifier: str
    ) -> Account | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE kind = ? AND lower(identifier) = lower(?)"
                " AND deleted_at IS NULL",
                (kind.value, identifier),
            ).fetchone()
        return _account_from_row(row) if row else None

    def set_account_enabled(self, account_id: str, enabled: bool) -> bool:
        with self._write() as conn:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE id = ? AND deleted_at IS NULL", (account_id,)
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE accounts SET enabled = ? WHERE id = ?", (int(enabled), account_id)
            )
            return True

    def delete_account(self, account_id: str, *, delete_messages: bool = True) -> bool:
        """删除账号。

        ``delete_messages=True``：级联删除会话/消息/待办/草稿/扫描记录。
        ``delete_messages=False``：软删除账号（隐藏），保留历史消息供用户查看。
        """
        with self._write() as conn:
            exists = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not exists:
                return False
            if delete_messages:
                conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            else:
                conn.execute(
                    "UPDATE accounts SET deleted_at = ?, enabled = 0 WHERE id = ?",
                    (to_iso(now_local()), account_id),
                )
                conn.execute(
                    "UPDATE conversations SET enabled = 0 WHERE account_id = ?", (account_id,)
                )
            return True

    def count_accounts(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE deleted_at IS NULL"
            ).fetchone()
        return int(row["n"]) if row else 0

    # -- 会话 ---------------------------------------------------------------

    def upsert_conversation(self, conversation: Conversation) -> None:
        """写入会话。已存在时只更新展示信息，**保留用户设置的 enabled/priority**。"""
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO conversations(
                    id, account_id, kind, title, external_key, enabled, priority,
                    last_scanned_at, last_message_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    external_key = excluded.external_key,
                    note = excluded.note
                """,
                (
                    conversation.id,
                    conversation.account_id,
                    conversation.kind.value,
                    conversation.title,
                    conversation.external_key,
                    int(conversation.enabled),
                    conversation.priority.value,
                    to_iso(conversation.last_scanned_at),
                    to_iso(conversation.last_message_at),
                    conversation.note or "",
                ),
            )

    def upsert_conversations(self, conversations: Sequence[Conversation]) -> None:
        for conversation in conversations:
            self.upsert_conversation(conversation)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return _conversation_from_row(row) if row else None

    def find_conversation_by_external_key(
        self, account_id: str, external_key: str
    ) -> Conversation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE account_id = ? AND external_key = ?",
                (account_id, external_key),
            ).fetchone()
        return _conversation_from_row(row) if row else None

    def list_conversations(
        self, account_id: str | None = None, *, include_disabled: bool = True
    ) -> list[Conversation]:
        sql = (
            "SELECT c.* FROM conversations c"
            " JOIN accounts a ON a.id = c.account_id"
            " WHERE a.deleted_at IS NULL"
        )
        params: list[Any] = []
        if account_id is not None:
            sql += " AND c.account_id = ?"
            params.append(account_id)
        if not include_disabled:
            sql += " AND c.enabled = 1"
        sql += (
            " ORDER BY (c.last_message_at IS NULL), c.last_message_at DESC,"
            " c.title, c.id"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def update_conversation(
        self,
        conversation_id: str,
        *,
        enabled: bool | None = None,
        priority: Priority | None = None,
        last_scanned_at: datetime | None = None,
        last_message_at: datetime | None = None,
        note: str | None = None,
        touch_scanned: bool = False,
    ) -> bool:
        with self._write() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not exists:
                return False
            assignments: list[str] = []
            params: list[Any] = []
            if enabled is not None:
                assignments.append("enabled = ?")
                params.append(int(enabled))
            if priority is not None:
                assignments.append("priority = ?")
                params.append(priority.value)
            if last_scanned_at is not None or touch_scanned:
                assignments.append("last_scanned_at = ?")
                params.append(to_iso(last_scanned_at or now_local()))
            if last_message_at is not None:
                assignments.append("last_message_at = ?")
                params.append(to_iso(last_message_at))
            if note is not None:
                assignments.append("note = ?")
                params.append(note)
            if not assignments:
                return True
            params.append(conversation_id)
            conn.execute(
                f"UPDATE conversations SET {', '.join(assignments)} WHERE id = ?", params
            )
            return True

    def count_conversations(self, *, enabled_only: bool = False) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM conversations c"
            " JOIN accounts a ON a.id = c.account_id WHERE a.deleted_at IS NULL"
        )
        if enabled_only:
            sql += " AND c.enabled = 1"
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
        return int(row["n"]) if row else 0

    # -- 消息 ---------------------------------------------------------------

    @staticmethod
    def _message_params(message: Message, raw_time_text: str | None) -> tuple[Any, ...]:
        sort_source = message.timestamp or message.collected_at
        return (
            message.id,
            message.account_id,
            message.conversation_id,
            message.conversation_title,
            message.conversation_kind.value,
            message.sender,
            int(message.is_self),
            message.text,
            to_iso(message.timestamp),
            message.timestamp_source.value,
            to_iso(message.collected_at),
            _ts_of(sort_source),
            local_day_of(sort_source),
            message.category.value,
            _dumps(list(message.category_reasons)),
            message.work_status.value,
            message.content_hash,
            message.source_locator,
            message.external_id,
            message.subject or "",
            _dumps(list(message.attachment_names)),
            raw_time_text,
        )

    _INSERT_MESSAGE_SQL = """
        INSERT INTO messages(
            id, account_id, conversation_id, conversation_title, conversation_kind,
            sender, is_self, text, timestamp, timestamp_source, collected_at,
            sort_ts, local_day, category, category_reasons, work_status, content_hash,
            source_locator, external_id, subject, attachment_names_json, raw_time_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def _insert_message_conn(
        self,
        conn: sqlite3.Connection,
        message: Message,
        raw_time_text: str | None = None,
    ) -> bool:
        if conn.execute(
            "SELECT 1 FROM messages WHERE content_hash = ?", (message.content_hash,)
        ).fetchone():
            return False
        try:
            conn.execute(self._INSERT_MESSAGE_SQL, self._message_params(message, raw_time_text))
        except sqlite3.IntegrityError:
            return False
        # 更新会话最近消息时间
        row = conn.execute(
            "SELECT last_message_at FROM conversations WHERE id = ?",
            (message.conversation_id,),
        ).fetchone()
        if row is not None:
            current = from_iso(row["last_message_at"])
            candidate = _aware(message.timestamp or message.collected_at)
            if current is None or _aware(candidate) > _aware(current):
                conn.execute(
                    "UPDATE conversations SET last_message_at = ? WHERE id = ?",
                    (to_iso(candidate), message.conversation_id),
                )
        return True

    def insert_message(self, message: Message, *, raw_time_text: str | None = None) -> bool:
        """插入消息；内容哈希重复（跨批次幂等）时返回 False。"""
        with self._write() as conn:
            return self._insert_message_conn(conn, message, raw_time_text)

    def insert_messages(
        self,
        messages: Sequence[Message],
        *,
        raw_time_texts: Mapping[str, str] | None = None,
    ) -> int:
        texts = raw_time_texts or {}
        inserted = 0
        with self._write() as conn:
            for message in messages:
                if self._insert_message_conn(conn, message, texts.get(message.id)):
                    inserted += 1
        return inserted

    def message_exists(self, content_hash: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM messages WHERE content_hash = ?", (content_hash,)
                ).fetchone()
                is not None
            )

    def find_message_by_external_id(
        self, conversation_id: str, external_id: str
    ) -> Message | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND external_id = ?"
                " LIMIT 1",
                (conversation_id, external_id),
            ).fetchone()
        return _message_from_row(row) if row else None

    def get_message(self, message_id: str) -> Message | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message_from_row(row) if row else None

    def get_message_row(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def list_messages(
        self,
        day: date | None = None,
        category: Category | None = None,
        *,
        limit: int = 200,
        include_self: bool = True,
        conversation_id: str | None = None,
        work_status: MessageWorkStatus | None = None,
    ) -> list[Message]:
        sql = ["SELECT * FROM messages WHERE 1 = 1"]
        params: list[Any] = []
        if day is not None:
            sql.append("AND local_day = ?")
            params.append(day.isoformat() if isinstance(day, date) else str(day))
        if category is not None:
            sql.append("AND category = ?")
            params.append(category.value)
        if work_status is not None:
            sql.append("AND work_status = ?")
            params.append(
                work_status.value
                if isinstance(work_status, MessageWorkStatus)
                else str(work_status)
            )
        if not include_self:
            sql.append("AND is_self = 0")
        if conversation_id is not None:
            sql.append("AND conversation_id = ?")
            params.append(conversation_id)
        sql.append("ORDER BY sort_ts DESC, rowid DESC LIMIT ?")
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [_message_from_row(row) for row in rows]

    def count_messages(
        self,
        day: date | None = None,
        *,
        category: Category | None = None,
        work_status: MessageWorkStatus | None = None,
    ) -> int:
        sql = ["SELECT COUNT(*) AS n FROM messages WHERE 1 = 1"]
        params: list[Any] = []
        if day is not None:
            sql.append("AND local_day = ?")
            params.append(day.isoformat())
        if category is not None:
            sql.append("AND category = ?")
            params.append(category.value)
        if work_status is not None:
            sql.append("AND work_status = ?")
            params.append(
                work_status.value
                if isinstance(work_status, MessageWorkStatus)
                else str(work_status)
            )
        with self._connect() as conn:
            row = conn.execute(" ".join(sql), params).fetchone()
        return int(row["n"]) if row else 0

    def count_messages_by_work_status(self) -> dict[str, int]:
        """全库消息按处理状态计数；始终返回四种状态（无数据为 0）。"""
        counts = {status.value: 0 for status in MessageWorkStatus}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT work_status, COUNT(*) AS n FROM messages GROUP BY work_status"
            ).fetchall()
        for row in rows:
            key = str(row["work_status"] or MessageWorkStatus.UNHANDLED.value)
            if key in counts:
                counts[key] = int(row["n"])
            else:
                # 未知状态（手工改库/未来版本）归入 unhandled，避免计数丢失
                counts[MessageWorkStatus.UNHANDLED.value] += int(row["n"])
        return counts

    def update_message_work_status(
        self, message_id: str, status: MessageWorkStatus
    ) -> bool:
        """更新单条消息处理状态；消息不存在返回 False。"""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE messages SET work_status = ? WHERE id = ?",
                (status.value, message_id),
            )
            return cursor.rowcount > 0

    def update_message_category(self, message_id: str, category: Category) -> bool:
        """更新单条消息的本地分类（用户手动调整）；消息不存在返回 False。"""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE messages SET category = ? WHERE id = ?",
                (category.value, message_id),
            )
            return cursor.rowcount > 0

    def distinct_conversations_touched(self, day: date, *, include_self: bool = False) -> int:
        sql = "SELECT COUNT(DISTINCT conversation_id) AS n FROM messages WHERE local_day = ?"
        params: list[Any] = [day.isoformat()]
        if not include_self:
            sql += " AND is_self = 0"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    # -- 邮箱收件箱查询（按账号/会话类型；不按日期过滤，历史邮件可见）----------------

    @staticmethod
    def _account_scope_sql(
        account_ids: Sequence[str],
        kinds: Sequence[ConversationKind] | None,
    ) -> tuple[str, list[Any]]:
        ids = [str(item) for item in account_ids if str(item)]
        if not ids:
            return "", []
        placeholders = ", ".join("?" for _ in ids)
        sql = f" AND account_id IN ({placeholders})"
        params: list[Any] = list(ids)
        if kinds:
            kind_values = [
                kind.value if isinstance(kind, ConversationKind) else str(kind)
                for kind in kinds
            ]
            kind_placeholders = ", ".join("?" for _ in kind_values)
            sql += f" AND conversation_kind IN ({kind_placeholders})"
            params.extend(kind_values)
        return sql, params

    def list_messages_for_accounts(
        self,
        account_ids: Sequence[str],
        *,
        limit: int = 200,
        kinds: Sequence[ConversationKind] | None = None,
    ) -> list[Message]:
        """按账号集合列出消息（跨日期），按 timestamp / collected_at 倒序。

        用于邮箱收件箱：真实邮件多为历史日期，不能按“今天”过滤。
        """
        scope_sql, params = self._account_scope_sql(account_ids, kinds)
        if not scope_sql:
            return []
        sql = (
            "SELECT * FROM messages WHERE 1 = 1"
            + scope_sql
            + " ORDER BY sort_ts DESC, rowid DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_message_from_row(row) for row in rows]

    def count_messages_for_accounts(
        self,
        account_ids: Sequence[str],
        *,
        kinds: Sequence[ConversationKind] | None = None,
    ) -> int:
        scope_sql, params = self._account_scope_sql(account_ids, kinds)
        if not scope_sql:
            return 0
        sql = "SELECT COUNT(*) AS n FROM messages WHERE 1 = 1" + scope_sql
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def latest_message_day(
        self,
        account_ids: Sequence[str],
        *,
        kinds: Sequence[ConversationKind] | None = None,
    ) -> date | None:
        """账号集合中最新一条消息所在的本地日期（没有消息返回 None）。"""
        scope_sql, params = self._account_scope_sql(account_ids, kinds)
        if not scope_sql:
            return None
        sql = (
            "SELECT local_day FROM messages WHERE 1 = 1"
            + scope_sql
            + " ORDER BY sort_ts DESC, rowid DESC LIMIT 1"
        )
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        try:
            return date.fromisoformat(str(row["local_day"]))
        except ValueError:
            return None

    # -- 扫描记录 -----------------------------------------------------------

    def insert_scan_run(self, result: CollectionResult, *, trigger: str = "manual") -> None:
        started = result.started_at or now_local()
        with self._write() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scan_runs(
                    scan_id, account_id, status, messages_seen, messages_new,
                    conversations_scanned, error, warnings, started_at, finished_at,
                    local_day, foreground_used, duration_ms, trigger
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.scan_id,
                    result.account_id,
                    result.status.value,
                    int(result.messages_seen),
                    int(result.messages_new),
                    int(result.conversations_scanned),
                    result.error,
                    _dumps(list(result.warnings)),
                    to_iso(result.started_at),
                    to_iso(result.finished_at),
                    local_day_of(started),
                    int(result.foreground_used),
                    int(result.duration_ms),
                    str(trigger),
                ),
            )

    def list_scan_runs(
        self, day: date | None = None, account_id: str | None = None, *, limit: int = 50
    ) -> list[CollectionResult]:
        sql = ["SELECT * FROM scan_runs WHERE 1 = 1"]
        params: list[Any] = []
        if day is not None:
            sql.append("AND local_day = ?")
            params.append(day.isoformat())
        if account_id is not None:
            sql.append("AND account_id = ?")
            params.append(account_id)
        sql.append("ORDER BY rowid DESC LIMIT ?")
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [_scan_run_from_row(row) for row in rows]

    def last_scan_run(self, account_id: str | None = None) -> CollectionResult | None:
        runs = self.list_scan_runs(account_id=account_id, limit=1)
        return runs[0] if runs else None

    def enabled_scan_accounts(self) -> list[Account]:
        """参与“采集覆盖”判断的已启用账号（排除 IMPORT 与演示账号）。"""
        return [
            account
            for account in self.list_accounts()
            if account.enabled
            and account.kind is not AccountKind.IMPORT
            and not is_demo_account_id(account.id)
        ]

    def latest_scan_runs_for_day(self, day: date) -> dict[str, CollectionResult]:
        """当天每个账号的**最新一次** scan_run（按 rowid 最新；无 run 的账号不出现）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scan_runs WHERE local_day = ? ORDER BY rowid DESC",
                (day.isoformat(),),
            ).fetchall()
        latest: dict[str, CollectionResult] = {}
        for row in rows:
            run = _scan_run_from_row(row)
            latest.setdefault(run.account_id, run)
        return latest

    def scan_status_for_day(self, day: date) -> ScanStatus:
        """当天“每个已启用账号最新一次 run”的聚合状态。

        - 当天没有任何 scan_run：默认 OK（未开始采集，不制造失败提示）；
        - 有 run 但某个已启用账号当天没有 run：覆盖不完整 → 至少 PARTIAL；
        - 某账号最新 run 失败/部分失败 → 按最坏状态返回（更早的失败不覆盖最新成功）。
        """
        accounts = self.enabled_scan_accounts()
        if not accounts:
            return ScanStatus.OK
        latest = self.latest_scan_runs_for_day(day)
        if not latest:
            return ScanStatus.OK
        statuses = [
            (
                latest[account.id].status
                if account.id in latest
                else ScanStatus.PARTIAL
            )
            for account in accounts
        ]
        for candidate in (
            ScanStatus.FAILED,
            ScanStatus.BLOCKED,
            ScanStatus.PARTIAL,
            ScanStatus.PAUSED,
            ScanStatus.OK,
        ):
            if candidate in statuses:
                return candidate
        return ScanStatus.OK

    def count_scan_runs(self, day: date | None = None) -> int:
        with self._connect() as conn:
            if day is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM scan_runs WHERE local_day = ?",
                    (day.isoformat(),),
                ).fetchone()
        return int(row["n"]) if row else 0

    # -- 待办 ---------------------------------------------------------------

    def insert_todo(self, todo: TodoItem) -> bool:
        """插入单条待办；同消息同规范化文本已存在时返回 False（不覆盖用户状态）。"""
        return self.insert_todos([todo]) > 0

    def insert_todos(self, todos: Sequence[TodoItem]) -> int:
        """批量插入待办；重复 (message_id, 规范化 text) 由唯一索引忽略。

        返回实际新增条数。重复插入不会覆盖已有待办的 status/文本，
        因此用户手动完成/忽略的状态不会被重新采集覆盖。
        """
        if not todos:
            return 0
        created = now_local()
        inserted = 0
        with self._write() as conn:
            for todo in todos:
                text = normalize_todo_text(todo.text)
                if not text:
                    continue
                todo_created = todo.created_at or created
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO todos(
                        id, message_id, conversation_id, conversation_title, text,
                        due_at, due_text, confidence, status, created_at, local_day
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        todo.id,
                        todo.message_id,
                        todo.conversation_id,
                        todo.conversation_title,
                        text,
                        to_iso(todo.due_at),
                        todo.due_text,
                        float(todo.confidence),
                        todo.status.value,
                        to_iso(todo_created),
                        local_day_of(todo_created),
                    ),
                )
                inserted += max(0, int(cursor.rowcount or 0))
        return inserted

    def get_todo(self, todo_id: str) -> TodoItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return _todo_from_row(row) if row else None

    def list_todos(
        self,
        day: date | None = None,
        *,
        include_done: bool = False,
        message_id: str | None = None,
        limit: int = 1000,
    ) -> list[TodoItem]:
        sql = ["SELECT * FROM todos WHERE 1 = 1"]
        params: list[Any] = []
        if not include_done:
            sql.append("AND status = ?")
            params.append(TodoStatus.OPEN.value)
        else:
            sql.append("AND status <> ?")
            params.append(TodoStatus.DISMISSED.value)
        if day is not None:
            sql.append("AND local_day = ?")
            params.append(day.isoformat())
        if message_id is not None:
            sql.append("AND message_id = ?")
            params.append(message_id)
        sql.append("ORDER BY rowid DESC LIMIT ?")
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [_todo_from_row(row) for row in rows]

    def update_todo_status(self, todo_id: str, status: TodoStatus) -> bool:
        with self._write() as conn:
            exists = conn.execute("SELECT 1 FROM todos WHERE id = ?", (todo_id,)).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE todos SET status = ? WHERE id = ?", (status.value, todo_id)
            )
            return True

    def count_open_todos(self, day: date | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM todos WHERE status = ?"
        params: list[Any] = [TodoStatus.OPEN.value]
        if day is not None:
            sql += " AND local_day = ?"
            params.append(day.isoformat())
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    # -- 草稿 ---------------------------------------------------------------

    def insert_draft(self, draft: ReplyDraft) -> None:
        self.insert_drafts([draft])

    def insert_drafts(self, drafts: Sequence[ReplyDraft]) -> int:
        if not drafts:
            return 0
        with self._write() as conn:
            for draft in drafts:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO drafts(
                        id, message_id, kind, style, text, created_at,
                        model_label, edited_text, requires_external_consent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.id,
                        draft.message_id,
                        draft.kind.value,
                        draft.style,
                        draft.text,
                        to_iso(draft.created_at),
                        draft.model_label or "",
                        draft.edited_text,
                        int(draft.requires_external_consent),
                    ),
                )
        return len(drafts)

    def get_draft(self, draft_id: str) -> ReplyDraft | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return _draft_from_row(row) if row else None

    def list_drafts(self, message_id: str) -> list[ReplyDraft]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drafts WHERE message_id = ? ORDER BY rowid", (message_id,)
            ).fetchall()
        return [_draft_from_row(row) for row in rows]

    def update_draft_edit(self, draft_id: str, text: str) -> bool:
        with self._write() as conn:
            exists = conn.execute("SELECT 1 FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not exists:
                return False
            conn.execute("UPDATE drafts SET edited_text = ? WHERE id = ?", (text, draft_id))
            return True

    def count_drafts(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM drafts").fetchone()
        return int(row["n"]) if row else 0

    # -- 设置 KV（内部状态；用户设置本身在 settings.json） --------------------

    def set_setting(self, key: str, value: Any) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (str(key), _dumps(value), to_iso(now_local())),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (str(key),)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def delete_setting(self, key: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (str(key),))

    def list_setting_keys(self, prefix: str = "") -> list[str]:
        """列出内部状态 key（供 IMAP 增量状态等需要按前缀清理的场景使用）。"""
        sql = "SELECT key FROM settings"
        params: tuple[Any, ...] = ()
        if prefix:
            sql += " WHERE key LIKE ?"
            params = (f"{prefix}%",)
        sql += " ORDER BY key"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [str(row["key"]) for row in rows]

    # -- 同意日志 -----------------------------------------------------------

    def record_consent(self, scope: str, granted: bool, note: str = "") -> int:
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO consent_log(scope, granted, at, note) VALUES (?, ?, ?, ?)",
                (str(scope), int(bool(granted)), to_iso(now_local()), note or ""),
            )
            return int(cursor.lastrowid or 0)

    def consent_is_granted(self, scope: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT granted FROM consent_log WHERE scope = ? ORDER BY id DESC LIMIT 1",
                (str(scope),),
            ).fetchone()
        return bool(row["granted"]) if row else False

    def consent_states(self) -> dict[str, bool]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.scope AS scope, c.granted AS granted
                FROM consent_log c
                JOIN (SELECT scope, MAX(id) AS max_id FROM consent_log GROUP BY scope) latest
                  ON latest.scope = c.scope AND latest.max_id = c.id
                """
            ).fetchall()
        return {str(row["scope"]): bool(row["granted"]) for row in rows}

    def consent_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM consent_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [
            {
                "scope": str(row["scope"]),
                "granted": bool(row["granted"]),
                "action": "grant" if bool(row["granted"]) else "revoke",
                "at": row["at"],
                "note": row["note"] or "",
            }
            for row in rows
        ]

    # -- 审计日志 -----------------------------------------------------------

    def append_audit(
        self,
        action: str,
        *,
        target_kind: str = "",
        target_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        safe_detail = sanitize_detail(detail)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO audit_log(at, action, target_kind, target_id, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    to_iso(now_local()),
                    str(action),
                    str(target_kind or ""),
                    str(target_id or ""),
                    _dumps(safe_detail),
                ),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (TypeError, ValueError):
                detail = {}
            events.append(
                {
                    "id": int(row["id"]),
                    "at": row["at"],
                    "action": str(row["action"]),
                    "target_kind": row["target_kind"],
                    "target_id": row["target_id"],
                    "detail": detail if isinstance(detail, dict) else {},
                }
            )
        return events

    # -- 摘要缓存 -----------------------------------------------------------

    def save_summary(
        self,
        day: date,
        generated_at: datetime,
        is_demo: bool,
        payload: Mapping[str, Any],
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO summaries(day, generated_at, is_demo, payload)"
                " VALUES (?, ?, ?, ?)",
                (day.isoformat(), to_iso(generated_at), int(bool(is_demo)), _dumps(payload)),
            )

    def load_summary(self, day: date) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM summaries WHERE day = ?", (day.isoformat(),)
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None
        return {
            "generated_at": from_iso(row["generated_at"]),
            "is_demo": bool(row["is_demo"]),
            "payload": payload if isinstance(payload, dict) else {},
        }

    def clear_summaries(self) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM summaries")

    # -- 统计 / 全量删除 -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in (
                "accounts",
                "conversations",
                "messages",
                "scan_runs",
                "todos",
                "drafts",
                "summaries",
            ):
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                counts[table] = int(row["n"]) if row else 0
            active_row = conn.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE deleted_at IS NULL"
            ).fetchone()
            day_row = conn.execute(
                "SELECT COUNT(DISTINCT local_day) AS n FROM messages"
            ).fetchone()
        try:
            db_size_bytes = self._db_path.stat().st_size
        except OSError:
            db_size_bytes = 0
        return {
            "schema_version": self.schema_version,
            "accounts": counts["accounts"],
            "active_accounts": int(active_row["n"]) if active_row else 0,
            "conversations": counts["conversations"],
            "messages": counts["messages"],
            "scan_runs": counts["scan_runs"],
            "todos": counts["todos"],
            "drafts": counts["drafts"],
            "summaries": counts["summaries"],
            "days_with_messages": int(day_row["n"]) if day_row else 0,
            "db_size_bytes": db_size_bytes,
            "db_path": str(self._db_path),
        }

    def delete_all_data(self) -> None:
        """清空全部本地业务数据（保留表结构）。"""
        with self._write() as conn:
            for table in (
                "drafts",
                "todos",
                "messages",
                "scan_runs",
                "conversations",
                "accounts",
                "settings",
                "consent_log",
                "audit_log",
                "summaries",
            ):
                conn.execute(f"DELETE FROM {table}")
            try:
                conn.execute("DELETE FROM sqlite_sequence")
            except sqlite3.Error:
                pass

    def close(self) -> None:  # 兼容上下文使用；短连接模式下无需释放
        return None


__all__ = [
    "DEMO_ACCOUNT_ID_PREFIX",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "Storage",
    "from_iso",
    "is_demo_account_id",
    "local_day_of",
    "now_local",
    "to_iso",
]
