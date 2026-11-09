"""tests/mail 共享的合成数据构造器与测试替身（不含任何真实邮件/凭据）。"""

from __future__ import annotations

import importlib.util
import sys
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_IMAP_PATH = REPO_ROOT / "tools" / "mock_imap_server.py"


def load_mock_imap_module():
    """把 tools/mock_imap_server.py 作为独立模块导入（tools 不是 Python 包）。"""
    module_name = "ss_mock_imap_server"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, MOCK_IMAP_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件一定存在
        raise RuntimeError(f"无法加载 mock IMAP 模块: {MOCK_IMAP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def make_email(
    subject: str = "测试主题：周五前确认",
    body: str = "这是合成邮件正文，仅用于测试。",
    *,
    from_addr: str = "sender@example.com",
    from_name: str = "合成发件人",
    to_addr: str = "test@qq.com",
    date: str | None = "Wed, 29 Oct 2025 09:30:00 +0800",
    charset: str = "utf-8",
    html_body: str | None = None,
    attachment: tuple[str, bytes, str, str] | None = None,
    omit_date: bool = False,
    message_id: str | None = "<synthetic-001@example.com>",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """构造一封合成邮件的原始 RFC822 字节。

    attachment: (文件名, 内容, maintype, subtype)；只用于验证"只统计数量/大小"。
    """
    message = EmailMessage()
    if subject:
        message["Subject"] = subject
    if from_name:
        message["From"] = f"{from_name} <{from_addr}>"
    else:
        message["From"] = from_addr
    message["To"] = to_addr
    if message_id:
        message["Message-ID"] = message_id
    if not omit_date and date is not None:
        message["Date"] = date
    if extra_headers:
        for name, value in extra_headers.items():
            message[name] = value

    if html_body is not None:
        message.set_content(body, charset=charset)
        message.add_alternative(html_body, subtype="html", charset=charset)
    else:
        message.set_content(body, charset=charset)

    if attachment is not None:
        filename, content, maintype, subtype = attachment
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


class FakeCredentialStore:
    """内存版 contracts.CredentialStore；不落盘、不打印 secret，仅测试用。"""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self.get_calls: list[str] = []

    def set_secret(self, key: str, secret: str, *, label: str = "") -> None:
        self._secrets[key] = secret

    def get_secret(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self._secrets.get(key)

    def delete_secret(self, key: str) -> bool:
        return self._secrets.pop(key, None) is not None

    def has_secret(self, key: str) -> bool:
        return key in self._secrets
