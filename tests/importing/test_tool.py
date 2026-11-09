"""原型工具 tools/import_chat_text.py 的纯函数测试（不打开 GUI 窗口）。"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from social_secretary.importing import ImportOptions, parse_chat_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = PROJECT_ROOT / "tools" / "import_chat_text.py"
FIXED_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture(scope="module")
def tool():
    assert TOOL_PATH.is_file(), "工具文件不存在"
    spec = importlib.util.spec_from_file_location("import_chat_text_tool", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse(text: str, **kwargs) -> object:
    kwargs.setdefault("format_hint", "auto")
    options = ImportOptions(
        format_hint=kwargs.pop("format_hint"),
        now=FIXED_NOW,
        source_name=kwargs.pop("source_name", "tool_sample.txt"),
        **kwargs,
    )
    return parse_chat_text(text, options=options)


def test_tool_module_imports_without_creating_window(tool) -> None:
    assert tool.EXPERIMENTAL is True
    assert "实验性" in tool.EXPERIMENTAL_NOTICE
    assert tool.TOOL_NAME == "import_chat_text"
    assert set(tool._FORMAT_TO_HINT.values()) == {"auto", "generic", "qq_copy", "json", "csv"}


def test_build_import_options_parses_self_names(tool) -> None:
    options = tool.build_import_options(
        file_name="chat.txt",
        conversation_title="  项目群  ",
        self_names_text="我，Alice、Bob",
        format_hint="generic",
        now=FIXED_NOW,
    )
    assert options.conversation_title == "项目群"
    assert options.format_hint == "generic"
    assert options.self_sender_names == ("我", "Alice", "Bob")
    assert options.source_name == "chat.txt"


def test_masked_preview_hides_text_by_default(tool) -> None:
    secret = "绝密消息不应显示"
    result = _parse(f"2024-01-01 12:30:00 张三: {secret}")
    masked = tool.masked_preview(result)
    assert secret not in masked
    assert "张三" not in masked
    assert "已脱敏" in masked
    revealed = tool.masked_preview(result, reveal=True)
    assert secret in revealed


def test_format_stats_text_has_no_message_content(tool) -> None:
    secret = "统计里也不能有正文"
    result = _parse(f"2024-01-01 12:30:00 张三: {secret}")
    text = tool.format_stats_text(result)
    assert secret not in text
    assert "解析消息：1" in text
    assert "qq_copy" not in text or "QQ" in text


def test_build_summary_counts_messages(tool) -> None:
    text = (
        "2024-01-01 12:30:00 张三: 请明天中午前回复确认\n"
        "2024-01-01 12:31:00 我: 收到\n"
        "2024-01-02 09:00:00 李四: 通知：本周五开会\n"
    )
    result = _parse(text)
    summary = tool.build_summary(result)
    assert summary.message_count == 3
    assert summary.sender_count == 3
    assert summary.self_messages == 1
    assert sum(summary.day_counts.values()) == 3
    assert "2024-01-01" in summary.day_counts
    assert any("实验性" in note for note in summary.notes)


def test_read_chat_file_returns_masked_info(tool, tmp_path: Path) -> None:
    path = tmp_path / "私有聊天.txt"
    path.write_text("2024-01-01 12:30:00 张三: 你好", encoding="utf-8")
    data, info = tool.read_chat_file(path)
    assert data.startswith(b"2024-01-01")
    assert info["extension"] == ".txt"
    assert info["size_bytes"] > 0
    assert len(info["content_sha256_16"]) == 16
    assert "私有聊天" not in json.dumps(info, ensure_ascii=False)
    assert info["name_fingerprint"]["length"] == len("私有聊天.txt")


def test_read_chat_file_rejects_missing_and_oversized(tool, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        tool.read_chat_file(tmp_path / "不存在.txt")
    huge = tmp_path / "huge.txt"
    huge.write_bytes(b"a" * (tool.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError):
        tool.read_chat_file(huge)


def test_build_evidence_payload_is_desensitized(tool, tmp_path: Path) -> None:
    secret = "证据里不该出现的聊天原文"
    result = _parse(f"2024-01-01 12:30:00 张三: {secret}", source_name=" confidential.txt")
    sample = tmp_path / "confidential.txt"
    sample.write_text("x", encoding="utf-8")
    _, file_info = tool.read_chat_file(sample)
    payload = tool.build_evidence_payload(result, mode="dry-run", file_info=file_info)
    dumped = json.dumps(payload, ensure_ascii=False)
    assert secret not in dumped
    assert "张三" not in dumped
    assert "confidential.txt" not in dumped
    assert payload["privacy"]["evidence_contains_raw_text"] is False
    assert payload["tool"]["experimental"] is True
    assert payload["dry_run"] is None


def test_clear_test_data_only_removes_import_dirs(tool, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "import_20260101_000000").mkdir()
    (root / "keep_me").mkdir()
    (root / "loose_file.txt").write_text("x", encoding="utf-8")
    removed = tool.clear_test_data(root)
    assert removed == 1
    assert (root / "keep_me").is_dir()
    assert (root / "loose_file.txt").is_file()
    assert not (root / "import_20260101_000000").exists()


def test_selftest_sample_is_synthetic(tool) -> None:
    assert "合成消息" in tool.SELFTEST_TEXT
    assert "消息记录" in tool.SELFTEST_TEXT
    assert tool.SELFTEST_TEXT.count("收到，这是第二条") == 2, "自检样例应包含重复消息以验证去重"
