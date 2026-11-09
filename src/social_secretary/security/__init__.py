"""安全模块：凭据存储、日志脱敏、同意门控、操作审计。

安全底线（见 AGENTS.md）：
- 凭据只进 Windows 凭据管理器；
- 日志/证据不含消息正文、授权码、API Key；
- 读取 QQ/邮箱与调用外部 AI 都必须先获得用户明确同意。
"""

from .audit import AuditEvent, AuditLogger, sanitize_detail
from .consent import (
    SCOPE_LABELS,
    ConsentLedger,
    ConsentManager,
    ConsentRecord,
    ConsentRequired,
    InMemoryConsentLedger,
    scope_label,
    scope_value,
)
from .credential_store import (
    EXTERNAL_AI_KEY,
    CredentialStoreError,
    InMemoryCredentialStore,
    KeyringCredentialStore,
    build_default_credential_store,
    mail_credential_key,
    mask_secret,
)
from .log_redaction import (
    REDACTED,
    RedactionFilter,
    hash_text,
    looks_like_secret,
    redact_text,
    safe_error,
)
from .logging_setup import JsonlFormatter, configure_logging, reset_logging

__all__ = [
    "AuditEvent",
    "AuditLogger",
    "ConsentLedger",
    "ConsentManager",
    "ConsentRecord",
    "ConsentRequired",
    "CredentialStoreError",
    "EXTERNAL_AI_KEY",
    "InMemoryConsentLedger",
    "InMemoryCredentialStore",
    "JsonlFormatter",
    "KeyringCredentialStore",
    "REDACTED",
    "RedactionFilter",
    "SCOPE_LABELS",
    "build_default_credential_store",
    "configure_logging",
    "hash_text",
    "looks_like_secret",
    "mail_credential_key",
    "mask_secret",
    "redact_text",
    "reset_logging",
    "safe_error",
    "sanitize_detail",
    "scope_label",
    "scope_value",
]
