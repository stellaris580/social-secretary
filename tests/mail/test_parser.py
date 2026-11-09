"""MIME 解析单元测试（全部使用合成邮件，不含真实邮件正文）。"""

from __future__ import annotations

import base64
from email.message import EmailMessage

from social_secretary.collectors.mail import html_to_text, parse_email
from social_secretary.collectors.mail.parser import (
    MAX_ATTACHMENT_NAME_CHARS,
    MAX_ATTACHMENT_NAMES,
    _sanitize_attachment_name,
)

from .helpers import make_email


def test_parse_plain_chinese_email():
    raw = make_email(
        subject="项目进度：请在周五前确认",
        body="王老师好：\n\n请在周五前确认下周安排。\n\n谢谢！",
        from_addr="teacher@example.com",
        from_name="王老师",
    )
    parsed = parse_email(raw)
    assert parsed.subject == "项目进度：请在周五前确认"
    assert parsed.from_name == "王老师"
    assert parsed.from_address == "teacher@example.com"
    assert parsed.to == ("test@qq.com",)
    assert "请在周五前确认" in parsed.body_text
    assert parsed.date is not None
    assert parsed.date.tzinfo is not None
    assert parsed.date.utcoffset().total_seconds() == 8 * 3600
    assert parsed.message_id == "<synthetic-001@example.com>"
    assert parsed.attachment_count == 0
    assert parsed.content_type == "text/plain"


def test_parse_rfc2047_encoded_headers_and_base64_body():
    subject = "你好，测试"
    display_name = "张三"
    body = "这是一封 base64 编码的中文正文。"
    raw = (
        "From: =?UTF-8?B?"
        + base64.b64encode(display_name.encode()).decode()
        + '?= <zhangsan@example.com>\r\n'
        "To: test@qq.com\r\n"
        "Subject: =?UTF-8?B?" + base64.b64encode(subject.encode()).decode() + "?=\r\n"
        "Date: Wed, 29 Oct 2025 09:30:00 +0800\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n" + base64.b64encode(body.encode()).decode() + "\r\n"
    ).encode()
    parsed = parse_email(raw)
    assert parsed.subject == subject
    assert parsed.from_name == display_name
    assert parsed.from_address == "zhangsan@example.com"
    assert parsed.body_text == body


def test_parse_html_only_body_converted_to_text():
    raw = (
        "<html><head><style>p{color:red}</style><script>alert('x')</script></head>"
        "<body><p>请在 <b>周五</b> 前确认安排。</p><div>第二段：附件见下。</div></body></html>"
    ).encode()
    parsed = parse_email(raw)
    # 没有 MIME 头的裸 HTML 会被 parser 视为 text/plain；显式构造 text/html 才能验证转换。
    raw_html = (
        "From: sender@example.com\r\n"
        "To: test@qq.com\r\n"
        "Subject: HTML 邮件\r\n"
        "Date: Wed, 29 Oct 2025 09:30:00 +0800\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n" + raw.decode("utf-8")
    ).encode()
    parsed = parse_email(raw_html)
    assert "周五" in parsed.body_text
    assert "第二段" in parsed.body_text
    assert "<b>" not in parsed.body_text
    assert "alert" not in parsed.body_text
    assert "color:red" not in parsed.body_text
    assert parsed.content_type == "text/html"


def test_parse_multipart_alternative_prefers_plain():
    raw = make_email(
        subject="双格式邮件",
        body="纯文本版本：请确认。",
        html_body="<html><body><p>HTML 版本：请确认。</p></body></html>",
    )
    parsed = parse_email(raw)
    assert "纯文本版本：请确认。" in parsed.body_text
    assert "<p>" not in parsed.body_text
    assert parsed.content_type == "text/plain"


def test_parse_attachment_only_records_count_and_size():
    attachment_bytes = b"%PDF-1.4 synthetic attachment"
    raw = make_email(
        subject="带附件邮件",
        body="见附件。",
        attachment=("合成报告.pdf", attachment_bytes, "application", "pdf"),
    )
    parsed = parse_email(raw)
    assert parsed.attachment_count == 1
    assert parsed.attachment_bytes == len(attachment_bytes)
    assert parsed.attachment_names == ("合成报告.pdf",)
    assert "合成报告.pdf" not in parsed.body_text
    assert "PDF" not in parsed.body_text


def test_parse_attachment_names_chinese_and_without_filename():
    """P0-19：只从 MIME 头收集文件名（含中文名；无文件名的附件不收集），不读取附件内容。"""
    message = EmailMessage()
    message["Subject"] = "多附件邮件"
    message["From"] = "sender@example.com"
    message["To"] = "test@qq.com"
    message["Date"] = "Wed, 29 Oct 2025 09:30:00 +0800"
    message.set_content("正文：见附件。")
    message.add_attachment(b"pdf-content", maintype="application", subtype="pdf", filename="合成报告.pdf")
    message.add_attachment(b"png-content", maintype="image", subtype="png", filename="图片 截图.png")
    message.add_attachment(b"nameless", maintype="application", subtype="octet-stream")

    parsed = parse_email(message.as_bytes())

    assert parsed.attachment_count == 3
    assert parsed.attachment_bytes == len(b"pdf-content") + len(b"png-content") + len(b"nameless")
    assert parsed.attachment_names == ("合成报告.pdf", "图片 截图.png")  # 无文件名的不收集
    assert "pdf-content" not in parsed.body_text
    assert "png-content" not in parsed.body_text


