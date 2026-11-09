"""IMAP 增量同步状态：持久化接口（由 app-core 接线）与内存实现（测试/演示用）。

设计要点
--------
1. 增量同步依赖 IMAP 的 ``UIDVALIDITY`` + ``UID``：同一 UIDVALIDITY 下 UID 单调递增，
   因此只需记住 "下一个要读取的 UID"（``next_uid``）。
2. ``UIDVALIDITY`` 变化意味着邮箱被重建 / UID 失效，必须从 1 重新全量同步。
   本模块只存数据；判断与重置逻辑在 ``collector.MailCollector`` 中完成。
3. 存储接口刻意保持最小（get/set），由 app-core 用 SQLite 实现并保证单飞（single-flight）；
   采集器内部再加一把进程内锁，避免同一实例并发同步重复返回消息。
4. 状态只包含邮箱地址级别的 account_key、文件夹名、UID 等元数据，不含正文/凭据。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


def normalize_mailbox(mailbox: str) -> str:
    """标准化文件夹名。INBOX 大小写不敏感；其它文件夹名保持原样（QQ 中文文件夹需 modified-UTF-7）。"""
    name = (mailbox or "INBOX").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    if name.upper() == "INBOX":
        return "INBOX"
    return name


def account_state_key(account_id: str, email: str) -> str:
    """同步状态里标识账号的 key：优先用 app-core 的 account.id（稳定），退回邮箱地址。"""
    account_id = (account_id or "").strip()
    if account_id:
        return f"account:{account_id}"
    return f"email:{(email or '').strip().lower()}"


@dataclass(frozen=True)
class MailSyncState:
    """某个账号 + 某个邮件文件夹的增量同步游标。"""

    account_key: str
    mailbox: str
    #: 上次同步时服务器返回的 UIDVALIDITY；None 表示尚未同步过。
    uidvalidity: int | None = None
    #: 下一次增量同步从哪个 UID 开始（含）。
    next_uid: int = 1
    #: 上次成功同步完成的时间（带时区）。
    last_synced_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.next_uid < 1:
            raise ValueError("next_uid 必须 >= 1")
        if self.uidvalidity is not None and self.uidvalidity < 0:
            raise ValueError("uidvalidity 必须 >= 0")

    @property
    def last_uid(self) -> int | None:
        """已同步到的最大 UID（None 表示还没有同步过任何邮件）。"""
        return self.next_uid - 1 if self.next_uid > 1 else None

    def to_dict(self) -> dict[str, object]:
        """app-core 落库/JSON 化用（时间用 ISO8601 带时区字符串，符合团队约定）。"""
        return {
            "account_key": self.account_key,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "next_uid": self.next_uid,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "MailSyncState":
        raw_time = data.get("last_synced_at")
        synced_at: datetime | None = None
        if raw_time:
            synced_at = datetime.fromisoformat(str(raw_time))
        uidvalidity = data.get("uidvalidity")
        return cls(
            account_key=str(data.get("account_key", "")),
            mailbox=normalize_mailbox(str(data.get("mailbox", "INBOX"))),
            uidvalidity=int(uidvalidity) if uidvalidity is not None else None,
            next_uid=int(data.get("next_uid", 1)),
            last_synced_at=synced_at,
        )


@runtime_checkable
class SyncStateStore(Protocol):
    """增量同步状态的持久化接口（app-core 用 SQLite 实现；测试用内存 fake）。

    实现方要求：
    - ``get`` 不存在时返回 ``None``（不要抛异常）；
    - ``set`` 覆盖同一 (account_key, mailbox) 的旧记录，需线程安全/事务安全；
    - 不得把邮件正文或凭据写进状态表。
    """

    def get(self, account_key: str, mailbox: str) -> MailSyncState | None: ...

    def set(self, state: MailSyncState) -> None: ...


class MemorySyncStateStore:
    """线程安全的内存实现：测试、演示、以及 app-core 接线前的占位实现。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], MailSyncState] = {}

    def get(self, account_key: str, mailbox: str) -> MailSyncState | None:
        key = (account_key, normalize_mailbox(mailbox))
        with self._lock:
            return self._states.get(key)

    def set(self, state: MailSyncState) -> None:
        key = (state.account_key, normalize_mailbox(state.mailbox))
        with self._lock:
            self._states[key] = state

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def snapshot(self) -> dict[tuple[str, str], MailSyncState]:
        with self._lock:
            return dict(self._states)


__all__ = [
    "MailSyncState",
    "SyncStateStore",
    "MemorySyncStateStore",
    "normalize_mailbox",
    "account_state_key",
]
