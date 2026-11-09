"""日志脱敏测试：redact_text/hash_text、过滤器、JSONL 日志文件不出现明文密钥。"""

from __future__ import annotations

import json
import logging

import pytest

from social_secretary.security.log_redaction import (
    REDACTED,
    RedactionFilter,
    hash_text,
    looks_like_secret,
    redact_text,
    safe_error,
)
from social_secretary.security.logging_setup import (
    LOG_FILE_NAME,
    configure_logging,
    reset_logging,
)

AUTH_CODE = "abcdefghijklmnop"
API_KEY = "sk-testplaceholder1234567890"


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        f"api_key={API_KEY}",
        f"api-key: {API_KEY}",
        f"token={API_KEY}",
        "Authorization: Bearer abcdefghijklmnop",
        f"邮箱授权码：{AUTH_CODE}",
        f"auth_code={AUTH_CODE}",
        f"password=super-secret-value",
    ],
)
def test_redact_text_masks_secrets(text: str) -> None:
    redacted = redact_text(text)
    for secret in (AUTH_CODE, API_KEY, "super-secret-value", "abcdefghijklmnop"):
        assert secret not in redacted
    assert REDACTED in redacted


def test_redact_text_keeps_normal_text_and_truncates() -> None:
    assert redact_text("今天需要整理 5 条消息") == "今天需要整理 5 条消息"
    long_text = "消息" * 300
    truncated = redact_text(long_text, max_len=20)
    assert len(truncated) <= 21
    assert truncated.endswith("…")
    assert redact_text(None) == ""


def test_hash_text_is_stable_and_not_reversible() -> None:
    first = hash_text("这是消息正文")
    assert first == hash_text("这是消息正文")
    assert first != hash_text("另一条消息")
    assert "消息" not in first
    assert first.startswith("sha256:")


@pytest.mark.parametrize(
    "value,expected",
    [
        (API_KEY, True),
        (AUTH_CODE, True),
        ("Bearer abcdefghijkl", True),
        ("api_key=xyz", True),
        ("deepseek-chat", False),
        ("https://api.deepseek.com/v1", False),
        ("INFO", False),
    ],
)
def test_looks_like_secret(value: str, expected: bool) -> None:
    assert looks_like_secret(value) is expected


def test_safe_error_redacts_and_truncates() -> None:
    error = safe_error(RuntimeError(f"登录失败 creds={API_KEY}"), max_len=80)
    try:
        raise ValueError("x" * 500)
    except ValueError as exc:
        truncated = safe_error(exc)
    assert API_KEY not in error
    assert "RuntimeError" in error
    assert len(truncated) < 300


# ---------------------------------------------------------------------------
# logging filter / handler
# ---------------------------------------------------------------------------

def test_redaction_filter_masks_message_and_args() -> None:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("ss_filter_test")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = _Capture()
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    try:
        logger.info("api_key=%s", API_KEY)
        logger.info("邮箱授权码：%s", AUTH_CODE)
    finally:
        logger.handlers.clear()

    assert len(records) == 2
    assert API_KEY not in records[0].getMessage()
    assert AUTH_CODE not in records[1].getMessage()


def test_configure_logging_writes_jsonl_without_secrets(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logger = configure_logging(logs_dir, level="DEBUG", logger_name="ss_test_jsonl")
    try:
        logger.info("scan_finished", extra={"event_data": {"message_count": 3}})
        logger.warning("api_key=%s", API_KEY)
        child = logging.getLogger("ss_test_jsonl.child")
        child.info("邮箱授权码：%s", AUTH_CODE)
        # event_data 中的正文类字段必须脱敏
        logger.info(
            "draft_event",
            extra={"event_data": {"text": "这是私密消息正文", "draft": "这是草稿正文"}},
        )
    finally:
        reset_logging("ss_test_jsonl")

    log_file = logs_dir / LOG_FILE_NAME
    assert log_file.exists()
    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line]
    assert lines
    payloads = [json.loads(line) for line in lines]
    raw_file = log_file.read_text(encoding="utf-8")

    assert API_KEY not in raw_file
    assert AUTH_CODE not in raw_file
    assert "私密消息正文" not in raw_file
    assert "草稿正文" not in raw_file
    for payload in payloads:
        assert {"ts", "level", "logger", "event"} <= set(payload)
    counts = [
        payload["data"]["message_count"]
        for payload in payloads
        if payload.get("data", {}).get("message_count") == 3
    ]
    assert counts == [3]


def test_configure_logging_is_idempotent(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    configure_logging(logs_dir, logger_name="ss_test_idem")
    configure_logging(logs_dir, logger_name="ss_test_idem")
    logger = logging.getLogger("ss_test_idem")
    try:
        assert len(logger.handlers) == 1
        logger.info("only once")
    finally:
        reset_logging("ss_test_idem")
    lines = (
        (logs_dir / LOG_FILE_NAME).read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(lines) == 1
