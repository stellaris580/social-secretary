"""把 UIA 冒泡节点树解析成结构化消息（只读）。

QQ NT（Chromium/Electron）的 UIA 结构大致为：

    chat-msg-area
      ├─ [时间分隔节点]（可选，文本形如 昨天 21:03 / 10月31日 12:30）
      ├─ message-container [message-container--self]      ← 经典模式气泡
      │    ├─ Text 正文行…
      │    ├─ message-container__sender（群聊昵称，可能不存在）
      │    └─ message-container__time（可能不存在）
      └─ msg-content-container …                          ← 效率模式气泡

以上结构来自 reference/qq-operations 的审计结论，**真实时间/发送者节点的
UIA 可见性尚未在真机验证**，所以本模块对「读不到」的情况全部降级：
- 时间读不到 -> timestamp=None + TimestampSource.COLLECTED + raw_time_text
- 群聊发送者读不到 -> 未知成员标签 + sender_resolved=False
- 正文读不到（图片/语音/文件等非文本消息）-> 该气泡丢弃并计数
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from ...contracts import ConversationKind, TimestampSource
from . import nodes
from .nodes import Rect, UiaNode
from .profile import ModeSelectors, compile_pattern
from .timeparse import ParsedTimestamp, parse_time_text

DEFAULT_NODES_LIMIT = 20_000
DEFAULT_DEPTH_LIMIT = 24


@dataclass(frozen=True)
class Bubble:
    """一条可见气泡的解析结果（尚未去重/未转换为 RawMessage）。"""

    index: int
    text: str
    is_self: bool
    sender: str
    sender_resolved: bool
    timestamp: datetime | None
    timestamp_source: TimestampSource
    raw_time_text: str | None
    class_name: str
    rect: Rect | None


@dataclass(frozen=True)
class SnapshotResult:
    """一次「看视口」的解析结果。"""

    bubbles: tuple[Bubble, ...]
    msg_area_found: bool
    msg_area_rect: Rect | None
    bubble_nodes_seen: int = 0
    empty_text_dropped: int = 0
    duplicates_dropped: int = 0
    nodes_scanned: int = 0


def compute_content_hash(
    *,
    conversation_key: str,
    sender: str,
    is_self: bool,
    text: str,
    timestamp: datetime | None,
) -> str:
    """内容哈希：sha256(conversation_id | sender | is_self | text | timestamp_bucket)。

    与 AGENTS.md 约定一致；无时间时 timestamp_bucket 为空串（此时上层必须做
    同一采集批次内的内容+位置流式去重，本模块的 ``MessageAccumulator`` 负责）。
    """

    bucket = ""
    if timestamp is not None:
        bucket = timestamp.astimezone().strftime("%Y%m%d%H%M")
    payload = "|".join(
        [conversation_key, sender or "", "1" if is_self else "0", text or "", bucket]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _class_matches(node: UiaNode, pattern: str) -> bool:
    return bool(compile_pattern(pattern).search(nodes.class_name(node)))


def _any_class_matches(node: UiaNode, patterns: tuple[str, ...]) -> bool:
    cls = nodes.class_name(node)
    return any(compile_pattern(pattern).search(cls) for pattern in patterns)


def _find_msg_area(
    root: UiaNode, selectors: ModeSelectors, *, max_nodes: int
) -> UiaNode | None:
    pattern = selectors.msg_area_class_pattern
    return nodes.find_first(
        root,
        lambda node: bool(compile_pattern(pattern).search(nodes.class_name(node))),
        include_self=True,
        max_nodes=max_nodes,
        max_depth=DEFAULT_DEPTH_LIMIT,
    )


def _parse_hint(text: str, now: datetime) -> ParsedTimestamp | None:
    return parse_time_text(text, now=now)


def _find_sender_node(bubble: UiaNode, selectors: ModeSelectors) -> UiaNode | None:
    patterns = selectors.sender_class_patterns
    if not patterns:
        return None
    for node, _depth in nodes.iter_descendants(bubble, max_depth=8, max_nodes=200):
        if _any_class_matches(node, patterns):
            return node
    return None


def _is_self_bubble(bubble: UiaNode, selectors: ModeSelectors) -> bool:
    pattern = selectors.self_class_pattern
    if _class_matches(bubble, pattern):
        return True
    # 参考项目在容器 class 上判定；为兼容不同版本，再查一层子节点的类名。
    for child, _depth in nodes.iter_descendants(bubble, max_depth=3, max_nodes=80):
        if _class_matches(child, pattern):
            return True
    return False


def _clean_name(text: str) -> str:
    return text.strip().strip("：:").strip()


def _collect_texts_and_time(
    bubble: UiaNode,
    selectors: ModeSelectors,
    now: datetime,
    *,
    excluded_ids: set[int] | None = None,
) -> tuple[str, ParsedTimestamp | None, set[int]]:
    """返回 (正文, 气泡内时间, 已作为时间/发送者排除的节点 id 集合)。"""

    excluded_ids = excluded_ids or set()
    time_candidates: list[tuple[UiaNode, ParsedTimestamp]] = []
    text_nodes: list[UiaNode] = []
    for node, _depth in nodes.iter_descendants(bubble, max_depth=10, max_nodes=400):
        if id(node) in excluded_ids:
            continue
        ctype = nodes.control_type(node).lower()
        if ctype not in {t.lower() for t in selectors.text_control_types}:
            continue
        name = nodes.node_name(node).strip()
        if not name:
            continue
        text_nodes.append(node)
        is_time_class = _any_class_matches(node, selectors.time_class_patterns)
        parsed = _parse_hint(name, now)
        if parsed is not None and (is_time_class or len(text_nodes) >= 2):
            time_candidates.append((node, parsed))

    excluded: set[int] = set()
    chosen: ParsedTimestamp | None = None
    if time_candidates:
        # 优先 class 命中时间样式的节点；其次取第一个可解析的。
        class_hits = [
            (node, parsed)
            for node, parsed in time_candidates
            if _any_class_matches(node, selectors.time_class_patterns)
        ]
        node, parsed = (class_hits or time_candidates)[0]
        excluded.add(id(node))
        chosen = parsed

    parts: list[str] = []
    for node in text_nodes:
        if id(node) in excluded:
            continue
        name = nodes.node_name(node).strip()
        if name:
            parts.append(name)
    body = selectors.text_join.join(parts).strip()
    return body, chosen, excluded


def _parse_bubble(
    bubble: UiaNode,
    *,
    index: int,
    pending_hint: ParsedTimestamp | None,
    selectors: ModeSelectors,
    conversation_title: str,
    conversation_kind: ConversationKind,
    self_label: str,
    unknown_sender_label: str,
    now: datetime,
) -> Bubble | None:
    is_self = _is_self_bubble(bubble, selectors)
    sender_node = None
    if is_self:
        sender, sender_resolved = self_label, True
    else:
        sender_node = _find_sender_node(bubble, selectors)
        if sender_node is not None:
            name = _clean_name(nodes.node_name(sender_node))
            if name:
                sender, sender_resolved = name, True
            else:
                sender_node = None
                if conversation_kind == ConversationKind.GROUP:
                    sender, sender_resolved = unknown_sender_label, False
                else:
                    sender, sender_resolved = conversation_title, True
        elif conversation_kind == ConversationKind.GROUP:
            sender, sender_resolved = unknown_sender_label, False
        else:
            sender, sender_resolved = conversation_title, True

    # 群聊昵称节点（含其子 Text）不参与正文拼接，避免昵称混进消息正文。
    sender_excluded_ids: set[int] = set()
    if sender_node is not None:
        sender_excluded_ids.add(id(sender_node))
        for child, _depth in nodes.iter_descendants(sender_node, max_depth=4, max_nodes=50):
            sender_excluded_ids.add(id(child))
    body, own_time, excluded = _collect_texts_and_time(
        bubble, selectors, now, excluded_ids=sender_excluded_ids
    )
    hint = own_time if own_time is not None else pending_hint
    timestamp: datetime | None = None
    source = TimestampSource.COLLECTED
    raw_time_text: str | None = None
    if hint is not None:
        raw_time_text = hint.raw_text
        if hint.timestamp is not None and not hint.date_only:
            timestamp = hint.timestamp
            source = hint.source
    if not body:
        return None
    return Bubble(
        index=index,
        text=body,
        is_self=is_self,
        sender=sender,
        sender_resolved=sender_resolved,
        timestamp=timestamp,
        timestamp_source=source,
        raw_time_text=raw_time_text,
        class_name=nodes.class_name(bubble),
        rect=nodes.node_rect(bubble),
    )


def _same_snapshot_bubble(left: Bubble, right: Bubble) -> bool:
    if (
        left.text != right.text
        or left.is_self != right.is_self
        or left.sender != right.sender
        or left.raw_time_text != right.raw_time_text
    ):
        return False
    if (left.timestamp is None) != (right.timestamp is None):
        return False
    if left.timestamp is not None and right.timestamp is not None:
        return left.timestamp == right.timestamp
    return True


def _rect_close(left: Rect | None, right: Rect | None, tolerance: int = 2) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return all(abs(a - b) <= tolerance for a, b in zip(left, right, strict=True))


def _collapse_virtualized_duplicates(bubbles: list[Bubble]) -> tuple[list[Bubble], int]:
    """折叠虚拟化渲染产生的「同内容 + 同矩形」重复气泡。"""

    collapsed: list[Bubble] = []
    dropped = 0
    for bubble in bubbles:
        if collapsed and _same_snapshot_bubble(collapsed[-1], bubble) and _rect_close(
            collapsed[-1].rect, bubble.rect
        ):
            dropped += 1
            continue
        collapsed.append(bubble)
    return collapsed, dropped


def read_visible_bubbles(
    root: UiaNode,
    selectors: ModeSelectors,
    *,
    conversation_title: str,
    conversation_kind: ConversationKind,
    self_label: str,
    unknown_sender_label: str,
    now: datetime,
    max_nodes: int = DEFAULT_NODES_LIMIT,
    max_depth: int = DEFAULT_DEPTH_LIMIT,
) -> SnapshotResult:
    """解析当前视口内的气泡（不滚动、不点击、不写任何东西）。"""

    area = _find_msg_area(root, selectors, max_nodes=max_nodes)
    if area is None:
        return SnapshotResult(bubbles=(), msg_area_found=False, msg_area_rect=None)

    bubble_re = compile_pattern(selectors.bubble_class_pattern)
    exclude_res = [compile_pattern(p) for p in selectors.bubble_exclude_class_patterns]
    time_res = [compile_pattern(p) for p in selectors.time_class_patterns]

    bubbles: list[Bubble] = []
    pending_hint: ParsedTimestamp | None = None
    bubble_nodes_seen = 0
    empty_text_dropped = 0
    nodes_scanned = 0

    def walk(node: UiaNode, depth: int) -> None:
        nonlocal pending_hint, bubble_nodes_seen, empty_text_dropped, nodes_scanned
        nodes_scanned += 1
        if nodes_scanned > max_nodes or depth > max_depth:
            return
        cls = nodes.class_name(node)
        if node is not area and bubble_re.search(cls):
            if any(res.search(cls) for res in exclude_res):
                # 内部包裹层：不当作气泡，但继续向下找真正气泡/正文。
                pass
            else:
                bubble_nodes_seen += 1
                parsed = _parse_bubble(
                    node,
                    index=len(bubbles),
                    pending_hint=pending_hint,
                    selectors=selectors,
                    conversation_title=conversation_title,
                    conversation_kind=conversation_kind,
                    self_label=self_label,
                    unknown_sender_label=unknown_sender_label,
                    now=now,
                )
                if parsed is None:
                    empty_text_dropped += 1
                else:
                    bubbles.append(parsed)
                return  # 气泡内部由 _parse_bubble 处理，避免嵌套重复计数
        name = nodes.node_name(node).strip()
        if name and any(res.search(cls) for res in time_res):
            hint = _parse_hint(name, now)
            if hint is not None:
                pending_hint = hint
        elif name and not any(res.search(cls) for res in time_res):
            # 无 class 线索时的保底：短的、能完整解析成时间的独立节点视为时间分隔。
            if len(name) <= 32:
                hint = _parse_hint(name, now)
                if hint is not None and hint.timestamp is not None:
                    pending_hint = hint
        for child in nodes.safe_children(node):
            walk(child, depth + 1)

    walk(area, 0)
    collapsed, dropped = _collapse_virtualized_duplicates(bubbles)
    return SnapshotResult(
        bubbles=tuple(collapsed),
        msg_area_found=True,
        msg_area_rect=nodes.node_rect(area),
        bubble_nodes_seen=bubble_nodes_seen,
        empty_text_dropped=empty_text_dropped,
        duplicates_dropped=dropped,
        nodes_scanned=nodes_scanned,
    )


class MessageAccumulator:
    """滚动历史读取的跨视口去重累加器。

    去重键 = 内容哈希（含时间分钟桶）。虚拟化渲染/滚动重叠会把同一条消息
    重复暴露多次，这里用「出现次数上限」算法既去掉重复，又保留用户真的连续
    发了多条相同内容的情况：

    - 第 1 遍视口看到 "OK" 出现 1 次 -> 记录 max_occurrence=1，保留；
    - 第 2 遍（上滚后视口重叠）又看到同一个 "OK" -> 出现次数 1 <= 1，丢弃；
    - 若上滚后看到更早还有一条 "OK"（出现 2 次）-> 出现次数 2 > 1，保留。
    """

    def __init__(self, *, conversation_key: str, limit: int | None = None) -> None:
        self.conversation_key = conversation_key
        self.limit = limit
        self._messages: list[Bubble] = []
        self._max_occurrence: dict[str, int] = {}
        self.duplicates_dropped = 0
        self.passes = 0

    @property
    def messages(self) -> tuple[Bubble, ...]:
        return tuple(self._messages)

    def content_hash(self, bubble: Bubble) -> str:
        return compute_content_hash(
            conversation_key=self.conversation_key,
            sender=bubble.sender,
            is_self=bubble.is_self,
            text=bubble.text,
            timestamp=bubble.timestamp,
        )

    def add_visible(self, bubbles: tuple[Bubble, ...], *, scrolled_up: bool) -> list[Bubble]:
        self.passes += 1
        local: dict[str, int] = {}
        new_items: list[Bubble] = []
        for bubble in bubbles:
            key = self.content_hash(bubble)
            local[key] = local.get(key, 0) + 1
            occurrence = local[key]
            if occurrence <= self._max_occurrence.get(key, 0):
                self.duplicates_dropped += 1
                continue
            new_items.append(bubble)
        for key, count in local.items():
            if count > self._max_occurrence.get(key, 0):
                self._max_occurrence[key] = count
        if scrolled_up and self._messages:
            self._messages = new_items + self._messages
        else:
            self._messages.extend(new_items)
        return new_items

    def latest(self, limit: int) -> list[Bubble]:
        if limit <= 0:
            return []
        return self._messages[-limit:]


__all__ = [
    "Bubble",
    "SnapshotResult",
    "compute_content_hash",
    "read_visible_bubbles",
    "MessageAccumulator",
]
