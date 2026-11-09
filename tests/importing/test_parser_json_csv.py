"""JSON / CSV / 解码 解析测试（合成文本）。"""

from __future__ import annotations

import json
from datetime import timedelta

from social_secretary.importing import ImportFormat, WarningCode
from social_secretary.importing.parser import (
    decode_text_bytes,
    parse_chat_bytes,
    parse_chat_text,
)

from .conftest import assert_local_aware


def test_json_list_basic(make_options) -> None:
    payload = [
        {"sender": "张三", "text": "你好", "timestamp": "2024-01-01 12:30:00"},
        {"sender": "我", "content": "收到", "time": "2024-01-01 12:31:00"},
    ]
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    assert result.format is ImportFormat.JSON
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[1].raw.sender == "我"
    assert result.messages[1].raw.is_self is True
    assert_local_aware(result.messages[0].raw.timestamp)


def test_json_object_with_messages_and_title(make_options) -> None:
    payload = {
        "title": "家庭群",
        "messages": [
            {"sender": "妈妈", "text": "吃饭了吗"},
            {"sender": "我", "text": "吃了"},
        ],
    }
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    assert len(result.messages) == 2
    assert result.conversation.title == "家庭群"


def test_json_epoch_timestamps_seconds_and_milliseconds(make_options) -> None:
    payload = [
        {"sender": "张三", "text": "秒级", "timestamp": 1704083400},
        {"sender": "李四", "text": "毫秒级", "timestamp": 1704083460000},
    ]
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    first, second = result.messages
    assert first.raw.timestamp is not None and second.raw.timestamp is not None
    assert first.raw.timestamp.utcoffset() == timedelta(hours=8)
    assert (first.raw.timestamp.hour, first.raw.timestamp.minute) == (12, 30)
    assert (second.raw.timestamp.hour, second.raw.timestamp.minute) == (12, 31)
    assert first.raw.timestamp_source.value == "ui"


def test_jsonl_format(make_options) -> None:
    text = (
        '{"sender": "张三", "text": "第一行", "timestamp": "2024-01-01 12:30:00"}\n'
        '{"sender": "李四", "text": "第二行", "timestamp": "2024-01-01 12:31:00"}\n'
    )
    result = parse_chat_text(text, options=make_options())
    assert result.format is ImportFormat.JSON
    assert len(result.messages) == 2
    assert result.messages[1].raw.text == "第二行"


def test_json_structured_fields(make_options) -> None:
    payload = [
        {
            "sender": {"nickname": "张三"},
            "text": ["你好，", "在吗"],
            "timestamp": {"seconds": 1704083400},
            "is_self": False,
        },
        {"from": "我", "msg": "在", "date": "2024-01-01 12:31:00", "self": True},
    ]
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[0].raw.text == "你好，在吗"
    assert result.messages[0].raw.is_self is False
    assert result.messages[1].raw.sender == "我"
    assert result.messages[1].raw.is_self is True


def test_json_invalid_syntax_returns_empty_with_warning(make_options) -> None:
    result = parse_chat_text("[{sender: 张三, text: 坏 JSON}]", options=make_options(format_hint="json"))
    assert result.ok is False
    assert result.format is ImportFormat.JSON
    assert WarningCode.JSON_PARSE_FAILED in result.warning_codes
    assert WarningCode.NO_MESSAGES in result.warning_codes


def test_json_invalid_records_are_skipped(make_options) -> None:
    payload = ["不是对象", {"sender": "张三"}, {"sender": "李四", "text": "正常"}]
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    assert len(result.messages) == 1
    assert result.messages[0].raw.text == "正常"
    assert result.stats.skipped_lines == 2
    assert WarningCode.JSON_RECORD_INVALID in result.warning_codes


def test_json_invalid_timestamp_untimed(make_options) -> None:
    payload = [{"sender": "张三", "text": "时间坏了", "timestamp": "不是时间"}]
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    assert len(result.messages) == 1
    assert result.messages[0].raw.timestamp is None
    assert result.stats.invalid_timestamps == 1
    assert WarningCode.INVALID_TIMESTAMP in result.warning_codes


def test_json_missing_sender_uses_placeholder(make_options) -> None:
    payload = [{"text": "没有发送者"}]
    result = parse_chat_text(json.dumps(payload, ensure_ascii=False), options=make_options())
    assert len(result.messages) == 1
    assert result.messages[0].raw.sender == "未知"
    assert WarningCode.JSON_RECORD_INVALID in result.warning_codes


def test_csv_with_header(make_options) -> None:
    text = (
        "sender,text,timestamp\n"
        "张三,你好,2024-01-01 12:30:00\n"
        "我,收到,2024-01-01 12:31:00\n"
    )
    result = parse_chat_text(text, options=make_options())
    assert result.format is ImportFormat.CSV
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[1].raw.is_self is True
    assert result.messages[1].raw.timestamp.hour == 12


def test_csv_headerless_assumed(make_options) -> None:
    text = "张三,你好\n李四,收到\n"
    result = parse_chat_text(text, options=make_options(format_hint="csv"))
    assert len(result.messages) == 2
    assert result.messages[0].raw.sender == "张三"
    assert result.messages[0].raw.text == "你好"
    assert WarningCode.CSV_HEADER_ASSUMED in result.warning_codes


def test_csv_quoted_multiline_text(make_options) -> None:
    text = 'sender,text,timestamp\n张三,"第一行\n第二行",2024-01-01 12:30:00\n'
    result = parse_chat_text(text, options=make_options())
    assert len(result.messages) == 1
    assert result.messages[0].raw.text == "第一行\n第二行"


def test_csv_invalid_content_returns_empty_with_warning(make_options) -> None:
    result = parse_chat_text("只有一列\n还是只有一列\n", options=make_options(format_hint="csv"))
    assert result.ok is False
    assert WarningCode.CSV_PARSE_FAILED in result.warning_codes


def test_decode_utf8_bom() -> None:
    decoded = decode_text_bytes("\ufeff你好，世界".encode("utf-8"))
    assert decoded.text == "你好，世界"
    assert decoded.encoding == "utf-8-sig"


def test_decode_gbk_fallback() -> None:
    decoded = decode_text_bytes("张三：你好".encode("gbk"))
    assert decoded.text == "张三：你好"
    assert decoded.encoding in ("gb18030", "gbk")


def test_decode_invalid_bytes_replacement() -> None:
    decoded = decode_text_bytes(b"\xff\xfe\x00\x00\xff\xff\xff", preferred="utf-8")
    assert decoded.replaced_chars >= 0
    assert decoded.encoding


def test_parse_chat_bytes_reports_encoding(make_options) -> None:
    data = "2024-01-01 12:30:00 张三: 你好\n".encode("gbk")
    result = parse_chat_bytes(data, options=make_options())
    assert len(result.messages) == 1
    assert result.messages[0].raw.text == "你好"
    assert result.encoding.lower().startswith(("gb", "gb18030"))


def test_format_auto_detects_json_and_csv(make_options) -> None:
    json_text = json.dumps([{"sender": "张三", "text": "hi"}], ensure_ascii=False)
    assert parse_chat_text(json_text, options=make_options()).format is ImportFormat.JSON
    csv_text = "sender,text,timestamp\n张三,hi,2024-01-01 12:00:00\n"
    assert parse_chat_text(csv_text, options=make_options()).format is ImportFormat.CSV
