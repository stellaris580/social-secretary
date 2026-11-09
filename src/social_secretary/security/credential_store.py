"""凭据存储：邮箱授权码 / 外部 AI API Key 只能进 Windows 凭据管理器（keyring）。

铁律：
- 凭据绝不写入普通文件、绝不打印、绝不进日志；日志里只允许出现「是否已保存」。
- ``get_secret`` 不写任何日志（本模块不使用 logging）。
- 测试注入 ``InMemoryCredentialStore``，绝不触碰真实凭据管理器。
"""

from __future__ import annotations

import threading
from typing import Any

SERVICE_NAME = "SocialSecretary"
MAIL_KEY_PREFIX = "mail:qq:"
EXTERNAL_AI_KEY = "external_ai:api_key"


class CredentialStoreError(RuntimeError):
    """凭据管理器不可用 / 操作失败（错误信息中不含凭据本身）。"""


def mail_credential_key(email: str) -> str:
    """邮箱凭据的 key 命名规范：``mail:qq:<email>``。"""
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("邮箱地址格式不正确")
    return f"{MAIL_KEY_PREFIX}{normalized}"


def mask_secret(secret: str | None) -> str:
    """只暴露「有没有 / 多长」，绝不暴露内容。"""
    if not secret:
        return "<未设置>"
    return f"<已保存 {len(secret)} 位>"


class KeyringCredentialStore:
    """Windows 凭据管理器实现（keyring WinVaultKeyring）。

    ``keyring_backend`` 仅用于测试注入假的 keyring 模块；生产环境传 None。
    """

    def __init__(
        self, *, service_name: str = SERVICE_NAME, keyring_backend: Any | None = None
    ) -> None:
        self._service_name = service_name
        self._backend = keyring_backend

    def _keyring(self) -> Any:
        if self._backend is not None:
            return self._backend
        import keyring  # 延迟 import：--selftest 不依赖凭据管理器

        return keyring

    def set_secret(self, key: str, secret: str, *, label: str = "") -> None:
        if not key or not str(key).strip():
            raise ValueError("凭据 key 不能为空")
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("凭据内容不能为空")
        try:
            self._keyring().set_password(self._service_name, str(key), secret)
        except CredentialStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不把原始异常（可能含内部路径）暴露给 UI
            raise CredentialStoreError(
                f"写入系统凭据管理器失败（{type(exc).__name__}）"
            ) from None

    def get_secret(self, key: str) -> str | None:
        if not key:
            return None
        try:
            return self._keyring().get_password(self._service_name, str(key))
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(
                f"读取系统凭据管理器失败（{type(exc).__name__}）"
            ) from None

    def delete_secret(self, key: str) -> bool:
        if not key:
            return False
        try:
            self._keyring().delete_password(self._service_name, str(key))
            return True
        except Exception as exc:  # noqa: BLE001
            # 条目不存在视为删除成功（幂等）
            if type(exc).__name__ in {"PasswordDeleteError", "KeyError"}:
                return False
            raise CredentialStoreError(
                f"删除系统凭据管理器条目失败（{type(exc).__name__}）"
            ) from None

    def has_secret(self, key: str) -> bool:
        try:
            return self.get_secret(key) is not None
        except CredentialStoreError:
            return False

    def __repr__(self) -> str:  # pragma: no cover - 防御性：repr 不泄漏
        return f"KeyringCredentialStore(service={self._service_name!r})"


class InMemoryCredentialStore:
    """测试用内存凭据存储：不写文件、可通过 ``keys()`` 断言，绝不打印值。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: dict[str, str] = {}

    def set_secret(self, key: str, secret: str, *, label: str = "") -> None:
        if not key or not str(key).strip():
            raise ValueError("凭据 key 不能为空")
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("凭据内容不能为空")
        with self._lock:
            self._secrets[str(key)] = secret

    def get_secret(self, key: str) -> str | None:
        with self._lock:
            return self._secrets.get(str(key))

    def delete_secret(self, key: str) -> bool:
        with self._lock:
            return self._secrets.pop(str(key), None) is not None

    def has_secret(self, key: str) -> bool:
        with self._lock:
            return str(key) in self._secrets

    def keys(self) -> list[str]:
        """仅返回 key 名（不含值），供测试与诊断使用。"""
        with self._lock:
            return sorted(self._secrets)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def __repr__(self) -> str:  # pragma: no cover - 防御性：repr 不泄漏
        return f"InMemoryCredentialStore(keys={len(self._secrets)})"


def build_default_credential_store() -> KeyringCredentialStore:
    """生产默认：Windows 凭据管理器。构造时不访问后端，首次读写才探测。"""
    return KeyringCredentialStore()


__all__ = [
    "CredentialStoreError",
    "EXTERNAL_AI_KEY",
    "InMemoryCredentialStore",
    "KeyringCredentialStore",
    "MAIL_KEY_PREFIX",
    "SERVICE_NAME",
    "build_default_credential_store",
    "mail_credential_key",
    "mask_secret",
]
