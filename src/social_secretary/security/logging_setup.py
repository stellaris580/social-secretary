"""JSONL 滚动日志配置。

- 日志文件：``<data_dir>/logs/app.jsonl``，每行一个 JSON 对象，便于诊断与证据归档。
- 所有 handler 挂 ``RedactionFilter``；formatter 再做一次兜底脱敏。
- 只允许记录事件名、id、计数、哈希、耗时；消息正文/授权码/API Key 一律不得进入日志。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .audit import sanitize_detail
from .log_redaction import RedactionFilter, redact_text

LOG_FILE_NAME = "app.jsonl"
DEFAULT_LOGGER_NAME = "social_secretary"
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUP_COUNT = 3


class JsonlFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON，并做最终脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage(), max_len=2000),
        }
        event_data = getattr(record, "event_data", None)
        if isinstance(event_data, dict):
            payload["data"] = sanitize_detail(event_data)
        if record.exc_info:
            payload["error"] = redact_text(
                self.formatException(record.exc_info), max_len=1000
            )
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    logs_dir: Path | str,
    *,
    level: str | int = "INFO",
    logger_name: str = DEFAULT_LOGGER_NAME,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    console: bool = False,
) -> logging.Logger:
    """配置并返回应用 logger。

    重复调用是幂等的：会先移除旧 handler，避免测试/多次初始化时重复写文件。
    """
    directory = Path(logs_dir)
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响后续配置
            pass

    formatter = JsonlFormatter()
    file_handler = RotatingFileHandler(
        directory / LOG_FILE_NAME,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactionFilter())
    logger.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(RedactionFilter())
        logger.addHandler(stream_handler)

    return logger


def reset_logging(logger_name: str = DEFAULT_LOGGER_NAME) -> None:
    """移除并关闭 logger 的全部 handler（测试清理用）。"""
    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    logger.propagate = True


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


__all__ = [
    "JsonlFormatter",
    "LOG_FILE_NAME",
    "configure_logging",
    "reset_logging",
]
