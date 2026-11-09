"""选择器配置加载、版本校准的离线测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from social_secretary.collectors.qq.errors import QqProfileError
from social_secretary.collectors.qq.profile import (
    QqUiMode,
    QqUiProfile,
    default_profile_path,
    detect_file_version,
    load_profile,
    parse_version,
    version_check,
)


def test_default_profile_loads_from_repo_config() -> None:
    profile = load_profile()
    assert profile.schema_version == 1
    assert profile.process_names == ("QQ",)
    assert profile.baseline_version == "9.9.35.52892"
    assert profile.classic.bubble_class_pattern == "^message-container"
    assert profile.efficiency.bubble_class_pattern == "^msg-content-container"
    assert profile.behavior.min_tree_nodes >= 1
    assert profile.behavior.scroll_notches_per_pass >= 1
    assert profile.safety.get("send_capability") == "none"
    assert profile.mode_selectors(QqUiMode.CLASSIC) is profile.classic
    assert profile.mode_selectors(QqUiMode.EFFICIENCY) is profile.efficiency
    # 未知模式按经典模式兜底（只影响选择器，不影响安全）
    assert profile.mode_selectors(QqUiMode.UNKNOWN) is profile.classic


def test_default_profile_path_is_repo_config() -> None:
    path = default_profile_path()
    assert path.name == "qq_ui_profile.json"
    assert path.parent.name == "config"
    assert path.is_file()


def test_missing_profile_raises_readable_error(tmp_path: Path) -> None:
    with pytest.raises(QqProfileError) as excinfo:
        load_profile(tmp_path / "missing.json")
    assert "未找到" in excinfo.value.message


def test_invalid_json_raises_readable_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(QqProfileError) as excinfo:
        load_profile(path)
    assert "JSON 格式错误" in excinfo.value.message


def test_invalid_default_mode_rejected(tmp_path: Path) -> None:
    data = json.loads(default_profile_path().read_text(encoding="utf-8"))
    data["behavior"]["default_mode"] = "unknown-mode"
    path = tmp_path / "bad-mode.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(QqProfileError) as excinfo:
        load_profile(path)
    assert "default_mode" in excinfo.value.message


def test_invalid_regex_rejected(tmp_path: Path) -> None:
    data = json.loads(default_profile_path().read_text(encoding="utf-8"))
    data["modes"]["classic"]["bubble_class_pattern"] = "(unclosed"
    path = tmp_path / "bad-regex.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(QqProfileError) as excinfo:
        load_profile(path)
    assert "正则" in excinfo.value.message


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    data = json.loads(default_profile_path().read_text(encoding="utf-8"))
    del data["modes"]["classic"]["header_class_pattern"]
    path = tmp_path / "missing-field.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(QqProfileError) as excinfo:
        load_profile(path)
    assert "header_class_pattern" in excinfo.value.message


def test_from_dict_roundtrip() -> None:
    data = json.loads(default_profile_path().read_text(encoding="utf-8"))
    profile = QqUiProfile.from_dict(data, source_path="inline")
    assert profile.classic.text_control_types == ("Text",)
    assert profile.behavior.self_label == "我"
    assert profile.behavior.max_messages_hard_cap >= 100


def test_parse_version() -> None:
    assert parse_version("9.9.35.52892") == (9, 9, 35, 52892)
    assert parse_version("QQ NT 9.9.35.52892 (2026)") == (9, 9, 35, 52892)
    assert parse_version("not-a-version") is None
    assert parse_version(None) is None


def test_version_check_exact_match() -> None:
    check = version_check("9.9.35.52892", "9.9.35.52892")
    assert check.compatible is True
    assert "一致" in check.note


def test_version_check_same_major_minor() -> None:
    check = version_check("9.9.40.1", "9.9.35.52892")
    assert check.compatible is True


def test_version_check_unknown_and_incompatible() -> None:
    unknown = version_check(None, "9.9.35.52892")
    assert unknown.compatible is None
    assert "未检测到" in unknown.note

    incompatible = version_check("10.0.1.1", "9.9.35.52892")
    assert incompatible.compatible is False
    assert "qq_probe" in incompatible.note


def test_detect_file_version_reads_real_qq_exe_version() -> None:
    profile = load_profile()
    if not Path(profile.exe_path).is_file():
        pytest.skip("本机没有 D:\\QQ.exe，跳过文件版本探测")
    detected = detect_file_version(profile.exe_path)
    assert detected == profile.baseline_version


def test_detect_file_version_missing_file(tmp_path: Path) -> None:
    assert detect_file_version(tmp_path / "nope.exe") is None
    assert detect_file_version(None) is None


def test_profile_to_version_check_helper() -> None:
    profile = load_profile()
    check = profile.version_check(profile.baseline_version)
    assert check.compatible is True
    assert check.to_dict()["baseline"] == profile.baseline_version
