"""凭据存储测试：只进凭据管理器、不落盘、不打印、错误信息不含密钥。"""

from __future__ import annotations

import pytest

from social_secretary.security.credential_store import (
    EXTERNAL_AI_KEY,
    CredentialStoreError,
    InMemoryCredentialStore,
    KeyringCredentialStore,
    build_default_credential_store,
    mail_credential_key,
    mask_secret,
)

AUTH_CODE = "abcdefghijklmnop"
API_KEY = "sk-testplaceholder1234567890"


class FakeKeyring:
    """模拟 keyring 模块（`set_password/get_password/delete_password`）。"""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.error: Exception | None = None

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.error:
            raise self.error
        self.data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        if self.error:
            raise self.error
        return self.data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if self.error:
            raise self.error
        if (service, username) not in self.data:
            raise KeyError("not found")
        del self.data[(service, username)]


# ---------------------------------------------------------------------------
# InMemoryCredentialStore（测试注入用）
# ---------------------------------------------------------------------------

def test_in_memory_store_crud() -> None:
    store = InMemoryCredentialStore()
    assert store.has_secret("mail:qq:a@example.com") is False
    assert store.get_secret("mail:qq:a@example.com") is None

    store.set_secret("mail:qq:a@example.com", AUTH_CODE, label="QQ邮箱授权码")
    assert store.has_secret("mail:qq:a@example.com") is True
    assert store.get_secret("mail:qq:a@example.com") == AUTH_CODE
    assert store.keys() == ["mail:qq:a@example.com"]
    assert store.delete_secret("mail:qq:a@example.com") is True
    assert store.delete_secret("mail:qq:a@example.com") is False
    assert store.keys() == []
    # repr 不暴露密钥
    store.set_secret("k", API_KEY)
    assert API_KEY not in repr(store)


@pytest.mark.parametrize("key,secret", [("", "value"), ("key", ""), ("key", "   ")])
def test_in_memory_store_validates_input(key: str, secret: str) -> None:
    with pytest.raises(ValueError):
        InMemoryCredentialStore().set_secret(key, secret)


def test_mail_credential_key_normalization() -> None:
    assert mail_credential_key(" Foo@Example.COM ") == "mail:qq:foo@example.com"
    with pytest.raises(ValueError):
        mail_credential_key("not-an-email")


def test_mask_secret_never_reveals() -> None:
    assert mask_secret(None) == "<未设置>"
    masked = mask_secret(AUTH_CODE)
    assert AUTH_CODE not in masked
    assert "16" in masked


# ---------------------------------------------------------------------------
# KeyringCredentialStore（注入 fake keyring，不触碰真实凭据管理器）
# ---------------------------------------------------------------------------

def test_keyring_store_uses_backend_and_roundtrips() -> None:
    backend = FakeKeyring()
    store = KeyringCredentialStore(keyring_backend=backend)
    store.set_secret(EXTERNAL_AI_KEY, API_KEY, label="外部 AI API Key")
    assert backend.data[("SocialSecretary", EXTERNAL_AI_KEY)] == API_KEY
    assert store.get_secret(EXTERNAL_AI_KEY) == API_KEY
    assert store.has_secret(EXTERNAL_AI_KEY) is True
    assert store.delete_secret(EXTERNAL_AI_KEY) is True
    assert store.has_secret(EXTERNAL_AI_KEY) is False
    # 删除不存在的条目 → False，不抛异常
    assert store.delete_secret(EXTERNAL_AI_KEY) is False
    assert API_KEY not in repr(store)


def test_keyring_store_error_does_not_leak_secret(tmp_path) -> None:
    backend = FakeKeyring()
    backend.error = RuntimeError(f"backend exploded with {AUTH_CODE} and {API_KEY}")
    store = KeyringCredentialStore(keyring_backend=backend)
    with pytest.raises(CredentialStoreError) as excinfo:
        store.set_secret("mail:qq:a@example.com", AUTH_CODE)
    message = str(excinfo.value)
    assert "RuntimeError" in message
    assert AUTH_CODE not in message and API_KEY not in message
    # has_secret 把错误吞掉，UI 不崩溃
    assert store.has_secret("anything") is False


def test_no_secret_written_to_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = InMemoryCredentialStore()
    store.set_secret(EXTERNAL_AI_KEY, API_KEY)
    keyring_store = KeyringCredentialStore(keyring_backend=FakeKeyring())
    keyring_store.set_secret("mail:qq:a@example.com", AUTH_CODE)

    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert API_KEY.encode() not in content
            assert AUTH_CODE.encode() not in content


def test_build_default_store_is_lazy_keyring() -> None:
    store = build_default_credential_store()
    assert isinstance(store, KeyringCredentialStore)
    assert isinstance(store, KeyringCredentialStore)  # 构造不访问后端，不抛异常
