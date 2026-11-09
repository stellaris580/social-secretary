"""异常输入 / 截断 / 上限 / 去重 测试。"""

from __future__ import annotations

import hashlib

import pytest

from social_secretary.importing import ImportFormat, WarningCode
from social_secretary.importing.parser import parse_chat_text

from .conftest import assert_local_aware


# ---------------------------------------------------------------------------
# 异常输入：绝不崩溃
# ---------------------------------------------------------------------------

def test_empty_and_whitespace_input(make_options) -> None:
    for text in ("", "   ", "\n\n\n", "\t\r\n "):
        result = parse_chat_text(text, options=make_options())
        assert result.ok is False
        assert WarningCode.EMPTY_INPUT in result.warning_codes


def test_none_input_is_tolerated(make_options) -> None:
    result = parse_chat_text(None, options=make_options())
    assert result.ok is False
    assert WarningCode.EMPTY_INPUT in result.warning_codes


def test_bytes_input_raises_type_error(make_options) -> None:
    with pytest.raises(TypeError):
        parse_chat_text(b"bytes", options=make_options())  # type: ignore[arg-type]


def test_control_chars_and_nul_are_stripped(make_options) -> None:
    result = parse_chat_text("2024-01-01 12:30:00 张三: 你\x00好\x07", options=make_options())
    assert len(result.messages) == 1
    assert result.messages[0].raw.text == "你好"
    assert WarningCode.CONTROL_CHARS_STRIPPED in result.warning_codes


def test_lone_surrogate_does_not_crash(make_options) -> None:
    result = parse_chat_text("2024-01-01 12:30:00 张三: 坏\ud800字符", options=make_options())
    assert len(result.messages) == 1
    assert "\ud800" not in result.messages[0].raw.text
    # 去重哈希/证据哈希必须能编码，不抛异常
    assert result.to_evidence_dict()["entries_preview"]


def test_mojibake_replacement_warning(make_options) -> None:
    result = parse_chat_text("2024-01-01 12:30:00 张三: 乱码\ufffd\ufffd", options=make_options())
    assert len(result.messages) == 1
    assert result.stats.replaced_chars == 2
    assert WarningCode.MOJIBAKE_SUSPECTED in result.warning_codes


def test_html_input_unsupported_warning(make_options) -> None:
    result = parse_chat_text(
        "<!DOCTYPE html><html><body><p>hello</p></body></html>",
        options=make_options(),
    )
    assert result.ok is False
    assert WarningCode.UNSUPPORTED_HTML in result.warning_codes
    assert WarningCode.NO_MESSAGES in result.warning_codes


def test_pathological_inputs_never_raise(make_options) -> None:
    samples = [
        "::::::",
        "[[[[[[",
        "]]]]]]",
        "2024-99-99 99:99:99 张三: 坏",
        "a" * 5000,
        "\u200b\u200c\u200d",
        "1\n2\n3\n4\n5",
        "2024-01-01 12:30:00 : 空发送者",
    ]
    for sample in samples:
        result = parse_chat_text(sample, options=make_options(max_input_chars=1000))
        assert result.stats.elapsed_ms >= 0
        assert isinstance(result.messages, tuple)


# ---------------------------------------------------------------------------
# 截断 / 上限
# ---------------------------------------------------------------------------

def test_input_truncation_warning_and_message_count(make_options) -> None:
    lines = [f"2024-01-01 12:{minute:02d}:00 张三: 第 {minute} 条消息" for minute in range(30)]
    text = "\n".join(lines)
    result = parse_chat_text(text, options=make_options(max_input_chars=120))
    assert result.stats.truncated_input is True
    assert WarningCode.INPUT_TOO_LARGE in result.warning_codes
    assert result.stats.parsed_messages < len(lines)


def test_max_messages_limit(make_options) -> None:
    text = "\n".join(f"2024-01-01 12:00:00 张三: 消息{i}" for i in range(10))
    result = parse_chat_text(text, options=make_options(max_messages=3))
    assert len(result.messages) == 3
    assert result.stats.truncated_messages is True
    assert WarningCode.MESSAGE_LIMIT_REACHED in result.warning_codes