def test_parse_attachment_names_truncated_and_limited():
    """P0-19：最多收集 10 个文件名，每个截断到 120 字符。"""
    message = EmailMessage()
    message["Subject"] = "很多附件"
    message["From"] = "sender@example.com"
    message["To"] = "test@qq.com"
    message["Date"] = "Wed, 29 Oct 2025 09:30:00 +0800"
    message.set_content("正文。")
    long_name = "长" * (MAX_ATTACHMENT_NAME_CHARS + 30) + ".txt"
    message.add_attachment(b"long", maintype="application", subtype="octet-stream", filename=long_name)
    for index in range(MAX_ATTACHMENT_NAMES + 2):
        message.add_attachment(
            f"file-{index}".encode(),
            maintype="application",
            subtype="octet-stream",
            filename=f"附件-{index}.bin",
        )

    parsed = parse_email(message.as_bytes())

    total = 1 + MAX_ATTACHMENT_NAMES + 2
    assert parsed.attachment_count == total
    assert len(parsed.attachment_names) == MAX_ATTACHMENT_NAMES
    assert parsed.attachment_names[0] == "长" * MAX_ATTACHMENT_NAME_CHARS
    assert len(parsed.attachment_names[0]) == MAX_ATTACHMENT_NAME_CHARS
    # 只保留前 10 个有文件名的附件（长名 + 附件-0..附件-8）
    assert parsed.attachment_names[1] == "附件-0.bin"
    assert parsed.attachment_names[-1] == "附件-8.bin"


def test_attachment_name_sanitizer_strips_control_and_format_chars():
    """P0-19：文件名展示前必须去掉控制字符/双向格式字符，并截断长度。"""
    assert _sanitize_attachment_name("") == ""
    assert _sanitize_attachment_name("   ") == ""
    assert _sanitize_attachment_name("bad\x00na\nme\u202e.txt") == "bad na me .txt"
    assert _sanitize_attachment_name("a" * 500) == "a" * MAX_ATTACHMENT_NAME_CHARS
    # 中英文正常文件名保持原样（仅折叠多余空白）
    assert _sanitize_attachment_name("  季度   报告.pdf ") == "季度 报告.pdf"


def test_parse_missing_date_header():
    raw = make_email(subject="无日期", omit_date=True)
    parsed = parse_email(raw)
    assert parsed.date is None
    assert parsed.date_raw is None
    assert any("Date" in warning for warning in parsed.warnings)


def test_parse_invalid_date_header():
    # EmailMessage 的 DateHeader 会丢弃非法 Date 头，所以这里手工构造原始邮件。
    raw = (
        "From: sender@example.com\r\n"
        "To: test@qq.com\r\n"
        "Subject: 非法日期\r\n"
        "Date: not-a-real-date\r\n"
        "Message-ID: <bad-date@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        "正文。\r\n"
    ).encode()
    parsed = parse_email(raw)
    assert parsed.date is None
    assert parsed.date_raw == "not-a-real-date"
    assert any("Date" in warning for warning in parsed.warnings)


def test_parse_date_without_timezone_uses_local_tz():
    raw = make_email(subject="无时区日期", date="Wed, 29 Oct 2025 09:30:00")
    parsed = parse_email(raw)
    assert parsed.date is not None
    assert parsed.date.tzinfo is not None
    assert any("时区" in warning for warning in parsed.warnings)


def test_parse_body_truncation():
    raw = make_email(subject="超长正文", body="字" * 500)
    parsed = parse_email(raw, max_body_chars=50)
    assert parsed.truncated is True
    assert "已截断" in parsed.body_text
    assert len(parsed.body_text) < 200


def test_parse_unknown_charset_falls_back():
    payload = "中文内容".encode("utf-8")
    raw = (
        "From: sender@example.com\r\n"
        "Subject: unknown charset\r\n"
        "Date: Wed, 29 Oct 2025 09:30:00 +0800\r\n"
        'Content-Type: text/plain; charset="x-unknown-charset"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n" + base64.b64encode(payload).decode() + "\r\n"
    ).encode()
    parsed = parse_email(raw)
    # 声明的 charset 未知时回退 utf-8 / gb18030，正文不能是乱码。
    assert "中文内容" in parsed.body_text


def test_parse_gb18030_body():
    payload = "用国标编码写的中文正文".encode("gb18030")
    raw = (
        "From: sender@example.com\r\n"
        "Subject: gb18030\r\n"
        "Date: Wed, 29 Oct 2025 09:30:00 +0800\r\n"
        'Content-Type: text/plain; charset="gb2312"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n" + base64.b64encode(payload).decode() + "\r\n"
    ).encode()
    parsed = parse_email(raw)
    assert "国标编码" in parsed.body_text


def test_html_to_text_normalizes_whitespace():
    text = html_to_text("<div>第一行&nbsp;内容</div><br><p>  第二行  </p>")
    assert "第一行 内容" in text
    assert "第二行" in text
    assert "\n" in text


def test_parse_empty_message_does_not_raise():
    parsed = parse_email(b"")
    assert parsed.subject == ""
    assert parsed.from_address == ""
    assert parsed.body_text == ""
