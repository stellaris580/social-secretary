"""数据模型测试：选项校验、格式枚举、统计、脱敏证据。"""

from __future__ import annotations

import json

import pytest

from social_secretary.contracts import (
    ConversationKind,
    RawMessage,
    TimestampSource,
)
from social_secretary.importing import (
    EXPERIMENTAL,
    EXPERIMENTAL_NOTICE,
    ImportFormat,
    ImportOptions,
    ImportResult,
    ImportStats,
    ImportWarning,
    ImportedConversation,
    ImportedMessage,
    WarningCode,
    text_fingerprint,
)
from social_secretary.importing.parser import parse_chat_text


def test_experimental_flags_are_set() -> None:
    assert EXPERIMENTAL is True
    assert "实验性" in EXPERIMENTAL_NOTICE


def test_import_format_coerce_and_labels() -> None:
    assert ImportFormat.coerce(None) is ImportFormat.AUTO
    assert ImportFormat.coerce("QQ_COPY") is ImportFormat.QQ_COPY
    assert ImportFormat.coerce("jsonl") is ImportFormat.JSON
    assert ImportFormat.coerce("unknown-value") is ImportFormat.AUTO
    assert ImportFormat.AUTO.label_zh == "自动识别"
    assert "QQ" in ImportFormat.QQ_COPY.label_zh


def test_options_validation_and_helpers() -> None:
    with pytest.raises(ValueError):
        ImportOptions(max_input_chars=0)
    with pytest.raises(ValueError):
        ImportOptions(max_messages=0)
    with pytest.raises(ValueError):
        ImportOptions(max_message_chars=0)

    options = ImportOptions(now=None, self_sender_names=("我", " ", "Alice"))
    assert options.resolved_format() is ImportFormat.AUTO
    assert options.resolved_title() == "导入的聊天记录"
    assert options.resolved_self_names() == ("我", "Alice")
    assert options.resolved_now().tzinfo is not None
    assert ImportOptions(conversation_kind="group").conversation_kind is ConversationKind.GROUP


def test_options_default_self_names_include_wo() -> None:
    assert "我" in ImportOptions().self_sender_names
    assert ImportOptions().dedupe is True


def test_stats_parse_success_rate() -> None:
    stats = ImportStats(parsed_messages=9, skipped_lines=1)
    assert stats.parse_success_rate == 0.9
    assert ImportStats().parse_success_rate == 0.0
    assert stats.to_dict()["parsed_messages"] == 9


def test_warning_to_dict_has_no_content_fields() -> None:
    warning = ImportWarning(code=WarningCode.UNRECOGNIZED_LINE, message="跳过若干行", count=3, line_numbers=(2, 4, 6))
    payload = warning.to_dict()
    assert payload["code"] == "unrecognized_line"
    assert payload["count"] == 3
    assert payload["line_numbers"] == [2, 4, 6]
    assert "text" not in payload


def test_text_fingerprint_is_irreversible() -> None:
    secret = "这是不应出现在证据里的私密内容"
    fingerprint = text_fingerprint(secret)
    assert fingerprint["length"] == len(secret)
    assert len(fingerprint["sha256_16"]) == 16
    assert secret not in json.dumps(fingerprint, ensure_ascii=False)


def test_import_result_helpers(make_options) -> None:
    result = parse_chat_text(
        "2024-01-01 10:00:00 张三: 你好\n2024-01-01 10:01:00 我: 收到",
        options=make_options(),
    )
    assert result.ok is True
    assert len(result.raw_messages) == 2
    assert all(isinstance(raw, RawMessage) for raw in result.raw_messages)
    assert result.raw_messages[0].timestamp_source is TimestampSource.UI
    assert WarningCode.SELF_SENDER_ASSUMED in result.warning_codes
    assert result.warning_by_code(WarningCode.SELF_SENDER_ASSUMED) is not None
    assert result.warning_by_code(WarningCode.EMPTY_INPUT) is None


def test_evidence_dict_masks_message_text(make_options) -> None:
    secret = "PRIVATE-TOKEN-绝不外泄"
    result = parse_chat_text(
        f"2024-01-01 10:00:00 张三: {secret}",
        options=make_options(source_name="secret_chat.txt"),
    )
    payload = json.dumps(result.to_evidence_dict(), ensure_ascii=False)
    assert secret not in payload
    assert "张三" not in payload
    assert "secret_chat.txt" not in payload
    assert "entries_preview" in payload
    entry = json.loads(payload)["entries_preview"][0]
    assert entry["text"]["length"] == len(secret)
    assert entry["sender"]["length"] == len("张三")


def test_empty_result_shape() -> None:
    result = parse_chat_text("", options=ImportOptions())
    assert result.ok is False
    assert result.messages == ()
    assert result.raw_messages == ()
    assert WarningCode.EMPTY_INPUT in result.warning_codes
    assert result.conversation.conversation_key.startswith("import:")


def test_imported_message_and_conversation_are_frozen() -> None:
    conversation = ImportedConversation(conversation_key="import:x", title="标题")
    raw = RawMessage(conversation_key="import:x", sender="张三", is_self=False, text="hi")
    message = ImportedMessage(raw=raw, line_no=1)
    with pytest.raises(Exception):
        conversation.title = "改不了"  # type: ignore[misc]
    assert message.line_no == 1
    assert conversation.masked_dict()["title"]["length"] == len("标题")
