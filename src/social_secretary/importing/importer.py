"""把 :class:`~social_secretary.importing.model.ImportResult` 写入本地存储（实验性）。

设计要点
--------
- ``importing`` 包**不直接依赖 ``core`` 实现**：这里只定义 ``MessageSink`` Protocol，
  由调用方（未来的 core ServiceAPI 适配器、原型工具、测试）注入具体实现。
  核心层将来若要提供 ``ServiceAPI.import_messages(...)``，只需实现这两个方法即可接入，
  本包不需要改代码。
- ``run_import(result, sink, dry_run=True)`` 提供 dry-run：只做结果校验与统计，
  **不会调用 sink 的任何方法**（可用于 UI 预览/试运行）。
- 提供一个最小可用的本地文件实现 :class:`JsonlMessageSink`（写 ``conversation.json`` +
  ``messages.jsonl``），供原型工具写入“独立测试数据目录”。它默认**不保存正文**，
  只写长度 + SHA-256 指纹，符合“证据/测试数据不得含私密明文”的约束；
  仅当调用方显式 ``include_text=True`` 时才保存 sender/text 原文（仅限本机实验用途）。
- 所有异常都收敛为 :class:`ImportRunReport`（``ok=False`` + ``error``），不向 UI 抛裸异常。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from ..contracts import RawMessage
from .model import (
    ImportResult,
    ImportedConversation,
    fingerprint_hash,
    text_fingerprint,
)

__all__ = [
    "ImportRunReport",
    "JsonlMessageSink",
    "MessageSink",
    "import_result",
    "run_import",
    "validate_sink",
]


@runtime_checkable
class MessageSink(Protocol):
    """调用方注入的本地写入接口（由 core 适配器或测试 Fake 实现）。

    实现方负责：会话落库/复用、消息去重（可用 ``RawMessage.extras`` 与
    ``source_locator`` 定位来源）、事务与统计。**禁止联网、禁止发送任何消息。**
    """

    def ensure_conversation(self, conversation: ImportedConversation) -> None:
        """确保导入会话存在（可复用同 ``conversation_key`` 的已有会话）。"""

    def write_messages(
        self,
        conversation: ImportedConversation,
        messages: Sequence[RawMessage],
    ) -> int:
        """写入消息并返回实际写入条数（>=0）。重复消息应由实现方自行去重。"""


def validate_sink(sink: object | None) -> list[str]:
    """检查对象是否满足 :class:`MessageSink` 协议，返回缺失方法名列表。"""
    if sink is None:
        return ["ensure_conversation", "write_messages"]
    missing: list[str] = []
    for name in ("ensure_conversation", "write_messages"):
        if not callable(getattr(sink, name, None)):
            missing.append(name)
    return missing


@dataclass(frozen=True)
class ImportRunReport:
    """一次导入写入的脱敏结果（可直接写入证据 JSON）。"""

    ok: bool
    dry_run: bool
    conversation_key: str
    title: str
    message_count: int
    written_count: int
    sink: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_ms: int = 0
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self, *, mask_title: bool = True) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "conversation_key": self.conversation_key,
            "title": text_fingerprint(self.title) if mask_title else self.title,
            "message_count": self.message_count,
            "written_count": self.written_count,
            "sink": self.sink,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
            "error": self.error,
        }


def run_import(
    result: ImportResult,
    sink: MessageSink | None,
    *,
    dry_run: bool = False,
) -> ImportRunReport:
    """把解析结果写入注入的 ``sink``；``dry_run=True`` 时只统计、不调用 sink。

    返回 :class:`ImportRunReport`，任何异常都转成 ``ok=False``（不崩溃）。
    """
    started_at = datetime.now().astimezone()
    started = time.perf_counter()
    conversation = result.conversation
    message_count = len(result.messages)
    warnings = result.warning_messages()

    def report(
        *,
        ok: bool,
        written: int = 0,
        error: str | None = None,
        sink_name: str = "",
    ) -> ImportRunReport:
        finished_at = datetime.now().astimezone()
        return ImportRunReport(
            ok=ok,
            dry_run=dry_run,
            conversation_key=conversation.conversation_key,
            title=conversation.title,
            message_count=message_count,
            written_count=written,
            sink=sink_name,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_ms=int(round((time.perf_counter() - started) * 1000)),
            warnings=warnings,
            error=error,
        )

    if dry_run:
        return report(ok=message_count > 0, sink_name=type(sink).__name__ if sink is not None else "dry-run")

    missing = validate_sink(sink)
    if missing:
        return report(
            ok=False,
            error="MessageSink 缺少必须方法：" + ", ".join(missing),
            sink_name=type(sink).__name__ if sink is not None else "",
        )
    assert sink is not None  # validate_sink 已保证
    if message_count <= 0:
        return report(ok=False, error="没有可导入的消息（result 为空）。", sink_name=type(sink).__name__)

    try:
        sink.ensure_conversation(conversation)
        written = sink.write_messages(conversation, result.raw_messages)
    except Exception as exc:  # noqa: BLE001 - 收敛为报告，不向 UI 抛裸异常
        return report(
            ok=False,
            error=f"写入失败：{type(exc).__name__}: {str(exc)[:200]}",
            sink_name=type(sink).__name__,
        )

    try:
        written_count = int(written)
    except (TypeError, ValueError):
        written_count = 0
    written_count = max(0, min(written_count, message_count))
    return report(ok=written_count > 0, written=written_count, sink_name=type(sink).__name__)


#: ``run_import`` 的别名（语义更直观）。
import_result = run_import


class JsonlMessageSink:
    """最小本地文件 sink：``conversation.json`` + ``messages.jsonl``。

    - 默认 ``include_text=False``：只保存正文/发送者的**长度 + SHA-256 指纹**，
      适合证据与“独立测试数据目录”；
    - ``include_text=True``：保存 sender/text 原文（仅限用户本机实验；不会写入证据 JSON）；
    - 不联网、不调用 QQ、不读写用户真实数据库目录。
    """

    def __init__(self, directory: str | Path, *, include_text: bool = False) -> None:
        self.directory = Path(directory)
        self.include_text = include_text
        self._messages_path = self.directory / "messages.jsonl"
        self._conversation_path = self.directory / "conversation.json"
        self._started = False

    def ensure_conversation(self, conversation: ImportedConversation) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.include_text:
            title: Any = conversation.title
            source_name: Any = conversation.source_name
        else:
            title = text_fingerprint(conversation.title)
            source_name = text_fingerprint(conversation.source_name)
        payload = {
            "conversation_key": conversation.conversation_key,
            "title": title,
            "kind": conversation.kind.value,
            "account_label": conversation.account_label,
            "source_name": source_name,
            "experimental": True,
            "note": "独立测试数据目录：只用于本机实验性导入验证，不是用户真实数据库。",
        }
        self._write_text(self._conversation_path, json.dumps(payload, ensure_ascii=False, indent=2))
        self._started = True

    def write_messages(
        self,
        conversation: ImportedConversation,
        messages: Sequence[RawMessage],
    ) -> int:
        if not self._started:
            self.ensure_conversation(conversation)
        lines: list[str] = []
        for raw in messages:
            if self.include_text:
                locator: Any = raw.source_locator
            else:
                locator = text_fingerprint(raw.source_locator)
            item: dict[str, Any] = {
                "conversation_key": raw.conversation_key,
                "sender": raw.sender if self.include_text else text_fingerprint(raw.sender),
                "is_self": raw.is_self,
                "text": raw.text if self.include_text else text_fingerprint(raw.text),
                "text_length": len(raw.text or ""),
                "timestamp": raw.timestamp.isoformat() if raw.timestamp else None,
                "timestamp_source": raw.timestamp_source.value,
                "raw_time_text": raw.raw_time_text,
                "source_locator": locator,
                "extras": {
                    "import_format": raw.extras.get("import_format"),
                    "import_line_no": raw.extras.get("import_line_no"),
                    "imported": bool(raw.extras.get("imported")),
                    "text_sha256_16": fingerprint_hash(raw.text or "")[:16],
                    "sender_sha256_16": fingerprint_hash(raw.sender or "")[:16],
                },
            }
            lines.append(json.dumps(item, ensure_ascii=False))
        self._write_text(self._messages_path, "\n".join(lines) + ("\n" if lines else ""))
        return len(lines)

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        """先写临时文件再原子替换，避免中断留下半个文件。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(path)

    @classmethod
    def read_messages(cls, directory: str | Path) -> list[dict[str, Any]]:
        """读取本 sink 写出的 messages.jsonl（测试/工具摘要用）。"""
        path = Path(directory) / "messages.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out
