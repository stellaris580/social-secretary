"""日志/证据脱敏工具。

铁律：日志与证据文件中不得出现消息正文、邮箱授权码、API Key。
本模块提供「识别 + 遮盖」能力；是否记录某个字段由调用方在架构上保证：
核心层只记录 id / 计数 / 哈希（见 ``hash_text``），绝不记录消息正文。
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

REDACTED = "<已脱敏>"

# 形如 api_key=xxx / token: xxx / 授权码：xxx / password=xxx 的键值对（含中文叫法）
_SECRET_FIELD = (
    r"(?:api[_-]?key|apikey|access[_-]?key|access[_-]?token|token|secret|"
    r"client[_-]?secret|password|passwd|pwd|auth[_-]?code|authorization|cookie|"
    r"授权码|密码|密钥|口令|凭据)"
)
_SECRET_ASSIGN = re.compile(rf"(?i)({_SECRET_FIELD}\s*[:=：]\s*)([^\s,;，；]+)")
_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/\-]{4,})=*")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")
_AK_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_QQ_AUTH = re.compile(r"(?i)((?:授权码|auth\s*code)\s*[:=：]?\s*)([a-z0-9]{8,32})\b")
_PLAIN_AUTH_CODE = re.compile(r"^[a-z]{16}$")

DEFAULT_MAX_LEN = 200


def redact_text(text: Any, *, max_len: int | None = DEFAULT_MAX_LEN) -> str:
    """遮盖字符串中的密钥/授权码。``max_len=None`` 表示不截断。"""
    if text is None:
        return ""
    s = str(text)
    # 先处理 Bearer（否则 Authorization: Bearer xxx 只替换掉 Bearer，token 会残留）
    s = _BEARER.sub(lambda m: m.group(1) + REDACTED, s)
    s = _SECRET_ASSIGN.sub(lambda m: m.group(1) + REDACTED, s)
    s = _SK_KEY.sub(REDACTED, s)
    s = _AK_KEY.sub(REDACTED, s)
    s = _QQ_AUTH.sub(lambda m: m.group(1) + REDACTED, s)
    if max_len is not None and len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def hash_text(text: Any, *, length: int = 16) -> str:
    """为需要可关联但不可还原的文本生成哈希（用于日志/证据）。"""
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    return "sha256:" + digest[:length]


def looks_like_secret(text: Any) -> bool:
    """粗判字符串是否疑似密钥/授权码（用于设置校验，避免把密钥写进 json 文件）。"""
    s = str(text).strip()
    if not s:
        return False
    if _SK_KEY.search(s) or _AK_KEY.search(s) or _BEARER.search(s):
        return True
    if _SECRET_ASSIGN.search(s) or _QQ_AUTH.search(s):
        return True
    # QQ 邮箱授权码常见形态：16 位小写字母
    return bool(_PLAIN_AUTH_CODE.fullmatch(s))


def _redact_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value, max_len=None)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {key: _redact_arg(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_arg(item) for item in value]
    return redact_text(repr(value), max_len=None)


class RedactionFilter(logging.Filter):
    """在日志进入 handler 之前，把 msg/args 中的敏感信息就地脱敏。"""

    def __init__(self, name: str = "") -> None:
        super().__init__(name)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401 - logging 接口
        try:
            # 先完成 % 插值，再整体脱敏，最后清空 args，避免 msg 被替换后 % 格式失配
            if record.args:
                message = record.getMessage()
            elif isinstance(record.msg, str):
                message = record.msg
            else:
                message = str(record.msg)
            record.msg = redact_text(message, max_len=None)
            record.args = ()
        except Exception:  # noqa: BLE001 - 脱敏失败宁可丢弃消息，也不能让日志崩溃或泄漏
            record.msg = REDACTED
            record.args = ()
        return True


def safe_error(exc: BaseException, *, max_len: int = 160) -> str:
    """把异常转换为可记录的短字符串：只保留类型名与脱敏后的简述。"""
    detail = redact_text(str(exc), max_len=max_len)
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


__all__ = [
    "DEFAULT_MAX_LEN",
    "REDACTED",
    "RedactionFilter",
    "hash_text",
    "looks_like_secret",
    "redact_text",
    "safe_error",
]
