"""邮件 MIME 解析：把 IMAP 取回的原始 RFC822 字节转成纯 Python 数据。

只负责解析，不发网络请求、不写文件、不打日志（调用方负责日志脱敏）。
使用标准库 ``email.policy.default``，覆盖 QQ 邮箱常见情况：
- 中文主题/正文的 RFC2047（``=?UTF-8?B?...?=``）与 quoted-printable / base64 传输编码；
- 无 charset / 声明了错误 charset（回退 utf-8 → gb18030）；
- 只有 HTML 正文（转纯文本）；
- 缺少 Date / 非法 Date；
- 附件只统计数量与字节数，绝不下载或保存附件内容。
"""

from __future__ import annotations

import email
import email.parser
import email.policy
import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

#: 单封邮件正文最多保留的字符数；超出截断并标记（避免 UI/数据库被巨型邮件拖垮）。
DEFAULT_MAX_BODY_CHARS = 20_000

#: 附件只展示文件名（绝不下载/保存/打开内容）：最多收集的个数与每个文件名的字符上限。
MAX_ATTACHMENT_NAMES = 10
MAX_ATTACHMENT_NAME_CHARS = 120

#: 文件名中需要替换掉的控制字符与双向/零宽格式字符（防止 UI 显示错乱/伪装）。
_ATTACHMENT_NAME_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]+"
)

#: 邮件头单值最大长度（防止恶意超长头）。
_MAX_HEADER_CHARS = 500

#: QQ 邮箱常见的中文编码别名。
_CHARSET_ALIASES = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "utf8": "utf-8",
}