def test_max_message_chars_truncation(make_options) -> None:
    text = "2024-01-01 12:00:00 张三: " + "长" * 500
    result = parse_chat_text(text, options=make_options(max_message_chars=100))
    assert len(result.messages) == 1
    assert len(result.messages[0].raw.text) == 100
    assert result.stats.truncated_texts >= 1
    assert WarningCode.MESSAGE_TOO_LONG in result.warning_codes


def test_multiline_message_chars_truncation(make_options) -> None:
    text = "2024-01-01 12:00:00 张三: 第一行\n" + "\n".join("续行" * 20 for _ in range(10))
    result = parse_chat_text(text, options=make_options(max_message_chars=50))
    assert len(result.messages) == 1
    assert len(result.messages[0].raw.text) <= 50
    assert WarningCode.MESSAGE_TOO_LONG in result.warning_codes


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------

def test_dedupe_same_sender_text_and_time(make_options) -> None:
    text = (
        "2024-01-01 12:00:00 张三: 重复消息\n"
        "2024-01-01 12:00:00 张三: 重复消息\n"
        "2024-01-01 12:01:00 张三: 重复消息\n"
    )
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.stats.duplicate_messages == 1
    assert WarningCode.DUPLICATES_REMOVED in result.warning_codes


def test_dedupe_same_text_different_time_kept(make_options) -> None:
    text = "2024-01-01 12:00:00 张三: 好\n2024-01-01 12:01:00 张三: 好"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.stats.duplicate_messages == 0


def test_untimed_identical_messages_are_kept(make_options) -> None:
    text = "张三: 好的\n张三: 好的"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.stats.duplicate_messages == 0


def test_dedupe_can_be_disabled(make_options) -> None:
    text = "2024-01-01 12:00:00 张三: 重复\n2024-01-01 12:00:00 张三: 重复"
    result = parse_chat_text(text, options=make_options(dedupe=False))
    assert len(result.messages) == 2
    assert result.stats.duplicate_messages == 0


def test_dedupe_key_formula_matches_core(make_options) -> None:
    from social_secretary.core.dedupe import compute_content_hash
    from social_secretary.importing.parser import dedupe_key

    result = parse_chat_text("2024-01-01 12:00:00 张三: 你好", options=make_options())
    raw = result.messages[0].raw
    expected = compute_content_hash(
        raw.conversation_key,
        raw.sender,
        raw.is_self,
        raw.text,
        raw.timestamp,
        raw.timestamp_source,
    )
    bucket = raw.timestamp.strftime("%Y-%m-%dT%H:%M")
    assert dedupe_key(raw.conversation_key, raw.sender, raw.is_self, raw.text, bucket) == expected


def test_conversation_key_stable_across_imports(make_options) -> None:
    text = "2024-01-01 12:00:00 张三: 你好"
    first = parse_chat_text(text, options=make_options(conversation_title="项目群"))
    second = parse_chat_text(text, options=make_options(conversation_title="项目群"))
    assert first.conversation.conversation_key == second.conversation.conversation_key
    assert first.conversation.conversation_key.startswith("import:")


def test_explicit_conversation_key_is_respected(make_options) -> None:
    result = parse_chat_text(
        "2024-01-01 12:00:00 张三: 你好",
        options=make_options(conversation_key="custom:key"),
    )
    assert result.conversation.conversation_key == "custom:key"
    assert result.messages[0].raw.conversation_key == "custom:key"


def test_unknown_format_hint_falls_back_with_warning(make_options) -> None:
    result = parse_chat_text(
        "2024-01-01 12:00:00 张三: 你好",
        options=make_options(format_hint="excel-xls"),
    )
    assert len(result.messages) == 1
    assert WarningCode.FORMAT_UNKNOWN in result.warning_codes


def test_evidence_hash_uses_sha256(make_options) -> None:
    secret = "证据哈希测试"
    result = parse_chat_text(f"2024-01-01 12:00:00 张三: {secret}", options=make_options())
    evidence = result.to_evidence_dict()
    expected = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
    assert evidence["entries_preview"][0]["text"]["sha256_16"] == expected
