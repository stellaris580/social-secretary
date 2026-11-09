"""QQ 复制/导出常见文本样式测试（全部为合成示例，不代表真实 QQ 导出格式）。"""

from __future__ import annotations

from social_secretary.importing import ImportFormat, WarningCode
from social_secretary.importing.parser import parse_chat_text

from .conftest import assert_local_aware

QQ_BANNER = "消息记录（与 张三 的聊天记录）\n2024年1月1日 星期一\n"

QQ_SAMPLE = (
    QQ_BANNER
    + "2024-01-01 12:30:00 张三\n"
    + "你好\n"
    + "这是第二条消息\n"
    + "\n"
    + "2024-01-01 12:30:10 李四\n"
    + "收到\n"
)


def test_qq_banner_title_and_sender_time_style(make_options) -> None:
    result = parse_chat_text(QQ_SAMPLE, options=make_options())
    assert result.format is ImportFormat.QQ_COPY
    assert result.conversation.title == "张三"
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[0].raw.text == "你好\n这是第二条消息"
    assert result.messages[1].raw.sender == "李四"
    assert result.messages[1].raw.text == "收到"
    assert_local_aware(result.messages[0].raw.timestamp)
    assert result.messages[0].raw.extras["qq_style"] == "qq_time_sender"


def test_qq_group_title_and_datetime_sender_variant(make_options) -> None:
    text = (
        "消息记录（群聊：项目组）\n"
        "2024-01-01 12:30:00 张三\n"
        "你好\n"
        "2024-01-01 12:31:00 李四\n"
        "收到\n"
    )
    result = parse_chat_text(text, options=make_options())
    assert result.conversation.title == "项目组"
    assert len(result.messages) == 2


def test_qq_time_sender_variant(make_options) -> None:
    text = "消息记录\n2024-01-01 12:30:00\n张三\n你好\n"
    # 本原型尚不支持“时间行 + 昵称行 + 内容行”三行式；应给 warning 而不是崩溃。
    result = parse_chat_text(text, options=make_options())
    assert result.format is ImportFormat.QQ_COPY
    assert len(result.messages) <= 1
    assert WarningCode.UNRECOGNIZED_LINE in result.warning_codes or result.messages


def test_qq_export_style_with_classic_banner_and_date_separator(make_options) -> None:
    text = (
        "=== 消息记录 ===\n"
        "2024-01-01 星期一\n"
        "10:00 张三: 早\n"
        "10:01 李四: 早\n"
    )
    result = parse_chat_text(text, options=make_options(), format_hint="qq_copy")
    assert result.format is ImportFormat.QQ_COPY
    assert len(result.messages) == 2
    assert result.messages[0].raw.timestamp.date().isoformat() == "2024-01-01"
    assert result.messages[0].raw.timestamp.hour == 10


def test_format_auto_detects_qq_marker(make_options) -> None:
    result = parse_chat_text(QQ_SAMPLE, options=make_options(format_hint="auto"))
    assert result.format is ImportFormat.QQ_COPY


def test_format_hint_generic_on_qq_like_text_still_parses(make_options) -> None:
    # 手动选择“通用行格式”时，QQ 风格的头行也走同一套启发式。
    result = parse_chat_text(QQ_SAMPLE, options=make_options(format_hint="generic"))
    assert result.format is ImportFormat.GENERIC
    assert len(result.messages) == 2


def test_qq_dedup_same_message(make_options) -> None:
    text = (
        "消息记录（与 张三 的聊天记录）\n"
        "2024-01-01 12:30:00 张三\n"
        "你好\n"
        "2024-01-01 12:30:00 张三\n"
        "你好\n"
    )
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 1
    assert result.stats.duplicate_messages == 1
    assert WarningCode.DUPLICATES_REMOVED in result.warning_codes
