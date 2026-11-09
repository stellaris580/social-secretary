"""邮件 IMAP 增量同步状态（UIDVALIDITY + next_uid）的 SQLite 持久化。

实现 ``social_secretary.collectors.mail.state.SyncStateStore`` 协议：
- ``get(account_key, mailbox)``：不存在返回 ``None``（不抛异常）；
- ``set(state)``：覆盖同一 (account_key, mailbox) 的旧记录。

只存 IMAP 游标元数据（账号 key、文件夹、UID、时间），**不含邮件正文、不含凭据**。
底层复用 ``Storage.settings`` 表（JSON 值），因此与核心层事务/备份/全量删除天然一致。
"""

from __future__ import annotations

from typing import Any

from .storage import Storage, to_iso

_SETTING_PREFIX = "mail_sync_state"


class StorageSyncStateStore:
    """基于 SQLite settings 表的增量同步状态存储（app-core 内置接线）。"""

    def __init__(self, storage: Storage, *, prefix: str = _SETTING_PREFIX) -> None:
        self._storage = storage
        self._prefix = prefix

    def _key(self, account_key: str, mailbox: str) -> str:
        return f"{self._prefix}:{account_key}:{mailbox}"

    def get(self, account_key: str, mailbox: str) -> Any | None:
        mailbox_name = _normalize_mailbox(mailbox)
        raw = self._storage.get_setting(self._key(str(account_key), mailbox_name), None)
        if not isinstance(raw, dict):
            return None
        state_cls = _mail_sync_state_class()
        if state_cls is None:
            return None
        try:
            return state_cls.from_dict(raw)
        except (TypeError, ValueError, KeyError):
            return None

    def set(self, state: Any) -> None:
        data = _state_to_dict(state)
        if data is None:
            return  # 不符合协议的对象直接忽略（fail closed，不写脏数据）
        account_key = str(data.get("account_key") or "").strip()
        mailbox = _normalize_mailbox(str(data.get("mailbox") or "INBOX"))
        if not account_key:
            return
        data["account_key"] = account_key
        data["mailbox"] = mailbox
        if data.get("last_synced_at") is None:
            data.pop("last_synced_at", None)
        self._storage.set_setting(self._key(account_key, mailbox), data)

    def clear(self, account_key: str | None = None) -> None:
        """删除同步状态（不带 account_key 时清空全部；用于用户删除本地数据/重新配置账号）。"""
        if account_key is None:
            for row_key in self._all_keys():
                self._storage.delete_setting(row_key)
            return
        prefix = f"{self._prefix}:{account_key}:"
        for row_key in self._all_keys():
            if row_key.startswith(prefix):
                self._storage.delete_setting(row_key)

    def _all_keys(self) -> list[str]:
        return self._storage.list_setting_keys(f"{self._prefix}:")


def _normalize_mailbox(mailbox: str) -> str:
    try:
        from ..collectors.mail.state import normalize_mailbox

        return normalize_mailbox(mailbox)
    except Exception:  # noqa: BLE001 - 采集器未就绪时退化为简单大写
        name = (mailbox or "INBOX").strip()
        return "INBOX" if name.upper() == "INBOX" else name


def _mail_sync_state_class() -> Any | None:
    try:
        from ..collectors.mail.state import MailSyncState

        return MailSyncState
    except Exception:  # noqa: BLE001
        return None


def _state_to_dict(state: Any) -> dict[str, Any] | None:
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # noqa: BLE001
            return None
        return dict(data) if isinstance(data, dict) else None
    # 结构兼容的 duck-typed 对象
    fields = ("account_key", "mailbox", "uidvalidity", "next_uid")
    if not all(hasattr(state, field) for field in fields):
        return None
    synced_at = getattr(state, "last_synced_at", None)
    return {
        "account_key": str(getattr(state, "account_key")),
        "mailbox": str(getattr(state, "mailbox")),
        "uidvalidity": getattr(state, "uidvalidity"),
        "next_uid": int(getattr(state, "next_uid")),
        "last_synced_at": to_iso(synced_at) if synced_at is not None else None,
    }


__all__ = ["StorageSyncStateStore"]
