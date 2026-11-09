"""tests/mail 的 pytest fixture：mock IMAP 服务器、内存凭据/同步状态、采集器工厂。"""

from __future__ import annotations

import socket

import pytest

from social_secretary.collectors.mail import (
    MailCollector,
    MemorySyncStateStore,
    mail_credential_key,
)
from social_secretary.contracts import Account, AccountKind

from .helpers import FakeCredentialStore, load_mock_imap_module

MOCK = load_mock_imap_module()


@pytest.fixture(scope="session")
def mock_module():
    return MOCK


@pytest.fixture
def credentials() -> FakeCredentialStore:
    return FakeCredentialStore()


@pytest.fixture
def state_store() -> MemorySyncStateStore:
    return MemorySyncStateStore()


@pytest.fixture
def account() -> Account:
    return Account(
        id="acc-mail-1",
        kind=AccountKind.EMAIL,
        display_name="QQ 邮箱（合成）",
        identifier="test@qq.com",
    )


@pytest.fixture
def start_mock(mock_module):
    """启动 mock 服务器工厂；测试结束统一清理。"""
    servers = []

    def _start(**kwargs):
        mailbox = kwargs.pop("mailbox", None)
        server = mock_module.MockImapServer(mailbox=mailbox, **kwargs)
        server.start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.stop()


@pytest.fixture
def mailbox(mock_module):
    return mock_module.MockMailbox(uidvalidity=100)


@pytest.fixture
def mock_server(start_mock, mailbox):
    return start_mock(mailbox=mailbox)


@pytest.fixture
def make_collector(mock_server, credentials, state_store, account):
    """默认指向 mock_server、使用内存凭据/同步状态的采集器工厂。"""
    def _make(*, password: str | None = "mock-auth-code", **overrides) -> MailCollector:
        credential_key = overrides.pop("credential_key", mail_credential_key(account.identifier))
        if password is not None:
            credentials.set_secret(credential_key, password)
        kwargs = {
            "account": account,
            "credential_store": credentials,
            "sync_state": state_store,
            "host": "127.0.0.1",
            "port": mock_server.port,
            "insecure_plaintext": True,
            "timeout": 2.0,
        }
        kwargs.update(overrides)
        return MailCollector(**kwargs)

    return _make


@pytest.fixture
def collector(make_collector) -> MailCollector:
    return make_collector()


@pytest.fixture
def conversations(collector) -> list:
    return collector.list_conversations()


def unused_tcp_port() -> int:
    """拿一个刚释放的本机端口（用于"连接被拒绝"测试）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
