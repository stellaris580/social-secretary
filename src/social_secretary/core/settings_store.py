"""非敏感设置持久化（``<data_dir>/settings.json``）。

- 原子写：先写同目录临时文件，再 ``os.replace`` 覆盖，避免程序崩溃造成半个 JSON。
- 凭据绝不在这里：邮箱授权码 / AI API Key 只进 Windows 凭据管理器（security.credential_store）。
- 保存前做一次「疑似密钥」校验：如果用户把 ``sk-...`` / 授权码形态的字符串误粘贴进
  设置项（例如 base_url），直接拒绝保存，避免把密钥落盘。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts import AppSettings
from ..security.log_redaction import looks_like_secret

# 这些字段允许用户填 URL / 模型名，但不允许包含密钥
_STRING_FIELDS = (
    "external_ai_base_url",
    "external_ai_model",
    "log_level",
    "theme",
)


class SettingsStore:
    """AppSettings 的 JSON 持久化。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AppSettings:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return AppSettings()
        except OSError:
            return AppSettings()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            self._backup_corrupt()
            return AppSettings()
        if not isinstance(data, dict):
            return AppSettings()
        try:
            return AppSettings.from_dict(data)
        except TypeError:
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self._validate_no_secrets(settings)
        payload = settings.to_dict()
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="settings-", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def update(self, **changes: Any) -> AppSettings:
        settings = replace(self.load(), **changes)
        self.save(settings)
        return settings

    def reset(self) -> AppSettings:
        settings = AppSettings()
        self.save(settings)
        return settings

    # -- 内部 ---------------------------------------------------------------

    def _validate_no_secrets(self, settings: AppSettings) -> None:
        for field_name in _STRING_FIELDS:
            value = getattr(settings, field_name, "")
            if isinstance(value, str) and looks_like_secret(value):
                raise ValueError(
                    f"设置项 {field_name} 疑似包含密钥/授权码；密钥请通过凭据管理器保存"
                )

    def _backup_corrupt(self) -> None:
        """损坏的 settings.json 改名保留，便于诊断（不含密钥）。"""
        try:
            backup = self._path.with_suffix(self._path.suffix + ".corrupt")
            if self._path.exists():
                os.replace(self._path, backup)
        except OSError:
            pass


__all__ = ["SettingsStore"]
