"""操作审计：只记录「发生了什么」（动作、对象 id、计数、哈希），绝不记录消息正文与凭据。

审计是隐私防线之一：即使调用方误传了消息正文/密钥，``sanitize_detail`` 也会在写入前脱敏。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .log_redaction import REDACTED, redact_text

# 无论值类型如何都必须脱敏的字段（密钥类）
_ALWAYS_SECRET_NAMES = {
    "secret",
    "secrets",
    "password",
    "passwd",
    "pwd",
    "token",
    "tokens",
    "apikey",
    "credential",
    "credentials",
    "cookie",
    "authorization",
    "auth_code",
    "authcode",
    "api_key",
    "api-key",
    "private_key",
}
# 仅当值是字符串时才脱敏的字段（正文类；计数/ID 这类非字符串值可以保留）
_CONTENT_TOKENS = {
    "text",
    "body",
    "content",
    "message",
    "messages",
    "raw",
    "draft",
    "drafts",
    "excerpt",
    "snippet",
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _field_kind(name: str) -> str | None:
    lower = name.strip().lower()
    if lower in _ALWAYS_SECRET_NAMES:
        return "secret"
    tokens = [token for token in _TOKEN_SPLIT.split(lower) if token]
    if any(token in _ALWAYS_SECRET_NAMES for token in tokens):
        return "secret"
    if lower in _CONTENT_TOKENS or any(token in _CONTENT_TOKENS for token in tokens):
        return "content"
    return None


def _sanitize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, max_len=300)
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, Mapping):
        return sanitize_detail(value)
    return redact_text(repr(value), max_len=300)


def sanitize_detail(
    detail: Mapping[str, Any] | None, *, _depth: int = 0
) -> dict[str, Any]:
    """递归脱敏审计/日志的 detail 字典。"""
    if not detail:
        return {}
    if _depth > 2:
        return {"detail": "<嵌套过深，已省略>"}
    out: dict[str, Any] = {}
    for key, value in detail.items():
        name = str(key)
        kind = _field_kind(name)
        if kind == "secret":
            out[name] = REDACTED
        elif kind == "content" and isinstance(value, str):
            out[name] = f"<已脱敏:{len(value)}字符>"
        elif isinstance(value, Mapping):
            out[name] = sanitize_detail(value, _depth=_depth + 1)
        else:
            out[name] = _sanitize_value(value)
    return out


@dataclass(frozen=True)
class AuditEvent:
    action: str
    at: datetime
    target_kind: str = ""
    target_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class AuditLogger:
    """把审计事件写入后端（``core.storage.Storage`` 实现 ``append_audit``）。"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def log(
        self,
        action: str,
        *,
        target_kind: str = "",
        target_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_detail = sanitize_detail(detail)
        self._backend.append_audit(
            action, target_kind=target_kind, target_id=target_id, detail=safe_detail
        )
        return safe_detail


__all__ = ["AuditEvent", "AuditLogger", "sanitize_detail"]
