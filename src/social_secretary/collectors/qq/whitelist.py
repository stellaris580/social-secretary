"""会话白名单与标题规范化。

安全原则：只有用户显式配置（allowlist）的会话才允许打开/读取；标题比较一律
先做 Unicode NFKC + 空白折叠 + 去掉「等 N 个会话」窗口合并后缀，再做精确匹配
（可选宽松包含匹配，但宽松匹配只用于「找搜索入口」，最终仍以聊天头部联系人
校验为准）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")
_MERGE_SUFFIX_RE = re.compile(r"等\s*\d+\s*个会话\s*$")


def normalize_title(raw: str | None) -> str:
    """标题规范化：用于白名单匹配 / 窗口标题比较 / 头部联系人校验。"""

    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = _INVISIBLE_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _MERGE_SUFFIX_RE.sub("", text).strip()
    return text.casefold()


class TitleWhitelist:
    """精确白名单（默认）；``fuzzy=True`` 时额外允许包含匹配（仅用于搜索入口）。"""

    def __init__(self, entries: Iterable[str], *, fuzzy: bool = False) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for entry in entries or ():
            key = normalize_title(entry)
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        self._entries: tuple[str, ...] = tuple(normalized)
        self.fuzzy = bool(fuzzy)

    @property
    def entries(self) -> tuple[str, ...]:
        return self._entries

    def __bool__(self) -> bool:
        return bool(self._entries)

    def allows(self, title: str | None) -> bool:
        key = normalize_title(title)
        if not key:
            return False
        if key in self._entries:
            return True
        if self.fuzzy:
            return any(key in entry or entry in key for entry in self._entries)
        return False

    def canonical(self, title: str | None) -> str | None:
        """返回命中的规范白名单条目（未命中返回 None）。"""

        key = normalize_title(title)
        if not key:
            return None
        if key in self._entries:
            return key
        if self.fuzzy:
            for entry in self._entries:
                if key in entry or entry in key:
                    return entry
        return None


__all__ = ["normalize_title", "TitleWhitelist"]
