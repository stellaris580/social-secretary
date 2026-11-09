"""邮件（IMAP）采集器的错误码与异常类型。

错误码是 UI / core / 采集器之间共享的稳定契约（见 docs/design/mail-collector.md）。
所有异常信息都必须脱敏：不得包含邮箱授权码、邮件正文、完整收件地址。
"""

from __future__ import annotations

from enum import Enum


class MailErrorCode(str, Enum):
    """稳定的错误码（字符串值写入 ConnectionCheck.discovered / 日志 / 文档）。"""

    CREDENTIAL_MISSING = "MAIL_CREDENTIAL_MISSING"
    AUTH_FAILED = "MAIL_AUTH_FAILED"
    CONNECTION_FAILED = "MAIL_CONNECTION_FAILED"
    TLS_FAILED = "MAIL_TLS_FAILED"
    TIMEOUT = "MAIL_TIMEOUT"
    PROTOCOL_ERROR = "MAIL_PROTOCOL_ERROR"
    FOLDER_NOT_FOUND = "MAIL_FOLDER_NOT_FOUND"
    READ_ONLY_VIOLATION = "MAIL_READ_ONLY_VIOLATION"
    COLLECTOR_CLOSED = "MAIL_COLLECTOR_CLOSED"
    UNEXPECTED = "MAIL_UNEXPECTED"


#: 面向普通用户的简体中文提示（UI 直接展示；细节在日志里，不含敏感信息）。
USER_MESSAGES: dict[MailErrorCode, str] = {
    MailErrorCode.CREDENTIAL_MISSING: (
        "还没有保存这个邮箱的授权码。请在本机输入 QQ 邮箱授权码（不是 QQ 密码）后再试。"
    ),
    MailErrorCode.AUTH_FAILED: (
        "邮箱登录失败：授权码可能已失效或被重置。请到 QQ 邮箱设置中重新生成授权码，"
        "并确认 QQ 邮箱已开启 IMAP 服务。"
    ),
    MailErrorCode.CONNECTION_FAILED: (
        "无法连接 QQ 邮箱服务器（imap.qq.com:993）。请检查网络连接，或稍后再试。"
    ),
    MailErrorCode.TLS_FAILED: (
        "与 QQ 邮箱服务器的加密连接失败。请检查系统时间、网络代理设置后重试。"
    ),
    MailErrorCode.TIMEOUT: "连接 QQ 邮箱服务器超时，请检查网络后重试。",
    MailErrorCode.PROTOCOL_ERROR: "QQ 邮箱服务器返回了无法识别的响应，本次同步已停止。",
    MailErrorCode.FOLDER_NOT_FOUND: "邮箱文件夹不存在或未在允许采集的范围内。",
    MailErrorCode.READ_ONLY_VIOLATION: "采集器试图执行写入类 IMAP 命令，已被安全护栏阻止。",
    MailErrorCode.COLLECTOR_CLOSED: "采集器已关闭，无法继续同步。",
    MailErrorCode.UNEXPECTED: "同步邮件时发生未知错误，详情见日志。",
}


class MailCollectorError(RuntimeError):
    """邮件采集器的所有可预期错误基类（只读、可安全展示给 UI 的中文提示）。"""

    code: MailErrorCode = MailErrorCode.UNEXPECTED

    def __init__(self, detail: str = "", *, code: MailErrorCode | None = None) -> None:
        if code is not None:
            self.code = code
        self.detail = _sanitize_detail(detail)
        super().__init__(self._compose())

    def _compose(self) -> str:
        if self.detail:
            return f"{self.code.value}: {self.detail}"
        return self.code.value

    @property
    def user_message(self) -> str:
        return USER_MESSAGES.get(self.code, USER_MESSAGES[MailErrorCode.UNEXPECTED])

    def to_discovered(self) -> dict[str, object]:
        """给 ConnectionCheck.discovered 用的最小脱敏信息。"""
        data: dict[str, object] = {"code": self.code.value}
        if self.detail:
            data["error"] = self.detail
        return data


class MailCredentialMissingError(MailCollectorError):
    code = MailErrorCode.CREDENTIAL_MISSING


class MailAuthError(MailCollectorError):
    code = MailErrorCode.AUTH_FAILED


class MailConnectionError(MailCollectorError):
    code = MailErrorCode.CONNECTION_FAILED


class MailTlsError(MailCollectorError):
    code = MailErrorCode.TLS_FAILED


class MailTimeoutError(MailCollectorError):
    code = MailErrorCode.TIMEOUT


class MailProtocolError(MailCollectorError):
    code = MailErrorCode.PROTOCOL_ERROR


class MailFolderNotFoundError(MailCollectorError):
    code = MailErrorCode.FOLDER_NOT_FOUND


class ReadOnlyViolation(MailCollectorError):
    code = MailErrorCode.READ_ONLY_VIOLATION


class MailCollectorClosedError(MailCollectorError):
    code = MailErrorCode.COLLECTOR_CLOSED


def _sanitize_detail(detail: str, *, limit: int = 240) -> str:
    """去掉换行/控制字符，截断长度，避免把整段服务器响应写进 UI/日志。"""
    text = " ".join(str(detail or "").split())
    # 防御性：URL/命令行里不会出现授权码，但这里再截断一次。
    return text[:limit]


__all__ = [
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
]
