"""只读 IMAP 会话封装（命令白名单护栏）。

为什么需要护栏
--------------
项目铁律要求"绝对只读"。即使调用方只使用我们提供的方法，也可能在将来误用
``imaplib`` 的写命令（STORE / EXPUNGE / DELETE / APPEND / COPY / MOVE / CLOSE ...）。
本模块在 imaplib 唯一的命令发送入口 ``IMAP4._command()`` 上做白名单校验：
任何不在白名单里的命令在**发送前**就抛 ``ReadOnlyViolation``，不会到达服务器。

允许的命令（全部只读）：
- ``CAPABILITY`` / ``NOOP`` / ``LOGOUT``（连接生命周期，非数据修改）；
- ``LOGIN``（只读会话的身份认证；不发送邮件）；
- ``EXAMINE``（只读方式选择邮箱，等价于 SELECT ... readonly）；
- ``UID SEARCH`` / ``UID FETCH``（且 FETCH 只允许 ``BODY.PEEK[...]`` / ``RFC822.SIZE`` 等只读项）。

日志约定：只记录命令动词（如 ``UID``），不记录参数（LOGIN 参数含授权码，FETCH 参数可能含搜索词）。
"""

from __future__ import annotations

import imaplib
import logging
from typing import Any

from .errors import ReadOnlyViolation

logger = logging.getLogger("social_secretary.mail.protocol")

#: 允许整条发送的 IMAP 命令（大写）。
ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "CAPABILITY",
        "LOGIN",
        "EXAMINE",
        "UID",
        "NOOP",
        "LOGOUT",
    }
)

#: 允许通过 ``UID`` 发送的子命令（大写）。SEARCH 只读；FETCH 由 ``_guard_fetch_items`` 限制为 BODY.PEEK。
ALLOWED_UID_COMMANDS: frozenset[str] = frozenset({"SEARCH", "FETCH"})

#: FETCH 允许的只读数据项前缀（小写比较）。
_ALLOWED_FETCH_ITEMS: tuple[str, ...] = (
    "body.peek[",
    "rfc822.size",
    "rfc822.header",
    "rfc822.text",
    "internaldate",
    "flags",
    "uid",
)

#: 明确禁止的写命令（给出更清晰的错误信息；其余未知命令同样被白名单拒绝）。
FORBIDDEN_COMMANDS: frozenset[str] = frozenset(
    {
        "STORE",
        "APPEND",
        "COPY",
        "MOVE",
        "EXPUNGE",
        "DELETE",
        "CREATE",
        "RENAME",
        "SUBSCRIBE",
        "UNSUBSCRIBE",
        "CLOSE",
        "UNSELECT",
        "SETACL",
        "SETQUOTA",
        "CHECK",
    }
)


class _ReadOnlyCommandMixin:
    """在 ``_command`` 处拦截非白名单命令。"""

    def _command(self, name: str, *args: Any) -> Any:  # type: ignore[override]
        verb = str(name).upper()
        if verb not in ALLOWED_COMMANDS:
            raise ReadOnlyViolation(f"已阻止非只读 IMAP 命令: {verb}")
        if verb == "UID":
            sub = str(args[0]).upper() if args else ""
            if sub not in ALLOWED_UID_COMMANDS:
                raise ReadOnlyViolation(f"已阻止非只读 UID 子命令: {sub or '<空>'}")
            if sub == "FETCH":
                _guard_fetch_items(verb, args)
        logger.debug("IMAP >> %s", verb)
        return super()._command(name, *args)  # type: ignore[misc]


def _guard_fetch_items(verb: str, args: tuple[Any, ...]) -> None:
    """UID FETCH 的数据项只允许只读项；禁止 ``BODY[]``（非 PEEK 会置 \\Seen 标志）。

    ``args`` 形如 ``("FETCH", "<uid-set>", "(BODY.PEEK[])")``：跳过前两项，只校验数据项。
    """
    tokens: list[str] = []
    for arg in args[2:]:
        if arg is None:
            continue
        text = arg.decode("ascii", "ignore") if isinstance(arg, (bytes, bytearray)) else str(arg)
        tokens.extend(part for part in text.replace("(", " ").replace(")", " ").split() if part)
    if not tokens:
        raise ReadOnlyViolation("已阻止空的 UID FETCH 参数")
    for token in tokens:
        lowered = token.lower()
        if lowered in {"body[]", "rfc822"}:
            raise ReadOnlyViolation("已阻止非 PEEK 的正文读取（可能修改 \\Seen 标志）")
        if not any(lowered.startswith(prefix) for prefix in _ALLOWED_FETCH_ITEMS):
            raise ReadOnlyViolation(f"已阻止未授权的 FETCH 数据项: {token}")


class ReadOnlyIMAP4(_ReadOnlyCommandMixin, imaplib.IMAP4):
    """明文 IMAP4 客户端（仅用于 127.0.0.1 上的 mock 服务器测试）。"""


class ReadOnlyIMAP4SSL(_ReadOnlyCommandMixin, imaplib.IMAP4_SSL):
    """TLS IMAP4 客户端（生产用：imap.qq.com:993）。"""


def quietly_logout(conn: imaplib.IMAP4 | None) -> None:
    """尽力发送 LOGOUT 并关闭 socket；错误只记 debug 日志（不抛异常）。"""
    if conn is None:
        return
    try:
        if conn.state != "LOGOUT":
            conn.logout()
    except ReadOnlyViolation:  # pragma: no cover - 理论上不会发生
        logger.warning("LOGOUT 未发送：连接状态异常")
    except Exception as exc:  # noqa: BLE001 - 清理阶段的错误不应影响同步结果
        logger.debug("关闭 IMAP 连接时出错: %s", type(exc).__name__)
    finally:
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "ALLOWED_COMMANDS",
    "ALLOWED_UID_COMMANDS",
    "FORBIDDEN_COMMANDS",
    "ReadOnlyIMAP4",
    "ReadOnlyIMAP4SSL",
    "quietly_logout",
]
