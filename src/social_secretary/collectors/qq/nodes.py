"""UIA 控件树的最小抽象与遍历工具。

只做只读操作：读名称/类名/控件类型/矩形/子节点，以及在搜索框上写值/聚焦。
本模块不包含任何发送消息、点击发送按钮的路径。

抽象出 ``UiaNode`` 协议的目的是让离线测试用 fake 节点树驱动采集逻辑，
测试不需要真实 QQ，也不需要 uiautomation 包（真实节点在 ``backend.py``）。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)

# 单次遍历的保护上限，防止异常 UIA 树造成卡死。
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_NODES = 20_000


@runtime_checkable
class UiaNode(Protocol):
    """UIA 节点只读视图 + 搜索框所需的两个受控写操作。"""

    @property
    def name(self) -> str:  # 控件 Name（搜索框/联系人名/文本内容）
        ...

    @property
    def class_name(self) -> str:  # 控件 ClassName（QQ NT 用类名标记气泡等）
        ...

    @property
    def control_type(self) -> str:  # 归一化控件类型，如 "Text"/"Edit"/"ListItem"
        ...

    @property
    def children(self) -> Sequence["UiaNode"]: ...

    @property
    def rect(self) -> Rect | None: ...

    @property
    def is_offscreen(self) -> bool: ...

    @property
    def is_enabled(self) -> bool: ...

    def get_value(self) -> str | None:
        """ValuePattern 的值；不支持或读取失败返回 None。"""

    def set_value(self, value: str) -> bool:
        """仅用于主面板搜索框。其他控件禁止调用。"""

    def set_focus(self) -> bool:
        """仅用于主面板搜索框的聚焦（剪贴板回退前置安全校验）。"""


def safe_children(node: UiaNode) -> list[UiaNode]:
    try:
        return list(node.children)
    except Exception:  # pragma: no cover - 真实 UIA 树异常
        return []


def node_rect(node: UiaNode) -> Rect | None:
    try:
        rect = node.rect
    except Exception:  # pragma: no cover
        return None
    if not rect:
        return None
    try:
        left, top, right, bottom = (int(v) for v in rect)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def rect_center(rect: Rect | None) -> tuple[int, int] | None:
    if not rect:
        return None
    left, top, right, bottom = rect
    return ((left + right) // 2, (top + bottom) // 2)


def rect_area(rect: Rect | None) -> int:
    if not rect:
        return 0
    left, top, right, bottom = rect
    return max(0, right - left) * max(0, bottom - top)


def control_type(node: UiaNode) -> str:
    try:
        value = node.control_type or ""
    except Exception:  # pragma: no cover
        return ""
    value = str(value).strip()
    if value.endswith("Control") and len(value) > len("Control"):
        value = value[: -len("Control")]
    return value


def node_name(node: UiaNode) -> str:
    try:
        return str(node.name or "")
    except Exception:  # pragma: no cover
        return ""


def class_name(node: UiaNode) -> str:
    try:
        return str(node.class_name or "")
    except Exception:  # pragma: no cover
        return ""


def iter_descendants(
    root: UiaNode,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    include_self: bool = False,
) -> Iterator[tuple[UiaNode, int]]:
    """DFS 先序遍历后代节点（UIA 文档顺序），带深度/节点数保护。"""

    stack: list[tuple[UiaNode, int]] = []
    if include_self:
        stack.append((root, 0))
    else:
        for child in reversed(safe_children(root)):
            stack.append((child, 1))
    seen = 0
    while stack:
        node, depth = stack.pop()
        yield node, depth
        seen += 1
        if seen >= max_nodes or depth >= max_depth:
            continue
        children = safe_children(node)
        for child in reversed(children):
            stack.append((child, depth + 1))


def find_all(
    root: UiaNode,
    predicate,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    include_self: bool = False,
) -> list[UiaNode]:
    result: list[UiaNode] = []
    for node, _depth in iter_descendants(
        root, max_depth=max_depth, max_nodes=max_nodes, include_self=include_self
    ):
        if predicate(node):
            result.append(node)
    return result


def find_first(
    root: UiaNode,
    predicate,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    include_self: bool = False,
) -> UiaNode | None:
    for node, _depth in iter_descendants(
        root, max_depth=max_depth, max_nodes=max_nodes, include_self=include_self
    ):
        if predicate(node):
            return node
    return None


def descendant_texts(root: UiaNode, *, control_types: Sequence[str] = ("Text",)) -> list[str]:
    """按文档顺序收集后代 Text 控件名称（去空白、去空串）。"""

    wanted = {t.lower() for t in control_types}
    texts: list[str] = []
    for node, _depth in iter_descendants(root):
        if control_type(node).lower() not in wanted:
            continue
        text = node_name(node).strip()
        if text:
            texts.append(text)
    return texts


def same_element(a: UiaNode | None, b: UiaNode | None) -> bool:
    """比较两个 UIA 包装对象是否指向同一控件（类名+控件类型+名称+矩形近似）。"""

    if a is None or b is None:
        return False
    if a is b:
        return True
    if control_type(a) != control_type(b):
        return False
    if class_name(a) != class_name(b):
        return False
    if node_name(a) != node_name(b):
        return False
    ra, rb = node_rect(a), node_rect(b)
    if ra is None or rb is None:
        return ra is None and rb is None
    return all(abs(x - y) <= 2 for x, y in zip(ra, rb, strict=True))


__all__ = [
    "Rect",
    "UiaNode",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "safe_children",
    "node_rect",
    "rect_center",
    "rect_area",
    "control_type",
    "node_name",
    "class_name",
    "iter_descendants",
    "find_all",
    "find_first",
    "descendant_texts",
    "same_element",
]
