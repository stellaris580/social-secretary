"""QQ 邮箱只读 IMAP 采集器包。

对外 API（app-core / UI / 测试使用）::

    from social_secretary.collectors.mail import (
        MailCollector, mail_credential_key,
        MailSyncState, SyncStateStore, MemorySyncStateStore,
        MailCollectorError, MailErrorCode, USER_MESSAGES,
        parse_email, ParsedMail,
        probe_imap_connectivity, write_evidence,
        ReadOnlyIMAP4, ReadOnlyIMAP4SSL, ReadOnlyViolation,
    )

接口文档：docs/design/mail-collector.md
可行性验证：docs/feasibility/02_qq_mail_imap.md
"""

from social_secretary.contracts import MessageSource

from .collector import (
    DEFAULT_FOLDERS,
    DEFAULT_HOST,
    DEFAULT_MAX_MESSAGE_BYTES,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    MailCollector,
    mail_credential_key,
    mask_email,
    translate_imap_exception,
)
from .errors import (
    USER_MESSAGES,
    MailAuthError,
    MailCollectorClosedError,
    MailCollectorError,
    MailConnectionError,
    MailCredentialMissingError,
    MailErrorCode,
    MailFolderNotFoundError,
    MailProtocolError,
    MailTimeoutError,
    MailTlsError,
    ReadOnlyViolation,
)
from .parser import ParsedMail, html_to_text, parse_email
from .session import ReadOnlyIMAP4, ReadOnlyIMAP4SSL, quietly_logout
from .state import (
    MailSyncState,
    MemorySyncStateStore,
    SyncStateStore,
    account_state_key,
    normalize_mailbox,
)

__all__ = [
    # contracts 协议（方便 isinstance 检查）
    "MessageSource",
    # collector
    "MailCollector",
    "mail_credential_key",
    "mask_email",
    "translate_imap_exception",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_FOLDERS",
    "DEFAULT_MAX_MESSAGE_BYTES",
    "DEFAULT_TIMEOUT",
    # state
    "MailSyncState",
    "SyncStateStore",
    "MemorySyncStateStore",
    "account_state_key",
    "normalize_mailbox",
    # errors
    "MailErrorCode",
    "USER_MESSAGES",
    "MailCollectorError",
    "MailCredentialMissingError",
    "MailAuthError",
    "MailConnectionError",
    "MailTlsError",
    "MailTimeoutError",
    "MailProtocolError",
    "MailFolderNotFoundError",
    "ReadOnlyViolation",
    "MailCollectorClosedError",
    # parser
    "ParsedMail",
    "parse_email",
    "html_to_text",
    # session
    "ReadOnlyIMAP4",
    "ReadOnlyIMAP4SSL",
    "quietly_logout",
    # connectivity（延迟导入：避免 ``python -m ...connectivity`` 触发 runpy 重复导入警告）
    "probe_imap_connectivity",
    "write_evidence",
]


def __getattr__(name: str):
    """延迟暴露 connectivity 模块的 API（见上）。"""
    if name in {"probe_imap_connectivity", "write_evidence"}:
        from . import connectivity

        return getattr(connectivity, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
