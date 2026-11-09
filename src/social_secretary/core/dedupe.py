"""消息去重。

去重键（AGENTS.md 跨模块约定）::

    content_hash = sha256(conversation_id | sender | is_self | text | timestamp_bucket)

策略
----
1. **有真实时间**（UI / RELATIVE）：``timestamp_bucket`` 取「本地时间精确到分钟」的
   ``YYYY-MM-DDTHH:MM``，同一消息在多次扫描中稳定 → 跨批次幂等，同时保留「同文不同时」
   的合法重复消息。
2. **无真实时间**（COLLECTED / None）：bucket 固定为 ``none``；同一采集批次内用
   ``(conversation_id, source_locator, content)`` 做流式去重（QQ 虚拟化渲染会重复
   暴露同一气泡），并对「同文案多条真正重复消息」追加出现序号，保证同批次不误删。
3. **external_id**（邮件 UID / 消息 ID）：业务层优先按 ``(conversation_id, external_id)``
   判重，最稳定。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..contracts import RawMessage, TimestampSource

UNKNOWN_TIMESTAMP_BUCKET = "none"


def timestamp_bucket(timestamp, timestamp_source: TimestampSource) -> str:
    """返回去重用的时间桶字符串。"""
    if timestamp is None or timestamp_source == TimestampSource.COLLECTED:
        return UNKNOWN_TIMESTAMP_BUCKET
    aware = timestamp if timestamp.tzinfo is not None else timestamp.astimezone()
    return aware.strftime("%Y-%m-%dT%H:%M")


def _digest(parts: list[str]) -> str:
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_content_hash(
    conversation_id: str,
    sender: str,
    is_self: bool,
    text: str,
    timestamp=None,
    timestamp_source: TimestampSource = TimestampSource.COLLECTED,
) -> str:
    """按约定公式计算内容哈希（不含批次状态，可跨批次幂等）。"""
    bucket = timestamp_bucket(timestamp, timestamp_source)
    return _digest(
        [
            str(conversation_id),
            str(sender),
            "1" if is_self else "0",
            str(text),
            bucket,
        ]
    )


def content_hash_for(conversation_id: str, raw: RawMessage) -> str:
    return compute_content_hash(
        conversation_id,
        raw.sender,
        raw.is_self,
        raw.text,
        raw.timestamp,
        raw.timestamp_source,
    )


def with_occurrence(content_hash: str, occurrence: int) -> str:
    """同批次内第 N 次出现的同内容消息使用不同的哈希，避免误删真实重复消息。"""
    if occurrence <= 1:
        return content_hash
    return _digest([content_hash, "occurrence", str(occurrence)])


@dataclass(frozen=True)
class DedupeDecision:
    """一条原始消息的去重判定结果。"""

    content_hash: str
    duplicate_in_batch: bool = False


class MessageDeduplicator:
    """同批次流式去重器（一次 :meth:`reset_batch` 对应一个会话的一次读取）。"""

    def __init__(self) -> None:
        self._locators: set[tuple[str, str, str]] = set()
        self._external_ids: set[tuple[str, str]] = set()
        self._occurrences: dict[tuple[str, str], int] = {}
        self._hashes: set[str] = set()

    def reset_batch(self) -> None:
        self._locators.clear()
        self._external_ids.clear()
        self._occurrences.clear()
        self._hashes.clear()

    def decide(self, conversation_id: str, raw: RawMessage) -> DedupeDecision:
        base = content_hash_for(conversation_id, raw)

        # 1) 同一渲染位置重复出现 → 同批次重复（QQ 虚拟化渲染）
        if raw.source_locator:
            locator_key = (conversation_id, raw.source_locator, base)
            if locator_key in self._locators:
                return DedupeDecision(content_hash=base, duplicate_in_batch=True)
            self._locators.add(locator_key)

        # 2) external_id 稳定标识重复
        if raw.external_id:
            external_key = (conversation_id, raw.external_id)
            if external_key in self._external_ids:
                return DedupeDecision(content_hash=base, duplicate_in_batch=True)
            self._external_ids.add(external_key)

        # 3) 同文案多条真实消息：第 2 条起追加出现序号（批次内判断，跨批次稳定）
        occurrence_key = (conversation_id, base)
        occurrence = self._occurrences.get(occurrence_key, 0) + 1
        self._occurrences[occurrence_key] = occurrence
        content_hash = with_occurrence(base, occurrence)
        if content_hash in self._hashes:
            return DedupeDecision(content_hash=content_hash, duplicate_in_batch=True)
        self._hashes.add(content_hash)
        return DedupeDecision(content_hash=content_hash)


__all__ = [
    "DedupeDecision",
    "MessageDeduplicator",
    "UNKNOWN_TIMESTAMP_BUCKET",
    "compute_content_hash",
    "content_hash_for",
    "timestamp_bucket",
    "with_occurrence",
]
