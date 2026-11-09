"""用户同意（consent）管理。

- 读取 QQ / 邮箱、调用外部 AI 都需要用户在界面上明确授权（ConsentScope）。
- 授权与撤回全部落 ``consent_log``（核心层 Storage 实现），可审计、可回看。
- ``ConsentManager.require`` 在未授权时抛出 ``ConsentRequired``，服务层必须在
  发起任何读取 / 网络请求**之前**调用它。
- 授权时用户输入的确认文本不落原始明文，只记录长度，避免二次隐私风险。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..contracts import ConsentScope

SCOPE_LABELS: dict[ConsentScope, str] = {
    ConsentScope.QQ_READ: "读取本机 QQ 聊天窗口中的可见消息",
    ConsentScope.MAIL_READ: "只读连接 QQ 邮箱并读取邮件",
    ConsentScope.EXTERNAL_AI: "将必要的消息内容发送给外部 AI 服务生成回复草稿",
}


def _ensure_scope(scope: ConsentScope | str) -> ConsentScope:
    if isinstance(scope, ConsentScope):
        return scope
    return ConsentScope(str(scope))


def scope_value(scope: ConsentScope | str) -> str:
    return _ensure_scope(scope).value


def scope_label(scope: ConsentScope | str) -> str:
    try:
        return SCOPE_LABELS.get(_ensure_scope(scope), scope_value(scope))
    except ValueError:  # 未知 scope 字符串
        return str(scope)


class ConsentRequired(PermissionError):
    """未获得用户明确同意时抛出；``user_message`` 可直接展示给用户。"""

    def __init__(self, scope: ConsentScope | str, *, detail: str = "") -> None:
        self.scope = scope
        message = f"需要用户明确同意：{scope_label(scope)}"
        if detail:
            message += f"（{detail}）"
        super().__init__(message)
        self.user_message = message


@runtime_checkable
class ConsentLedger(Protocol):
    """同意记录后端协议（core.storage.Storage 与 InMemoryConsentLedger 均实现）。"""

    def record_consent(self, scope: str, granted: bool, note: str = "") -> int | None: ...

    def consent_is_granted(self, scope: str) -> bool: ...

    def consent_history(self, limit: int = 100) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ConsentRecord:
    scope: ConsentScope
    granted: bool
    at: datetime
    note: str = ""


class InMemoryConsentLedger:
    """仅测试用：不落盘的同意记录后端。"""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def record_consent(self, scope: str, granted: bool, note: str = "") -> int:
        self._rows.append(
            {
                "scope": str(scope),
                "granted": bool(granted),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "note": note,
            }
        )
        return len(self._rows)

    def consent_is_granted(self, scope: str) -> bool:
        for row in reversed(self._rows):
            if row["scope"] == str(scope):
                return bool(row["granted"])
        return False

    def consent_history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = list(reversed(self._rows))[: max(0, int(limit))]
        return [
            {**row, "action": "grant" if row["granted"] else "revoke"} for row in rows
        ]


class ConsentManager:
    """同意门控：读取与外部 AI 的前置条件。"""

    def __init__(self, ledger: ConsentLedger) -> None:
        self._ledger = ledger

    def is_granted(self, scope: ConsentScope | str) -> bool:
        return bool(self._ledger.consent_is_granted(scope_value(scope)))

    def require(self, scope: ConsentScope | str, *, action: str = "") -> None:
        if not self.is_granted(scope):
            raise ConsentRequired(scope, detail=action)

    def grant(self, scope: ConsentScope | str, *, confirmation_text: str = "") -> ConsentRecord:
        actual = _ensure_scope(scope)
        note = f"用户授权：{scope_label(actual)}"
        if confirmation_text:
            # 只记录长度，不保存用户输入的确认文本原文
            note += f"；确认文本长度 {len(confirmation_text)}"
        self._ledger.record_consent(actual.value, True, note)
        return ConsentRecord(
            scope=actual, granted=True, at=datetime.now().astimezone(), note=note
        )

    def revoke(self, scope: ConsentScope | str) -> ConsentRecord:
        actual = _ensure_scope(scope)
        note = f"用户撤回：{scope_label(actual)}"
        self._ledger.record_consent(actual.value, False, note)
        return ConsentRecord(
            scope=actual, granted=False, at=datetime.now().astimezone(), note=note
        )

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._ledger.consent_history(limit))

    def states(self) -> dict[ConsentScope, bool]:
        return {scope: self.is_granted(scope) for scope in ConsentScope}


__all__ = [
    "ConsentLedger",
    "ConsentManager",
    "ConsentRecord",
    "ConsentRequired",
    "InMemoryConsentLedger",
    "SCOPE_LABELS",
    "scope_label",
    "scope_value",
]
