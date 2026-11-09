"""设置持久化测试：原子写、损坏恢复、疑似密钥拒绝落盘。"""

from __future__ import annotations

import json

import pytest

from social_secretary.contracts import AppSettings
from social_secretary.core.settings_store import SettingsStore


def test_missing_file_returns_defaults(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    settings = store.load()
    assert settings == AppSettings()
    assert settings.collection_paused is True
    assert settings.external_ai_consent is False


def test_save_and_load_roundtrip_is_atomic(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(collection_paused=False, log_level="DEBUG", theme="dark")
    store.save(settings)
    assert store.load() == settings
    # 原子写：不残留临时文件
    assert path.exists()
    assert [p.name for p in tmp_path.glob("settings-*.tmp")] == []
    # 文件内容是可读 JSON 且不含凭据字段
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["collection_paused"] is False
    assert "auth_code" not in payload and "api_key" not in payload


def test_corrupt_file_falls_back_to_defaults(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = SettingsStore(path)
    assert store.load() == AppSettings()
    assert (tmp_path / "settings.json.corrupt").exists()


def test_unknown_fields_are_ignored(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"log_level": "WARNING", "unknown": 1}), encoding="utf-8")
    settings = SettingsStore(path).load()
    assert settings.log_level == "WARNING"


def test_refuses_to_persist_suspected_secret(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings(log_level="INFO"))

    with pytest.raises(ValueError):
        store.save(
            AppSettings(
                log_level="INFO",
                external_ai_base_url="https://api.example.com/v1?api_key=sk-abcdef123456",
            )
        )
    # 旧文件未被覆盖，也绝无密钥落盘
    assert "sk-abcdef123456" not in path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        store.update(external_ai_model="abcdefghijklmnop")


def test_update_and_reset(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    updated = store.update(log_level="DEBUG")
    assert updated.log_level == "DEBUG"
    assert store.load().log_level == "DEBUG"
    assert store.reset() == AppSettings()
    assert store.load() == AppSettings()