@dataclass(frozen=True)
class ParsedMail:
    """解析后的邮件（脱敏前）。正文仅在内存中流转，不得写入日志。"""

    subject: str = ""
    from_name: str = ""
    from_address: str = ""
    to: tuple[str, ...] = ()
    date: datetime | None = None
    date_raw: str | None = None
    message_id: str | None = None
    body_text: str = ""
    content_type: str = ""
    attachment_count: int = 0
    attachment_bytes: int = 0
    #: 附件文件名（只读展示用；最多 MAX_ATTACHMENT_NAMES 个、每个截断 MAX_ATTACHMENT_NAME_CHARS；
    #: 无文件名/纯内嵌附件不收集）。绝不代表附件内容被下载或保存。
    attachment_names: tuple[str, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 文本解码 / HTML 转纯文本
# ---------------------------------------------------------------------------

class _HtmlTextExtractor(HTMLParser):
    """极简 HTML → 纯文本：块级标签换行、跳过 script/style、折叠空白。"""

    _BLOCK_TAGS = frozenset(
        {
            "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
            "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
            "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
            "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
        }
    )
    _SKIP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return _normalize_plain_text("".join(self._parts))


def _normalize_plain_text(text: str) -> str:
    """统一换行、去行尾空白、最多保留一个空行、压缩行内连续空格。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines: list[str] = []
    blank = 0
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if not line:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        lines.append(line)
    return "\n".join(lines).strip()


def html_to_text(html: str) -> str:
    """HTML 片段转纯文本（容错：HTML 畸形时尽量保留可见文本）。"""
    if not html:
        return ""
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
        return parser.get_text()
    except Exception:  # noqa: BLE001 - HTML 解析绝不能中断整个同步
        return _normalize_plain_text(re.sub(r"<[^>]+>", " ", html))


def _normalize_charset(charset: str | None) -> str | None:
    if not charset:
        return None
    name = charset.strip().strip("\"'").lower()
    return _CHARSET_ALIASES.get(name, name)


def _decode_text_part(part: Message, warnings: list[str]) -> str:
    """解码一个 text/* 叶子 part；多级回退策略见模块 docstring。"""
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        try:
            raw_payload = part.get_payload()
        except Exception:  # noqa: BLE001
            raw_payload = None
        if isinstance(raw_payload, str):
            return raw_payload
        warnings.append("无法解码的邮件正文片段")
        return ""
    if not payload:
        return ""
    declared = _normalize_charset(part.get_content_charset())
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    for fallback in ("utf-8", "gb18030"):
        if fallback not in candidates:
            candidates.append(fallback)
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    warnings.append(f"正文字符集无法识别（声明 {declared or '无'}），已用替换字符解码")
    return payload.decode("utf-8", errors="replace")


def _is_attachment(part: Message) -> tuple[bool, str]:
    """判断 part 是否算附件，返回 (是否附件, 文件名或空串)。"""
    try:
        filename = part.get_filename() or ""
    except Exception:  # noqa: BLE001
        filename = ""
    try:
        disposition = str(part.get("content-disposition") or "").lower()
    except Exception:  # noqa: BLE001
        disposition = ""
    try:
        content_type = (part.get_content_type() or "").lower()
    except Exception:  # noqa: BLE001
        content_type = ""
    if filename:
        return True, filename
    if disposition.startswith("attachment"):
        return True, ""
    if content_type and not content_type.startswith("text/"):
        # 非 text/* 的内嵌内容（图片等）按附件计数，但不保存内容。
        return True, ""
    return False, ""


def _sanitize_attachment_name(filename: str) -> str:
    """把附件文件名清洗为可安全展示的文本（只处理文件名本身，不读取附件内容）。

    - 去掉控制字符 / 双向与零宽格式字符（替换为空格）并折叠空白；
    - 每个文件名截断到 ``MAX_ATTACHMENT_NAME_CHARS`` 个字符；
    - 空的（或清洗后为空的）文件名不收集。
    """
    text = _ATTACHMENT_NAME_CONTROL_RE.sub(" ", str(filename or ""))
    text = " ".join(text.split())
    if not text:
        return ""
    return text[:MAX_ATTACHMENT_NAME_CHARS]


def _extract_body(
    msg: Message, max_body_chars: int, warnings: list[str]
) -> tuple[str, str, int, int, tuple[str, ...], bool]:
    """返回 (body_text, content_type, attachment_count, attachment_bytes, attachment_names, truncated)。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachment_count = 0
    attachment_bytes = 0
    attachment_names: list[str] = []

    try:
        parts = list(msg.walk())
    except Exception:  # noqa: BLE001
        parts = [msg]

    for part in parts:
        if part.is_multipart():
            continue
        is_attachment, filename = _is_attachment(part)
        if is_attachment:
            attachment_count += 1
            # 只收集 MIME 头里的文件名用于展示；附件内容只统计字节数，绝不解码保存/打开。
            sanitized = _sanitize_attachment_name(filename)
            if sanitized and len(attachment_names) < MAX_ATTACHMENT_NAMES:
                attachment_names.append(sanitized)
            try:
                payload = part.get_payload(decode=True)
            except Exception:  # noqa: BLE001
                payload = None
            attachment_bytes += len(payload) if isinstance(payload, (bytes, bytearray)) else 0
            continue
        try:
            content_type = (part.get_content_type() or "").lower()
        except Exception:  # noqa: BLE001
            content_type = ""
        if content_type == "text/html":
            html_parts.append(_decode_text_part(part, warnings))
        elif content_type.startswith("text/"):
            plain_parts.append(_decode_text_part(part, warnings))
        else:
            # 既不是附件（没有文件名/attachment），也不是 text/*：跳过，避免下载未知内容。
            warnings.append("跳过未知 MIME 片段")

    content_type = "text/plain" if plain_parts else ("text/html" if html_parts else "")
    if plain_parts:
        body = "\n\n".join(part for part in plain_parts if part.strip())
        body = _normalize_plain_text(body)
    elif html_parts:
        body = html_to_text("\n".join(html_parts))
    else:
        body = ""
        warnings.append("无文本正文（可能仅含附件或非文本内容）")

    truncated = len(body) > max_body_chars
    if truncated:
        body = body[:max_body_chars].rstrip() + "\n…（正文过长，已截断）"
    return (
        body,
        content_type,
        attachment_count,
        attachment_bytes,
        tuple(attachment_names),
        truncated,
    )


# ---------------------------------------------------------------------------
# 邮件头解析
# ---------------------------------------------------------------------------

def _safe_header(msg: Message, name: str) -> str:
    """读取单个邮件头并折叠空白；个别畸形 RFC2047 头会让 str() 抛异常。"""
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001
        return ""
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001
        text = _decode_header_fallback(value)
    return " ".join(text.split())[:_MAX_HEADER_CHARS]


def _decode_header_fallback(value: object) -> str:
    """``str(header)`` 失败时的兜底：用 email.header.decode_header 手工拼接。"""
    try:
        chunks = decode_header(str(value))
    except Exception:  # noqa: BLE001
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return ""
    out: list[str] = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(_normalize_charset(charset) or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _parse_addresses(msg: Message, header_name: str, warnings: list[str]) -> tuple[str, str]:
    """返回 (显示名, 地址)。优先显示名，没有显示名时用地址。"""
    raw = _safe_header(msg, header_name)
    if not raw:
        return "", ""
    try:
        pairs = getaddresses([raw])
    except Exception:  # noqa: BLE001
        warnings.append(f"{header_name} 邮件头无法解析")
        return "", ""
    for display_name, address in pairs:
        address = (address or "").strip()
        display_name = (display_name or "").strip()
        if address or display_name:
            return display_name, address
    warnings.append(f"{header_name} 邮件头为空")
    return "", ""


def _parse_to(msg: Message, warnings: list[str]) -> tuple[str, ...]:
    raw = _safe_header(msg, "To")
    if not raw:
        return ()
    try:
        pairs = getaddresses([raw])
    except Exception:  # noqa: BLE001
        warnings.append("To 邮件头无法解析")
        return ()
    return tuple(address.strip() for _name, address in pairs if (address or "").strip())


def _parse_date(msg: Message, local_tz: tzinfo | None, warnings: list[str]) -> tuple[datetime | None, str | None]:
    raw = _safe_header(msg, "Date")
    if not raw:
        warnings.append("缺少 Date 邮件头，使用采集时间并标注 timestamp_source=COLLECTED")
        return None, None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        warnings.append("Date 邮件头格式非法，使用采集时间并标注 timestamp_source=COLLECTED")
        return None, raw
    if parsed is None:
        warnings.append("Date 邮件头为空，使用采集时间并标注 timestamp_source=COLLECTED")
        return None, raw
    if parsed.tzinfo is None:
        # 邮件头缺时区：按本机时区解释（团队约定所有时间用本地时区），并留警告。
        tz = local_tz if local_tz is not None else datetime.now().astimezone().tzinfo
        parsed = parsed.replace(tzinfo=tz)
        warnings.append("Date 邮件头缺少时区，已按本机时区解释")
    return parsed, raw


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def parse_email(
    raw: bytes,
    *,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    local_tz: tzinfo | None = None,
) -> ParsedMail:
    """解析一封 RFC822 原始邮件。任何异常都会降级为带 warnings 的部分结果。"""
    if max_body_chars < 0:
        raise ValueError("max_body_chars 不能为负数")
    warnings: list[str] = []
    try:
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    except Exception:  # noqa: BLE001
        # 极端畸形邮件：用 compat32 再试一次，仍失败则返回空壳并标记。
        warnings.append("邮件 MIME 结构畸形，已降级解析")
        try:
            msg = email.parser.BytesParser().parsebytes(raw)
        except Exception:  # noqa: BLE001
            return ParsedMail(warnings=tuple(warnings))

    subject = _safe_header(msg, "Subject")
    from_name, from_address = _parse_addresses(msg, "From", warnings)
    to = _parse_to(msg, warnings)
    date, date_raw = _parse_date(msg, local_tz, warnings)
    message_id = _safe_header(msg, "Message-ID") or None

    try:
        (
            body,
            content_type,
            attachment_count,
            attachment_bytes,
            attachment_names,
            truncated,
        ) = _extract_body(msg, max_body_chars, warnings)
    except Exception:  # noqa: BLE001
        warnings.append("正文解析失败")
        body, content_type, attachment_count, attachment_bytes = "", "", 0, 0
        attachment_names: tuple[str, ...] = ()
        truncated = False

    return ParsedMail(
        subject=subject,
        from_name=from_name,
        from_address=from_address,
        to=to,
        date=date,
        date_raw=date_raw,
        message_id=message_id,
        body_text=body,
        content_type=content_type,
        attachment_count=attachment_count,
        attachment_bytes=attachment_bytes,
        attachment_names=tuple(attachment_names),
        truncated=truncated,
        warnings=tuple(warnings),
    )


__all__ = [
    "ParsedMail",
    "parse_email",
    "html_to_text",
    "DEFAULT_MAX_BODY_CHARS",
]
