"""实验性「用户主动导入聊天文本」包（P1 原型）。

对外 API
--------
- :func:`parse_chat_text` / :func:`parse_chat_bytes` / :func:`decode_text_bytes`
- :class:`ImportOptions`、:class:`ImportResult`、:class:`ImportStats`、:class:`ImportWarning`
- :class:`MessageSink`、:func:`run_import`（含 dry-run）、:class:`JsonlMessageSink`

边界
----
本包只处理用户**主动选择/粘贴**的文本：不读取 QQ 本地消息数据库、不调用 QQ 非官方
协议、不抓取登录凭据、不联网。功能整体为实验性，不保证兼容任意导出格式，导入内容
只存本地、不会自动同步，**不等于“自动读取 QQ 聊天记录”**。
"""

from __future__ import annotations

from .importer import (
    ImportRunReport,
    JsonlMessageSink,
    MessageSink,
    import_result,
    run_import,
    validate_sink,
)
from .model import (
    DEFAULT_SELF_NAMES,
    EXPERIMENTAL,
    EXPERIMENTAL_NOTICE,
    ImportFormat,
    ImportOptions,
    ImportResult,
    ImportStats,
    ImportSummary,
    ImportWarning,
    ImportedConversation,
    ImportedMessage,
    WarningCode,
    fingerprint_hash,
    text_fingerprint,
)
from .parser import (
    MAX_INPUT_CHARS,
    DecodedChatText,
    decode_text_bytes,
    parse_chat_bytes,
    parse_chat_text,
)

__all__ = [
    "DEFAULT_SELF_NAMES",
    "EXPERIMENTAL",
    "EXPERIMENTAL_NOTICE",
    "MAX_INPUT_CHARS",
    "DecodedChatText",
    "ImportFormat",
    "ImportOptions",
    "ImportResult",
    "ImportRunReport",
    "ImportStats",
    "ImportSummary",
    "ImportWarning",
    "ImportedConversation",
    "ImportedMessage",
    "JsonlMessageSink",
    "MessageSink",
    "WarningCode",
    "decode_text_bytes",
    "fingerprint_hash",
    "import_result",
    "parse_chat_bytes",
    "parse_chat_text",
    "run_import",
    "text_fingerprint",
    "validate_sink",
]
