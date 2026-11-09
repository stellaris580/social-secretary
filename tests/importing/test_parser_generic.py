"""通用行格式解析测试（合成文本）。"""

from __future__ import annotations

from datetime import timedelta

from social_secretary.importing import ImportFormat, WarningCode
from social_secretary.importing.parser import parse_chat_text

from .conftest import assert_local_aware


def test_basic_full_datetime_sender_colon(make_options) -> None:
    result = parse_chat_text(
        "2024-01-01 12:30:00 张三: 你好\n2024-01-01 12:31:00 李四: 收到",
        options=make_options(conversation_title="项目群", conversation_kind="group"),
    )
    assert result.format is ImportFormat.GENERIC
    assert len(result.messages) == 2

    first = result.messages[0]
    assert first.line_no == 1
    assert first.raw.conversation_key.startswith("import:")
    assert first.raw.sender == "张三"
    assert first.raw.is_self is False
    assert first.raw.text == "你好"
    assert_local_aware(first.raw.timestamp)
    assert first.raw.timestamp.hour == 12 and first.raw.timestamp.minute == 30
    assert first.raw.timestamp_source.value == "ui"
    assert first.raw.source_locator == "import:test_chat.txt:1"
    assert first.raw.extras["imported"] is True
    assert first.raw.extras["import_format"] == "generic"
    assert result.conversation.title == "项目群"
    assert result.conversation.kind.value == "group"


def test_bracket_datetime_and_chinese_date(make_options) -> None:
    text = "[2024-01-01 12:31] 李四: 收到\n2024年1月1日 12:32 王五: 在吗"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "李四"
    assert result.messages[1].raw.sender == "王五"
    assert result.messages[1].raw.timestamp.month == 1


def test_date_separator_then_clock_only(make_options, fixed_now) -> None:
    text = "2024年1月1日 星期一\n12:30 张三: 早\n12:31 李四: 早"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    for message in result.messages:
        assert message.raw.timestamp.date().isoformat() == "2024-01-01"
        assert message.raw.timestamp_source.value == "ui"
    assert result.messages[0].raw.timestamp.hour == 12


def test_sender_time_header_with_multiline_content(make_options) -> None:
    text = "张三 2024-01-01 12:30:00\n你好\n第二行\n\n李四 2024-01-01 12:31:00\n收到"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[0].raw.text == "你好\n第二行"
    assert result.messages[1].raw.sender == "李四"
    assert result.messages[1].raw.text == "收到"


def test_multiline_continuation_with_first_line_text(make_options) -> None:
    text = "2024-01-01 12:30:00 张三: 第一行\n第二行\n  第三行保留缩进\n2024-01-01 12:31:00 李四: 收到"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.messages[0].raw.text == "第一行\n第二行\n  第三行保留缩进"
    assert result.messages[1].raw.text == "收到"
    assert result.stats.parsed_messages == 2


def test_multiple_senders_and_self_detection(make_options) -> None:
    text = (
        "2024-01-01 12:00:00 我: 我去开会\n"
        "2024-01-01 12:01:00 张三: 好的\n"
        "2024-01-01 12:02:00 李四: 收到\n"
        "2024-01-01 12:03:00 我: 晚点聊"
    )
    result = parse_chat_text(text, options=make_options())
    assert result.stats.self_messages == 2
    assert result.stats.other_messages == 2
    assert result.stats.sender_count == 3
    assert result.messages[0].raw.is_self is True
    assert result.messages[1].raw.is_self is False
    assert WarningCode.SELF_SENDER_ASSUMED in result.warning_codes


def test_custom_self_sender_names(make_options) -> None:
    text = "2024-01-01 12:00:00 小助手: 你好\n2024-01-01 12:01:00 张三: 在吗"
    result = parse_chat_text(
        text,
        options=make_options(self_sender_names=("小助手",)),
    )
    assert result.messages[0].raw.is_self is True
    assert result.messages[1].raw.is_self is False


def test_pending_datetime_line_then_sender(make_options) -> None:
    text = "2024-01-01 12:30:00\n张三: 你好\n2024-01-01 12:31:00\n李四: 收到"
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[0].raw.timestamp.hour == 12
    assert result.messages[1].raw.sender == "李四"


def test_sender_colon_without_time_is_untimed(make_options) -> None:
    result = parse_chat_text("张三: 没有时间\n李四: 也没有", options=make_options())
    assert len(result.messages) == 2
    assert result.messages[0].raw.timestamp is None
    assert result.messages[0].raw.timestamp_source.value == "collected"
    assert result.stats.untimed_messages == 2
    assert WarningCode.MISSING_TIMESTAMP in result.warning_codes


def test_heading_and_separator_lines_are_skipped(make_options) -> None:
    text = (
        "消息记录\n"
        "====================\n"
        "2024-01-01 12:30:00 张三: 你好\n"
        "2024-01-01 12:31:00 李四: 收到\n"
        "以上是聊天记录"
    )
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 2
    assert result.stats.skipped_lines == 0
    assert WarningCode.UNRECOGNIZED_LINE not in result.warning_codes


def test_junk_lines_produce_warnings_not_crash(make_options) -> None:
    result = parse_chat_text(
        "这不是聊天记录\n随便写点\n\ufffd坏字符",
        options=make_options(),
    )
    assert result.ok is False
    assert result.stats.skipped_lines == 3
    assert WarningCode.UNRECOGNIZED_LINE in result.warning_codes
    assert WarningCode.FORMAT_UNKNOWN in result.warning_codes
    assert WarningCode.MOJIBAKE_SUSPECTED in result.warning_codes
    assert WarningCode.NO_MESSAGES in result.warning_codes


def test_stats_and_parse_success_rate(make_options) -> None:
    text = "2024-01-01 12:30:00 张三: 你好\n无归属的杂行\n2024-01-01 12:31:00 李四: 收到"
    # “无归属的杂行”紧跟在第一条消息后，按续行处理；用独立短文本验证跳过行统计
    result = parse_chat_text(text, options=make_options())
    assert result.stats.parsed_messages == 2
    assert result.stats.skipped_lines == 0
    assert result.stats.parse_success_rate > 0.9
    assert result.stats.elapsed_ms >= 0
    assert result.stats.input_lines == 3
