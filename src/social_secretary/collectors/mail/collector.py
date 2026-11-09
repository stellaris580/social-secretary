"""QQ 邮箱只读 IMAP 采集器（实现 contracts.MessageSource）。

安全边界（与项目铁律一致）
--------------------------
- 只用 IMAPS（993，``ssl.create_default_context`` 校验证书与主机名）；
- 登录使用用户授权码，只从注入的 ``CredentialStore`` 读取，绝不硬编码/落盘/打印；
- 选择邮箱只用 ``EXAMINE``（readonly），读正文只用 ``BODY.PEEK[]``；
- 会话层 ``session.ReadOnlyIMAP4*`` 在命令发送前做白名单拦截，不存在 SMTP/发送/删除/标记路径；
- 日志只记录账号掩码、文件夹、UID、数量、错误码；不记录授权码、正文、完整邮箱地址。

增量同步
--------
``SyncStateStore``（见 ``state.py``）持久化 ``UIDVALIDITY + next_uid``：
- 首次同步从 UID 1 开始；
- 之后每次只 ``UID SEARCH UID {next_uid}:*``，只 FETCH 新 UID；
- UIDVALIDITY 变化 → 自动从 1 全量重同步，并在 ``last_warnings`` 中说明；
- 单次调用最多处理 ``limit`` 封，状态只推进到已处理的最大 UID，剩余邮件下次继续。

错误约定
--------
可预期失败抛 ``MailCollectorError`` 子类（见 errors.py）；``test_connection`` 不抛异常，
通过 ``ConnectionCheck(ok=False, discovered={"code": ...})`` 返回。单封邮件解析失败、
超大邮件、按 ``since`` 过滤等非致命问题记入 ``last_warnings``，不中断整批同步。
"""

from __future__ import annotations

import imaplib
import logging
import re
import socket
import ssl
import threading
import time
from datetime import datetime
from typing import Any, Callable

from social_secretary.contracts import (
    Account,
    AccountKind,
    ConnectionCheck,
    Conversation,
    ConversationKind,
    CredentialStore,
    MessageSource,
    RawMessage,
    TimestampSource,
)

from .errors import (
    MailAuthError,
    MailCollectorClosedError,
    MailCollectorError,
    MailConnectionError,
    MailCredentialMissingError,
    MailErrorCode,
    MailFolderNotFoundError,
    MailProtocolError,
    MailTimeoutError,
    MailTlsError,
    ReadOnlyViolation,
)
from .parser import DEFAULT_MAX_BODY_CHARS, ParsedMail, parse_email
from .session import ReadOnlyIMAP4, ReadOnlyIMAP4SSL, quietly_logout
from .state import (
    MailSyncState,
    MemorySyncStateStore,  # noqa: F401  (re-export 方便 app-core 演示接线)
    SyncStateStore,
    account_state_key,
    normalize_mailbox,
)

logger = logging.getLogger("social_secretary.mail")

#: QQ 邮箱 IMAP 服务地址。
DEFAULT_HOST = "imap.qq.com"
DEFAULT_PORT = 993
#: 默认只采集收件箱；app-core 可传多个文件夹。
DEFAULT_FOLDERS: tuple[str, ...] = ("INBOX",)
#: 单封邮件最大字节数（超过则跳过正文下载，只记 warning；避免 50MB 附件邮件拖垮同步）。
DEFAULT_MAX_MESSAGE_BYTES = 5 * 1024 * 1024
#: 网络超时（秒）。
DEFAULT_TIMEOUT = 20.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_FOLDER_TITLES = {
    "INBOX": "收件箱",
    "SENT": "已发送",
    "SENT MESSAGES": "已发送",
    "DRAFTS": "草稿箱",
    "TRASH": "已删除",
    "DELETED MESSAGES": "已删除",
    "JUNK": "垃圾邮件",
    "SPAM": "垃圾邮件",
    "ARCHIVE": "存档",
}

_AUTH_MARKERS = (
    "authenticationfailed",
    "authentication failed",
    "invalid credentials",
    "auth failed",
    "login failed",
    "authentication unsuccessful",
)

_READONLY_FLAG = "READ-ONLY"


def mail_credential_key(email: str) -> str:
    """CredentialStore 中邮箱授权码的 key 命名（与 Windows 凭据管理器条目对应）。"""
    normalized = (email or "").strip().lower()
    return f"mail.auth_code.{normalized}"


def mask_email(email: str) -> str:
    """日志用：``zhangsan@qq.com`` → ``z***@qq.com``。不用于业务逻辑。"""
    local, sep, domain = (email or "").strip().partition("@")
    if not sep:
        return "***"
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


class MailCollector:
    """只读 IMAP 邮件采集器。

    参数
    ----
    account:
        必须是 ``AccountKind.EMAIL``，且 ``identifier`` 为邮箱地址（非敏感标识）。
    credential_store:
        实现 contracts.CredentialStore；授权码 key 见 ``mail_credential_key()``。
    sync_state:
        实现 state.SyncStateStore；由 app-core 用 SQLite 实现并负责接线。
    folders:
        允许采集的文件夹（白名单）；``fetch_messages`` 只接受这里的文件夹。
    fetch_limit_cap:
        可选的单次同步条数上限（None=不限制）。产品流水线固定传 ``limit=200``，
        验收/演示工具可通过该参数把单次读取限制为 N 封（如 20）。
    initial_sync_newest:
        首次同步（无状态或 UIDVALIDITY 变化）时是否优先读取**最近**的邮件：
        True 时从搜索结果尾部取 ``limit`` 封（跳过更早的积压），并只推进游标到最新；
        默认 False（保持原有从 UID 1 起按升序读取的行为）。
    insecure_plaintext:
        仅测试用：对 127.0.0.1/localhost 的 mock 服务器走明文 IMAP4；非回环地址直接拒绝。
    connection_factory:
        高级测试注入点：``factory(host, port, timeout) -> imaplib.IMAP4``；
        默认按 insecure_plaintext/ssl_context 创建只读会话。
    clock:
        可注入时钟（测试用），默认 ``datetime.now().astimezone()``。
    """

    kind = AccountKind.EMAIL

    def __init__(
        self,
        *,
        account: Account,
        credential_store: CredentialStore,
        sync_state: SyncStateStore,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        folders: tuple[str, ...] | list[str] = DEFAULT_FOLDERS,
        timeout: float = DEFAULT_TIMEOUT,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
        fetch_limit_cap: int | None = None,
        initial_sync_newest: bool = False,
        insecure_plaintext: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        connection_factory: Callable[[str, int, float], Any] | None = None,
        credential_key: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if account.kind != AccountKind.EMAIL:
            raise ValueError("MailCollector 只支持 EMAIL 类型的 Account")
        email = (account.identifier or "").strip()
        if "@" not in email:
            raise ValueError("Account.identifier 必须是邮箱地址（不含密码/授权码）")
        if not folders:
            raise ValueError("folders 不能为空")
        if timeout <= 0:
            raise ValueError("timeout 必须为正数")
        if max_message_bytes <= 0:
            raise ValueError("max_message_bytes 必须为正数")
        if max_body_chars < 0:
            raise ValueError("max_body_chars 不能为负数")
        if fetch_limit_cap is not None and (
            not isinstance(fetch_limit_cap, int) or isinstance(fetch_limit_cap, bool) or fetch_limit_cap < 1
        ):
            raise ValueError("fetch_limit_cap 必须是 >= 1 的整数或 None")
        if insecure_plaintext and (host or "").strip().lower() not in _LOOPBACK_HOSTS:
            raise ValueError("insecure_plaintext 只允许连接本机回环地址（mock 服务器）")

        self.account = account
        self.host = host
        self.port = int(port)
        self._email = email
        self._credential_store = credential_store
        self._sync_state = sync_state
        self._timeout = float(timeout)
        self._max_message_bytes = int(max_message_bytes)
        self._max_body_chars = int(max_body_chars)
        self._fetch_limit_cap = int(fetch_limit_cap) if fetch_limit_cap is not None else None
        self._initial_sync_newest = bool(initial_sync_newest)
        self._insecure_plaintext = bool(insecure_plaintext)
        self._connection_factory = connection_factory
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._account_key = account_state_key(account.id, email)
        self._credential_key = credential_key or mail_credential_key(email)

        self._folders: tuple[str, ...] = tuple(_dedupe(normalize_mailbox(f) for f in folders))

        if not insecure_plaintext:
            self._ssl_context = ssl_context if ssl_context is not None else ssl.create_default_context()
        else:
            self._ssl_context = None

        self._fetch_lock = threading.Lock()
        self._closed = False
        self._last_warnings: tuple[str, ...] = ()
        self._last_stats: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # MessageSource 协议
    # ------------------------------------------------------------------
    def test_connection(self) -> ConnectionCheck:
        """连通性 + 认证 + 只读选择 INBOX 的快速自检；不抛异常，返回脱敏 ConnectionCheck。"""
        started = time.perf_counter()
        conn: Any = None
        try:
            self._ensure_open()
            secret = self._get_secret()
            conn = self._open_connection()
            self._login(conn, secret)
            uidvalidity = self._select_readonly(conn, "INBOX")
            exists = _exists_count(conn)
            discovered: dict[str, Any] = {
                "host": self.host,
                "port": self.port,
                "readonly": True,
                "uidvalidity": uidvalidity,
                "message_count": exists,
                "capabilities": list(getattr(conn, "capabilities", ())),
                "tls": _tls_summary(conn),
            }
            elapsed = _elapsed_ms(started)
            logger.info(
                "IMAP 连接检查成功 account=%s host=%s:%s readonly=True",
                mask_email(self._email),
                self.host,
                self.port,
            )
            return ConnectionCheck(
                ok=True,
                detail="QQ 邮箱 IMAP 连接成功（只读 EXAMINE 收件箱，未做任何写入）。",
                latency_ms=elapsed,
                discovered=discovered,
            )
        except MailCollectorError as exc:
            logger.warning(
                "IMAP 连接检查失败 account=%s code=%s", mask_email(self._email), exc.code.value
            )
            return ConnectionCheck(
                ok=False,
                detail=exc.user_message,
                latency_ms=_elapsed_ms(started),
                discovered=exc.to_discovered(),
            )
        except Exception as exc:  # noqa: BLE001 - ConnectionCheck 约定不抛异常
            translated = self._translate_exception(exc)
            logger.warning(
                "IMAP 连接检查异常 account=%s code=%s", mask_email(self._email), translated.code.value
            )
            return ConnectionCheck(
                ok=False,
                detail=translated.user_message,
                latency_ms=_elapsed_ms(started),
                discovered=translated.to_discovered(),
            )
        finally:
            quietly_logout(conn)

    def list_conversations(self) -> list[Conversation]:
        """把配置的文件夹映射为 MAIL 会话（不访问网络，离线也可用）。

        MVP 只支持 app-core/用户配置的 ASCII 文件夹名（INBOX 等）。QQ 中文自定义文件夹
        需要 modified-UTF-7 解码与 LIST 命令，列为后续工作（见可行性文档）。
        """
        self._ensure_open()
        conversations: list[Conversation] = []
        for folder in self._folders:
            state = self._sync_state.get(self._account_key, folder)
            conversations.append(
                Conversation(
                    id=self._conversation_id(folder),
                    account_id=self.account.id,
                    kind=ConversationKind.MAIL,
                    title=_folder_title(folder),
                    external_key=folder,
                    enabled=True,
                    last_scanned_at=state.last_synced_at if state else None,
                )
            )
        return conversations

    def fetch_messages(
        self,
        conversation: Conversation,
        *,
        since: datetime | None = None,
        limit: int = 100,
        foreground_ack: bool = False,
    ) -> list[RawMessage]:
        """增量拉取指定文件夹的新邮件（只读）。

        - ``since``：可选的额外下界（按邮件 Date 头过滤；无 Date 的邮件不会因此被过滤）；
          主增量语义仍是 UID，since 只作为调用方的额外限制，被过滤的邮件状态照常推进。
        - ``limit``：本次最多处理的邮件数；处理不完的邮件下次继续（状态只推到已处理 UID）。
          构造参数 ``fetch_limit_cap`` 会对它再做一次上限约束（用于验收工具限制最近 N 封）。
        - ``foreground_ack``：邮件采集不需要前台窗口，参数仅为兼容协议，忽略。
        """
        self._ensure_open()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是 >= 1 的整数")
        if self._fetch_limit_cap is not None:
            limit = min(limit, self._fetch_limit_cap)
        folder = normalize_mailbox(conversation.external_key or "INBOX")
        if folder.upper() not in {f.upper() for f in self._folders}:
            raise MailFolderNotFoundError(f"文件夹不在允许采集范围内: {folder}")
        if since is not None and since.tzinfo is None:
            since = since.astimezone()

        with self._fetch_lock:
            return self._fetch_locked(conversation, folder, since=since, limit=limit)

    def close(self) -> None:
        """标记采集器关闭（每次同步独立连接，无长连接需要回收）；可重复调用。"""
        self._closed = True

    # ------------------------------------------------------------------
    # 只读属性（给 app-core 组装 CollectionResult / UI 提示用）
    # ------------------------------------------------------------------
    @property
    def last_warnings(self) -> tuple[str, ...]:
        """最近一次 fetch 的非致命警告（中文，已脱敏）。"""
        return self._last_warnings

    @property
    def last_stats(self) -> dict[str, Any]:
        """最近一次 fetch 的统计（供 core 组装 CollectionResult）。"""
        return dict(self._last_stats)

    # ------------------------------------------------------------------
    # 同步主流程
    # ------------------------------------------------------------------
    def _fetch_locked(
        self,
        conversation: Conversation,
        folder: str,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[RawMessage]:
        warnings: list[str] = []
        stats: dict[str, Any] = {
            "uids_found": 0,
            "messages_seen": 0,
            "messages_new": 0,
            "skipped_oversize": 0,
            "skipped_before_since": 0,
            "parse_errors": 0,
        }
        conn: Any = None
        try:
            secret = self._get_secret()
            conn = self._open_connection()
            self._login(conn, secret)
            uidvalidity = self._select_readonly(conn, folder)
            previous = self._sync_state.get(self._account_key, folder)
            next_uid = self._resolve_next_uid(previous, uidvalidity, warnings)
            uids = self._search_uids_from(conn, next_uid)
            stats["uids_found"] = len(uids)
            fresh_start = previous is None or (
                uidvalidity is not None
                and previous.uidvalidity is not None
                and uidvalidity != previous.uidvalidity
            )
            newest_first = bool(self._initial_sync_newest and fresh_start and len(uids) > limit)
            if newest_first:
                batch = uids[-limit:]
                skipped = len(uids) - limit
                warnings.append(
                    f"首次同步只读取最近 {limit} 封邮件（跳过更早的 {skipped} 封积压；"
                    "如需更早邮件可用更大 N 重试）"
                )
            else:
                batch = uids[:limit]
            stats["messages_seen"] = len(batch)
            stats["initial_sync"] = fresh_start
            stats["newest_first"] = newest_first

            messages: list[RawMessage] = []
            processed_uid: int | None = None
            for uid in batch:
                message, fatal = self._process_one_uid(
                    conn,
                    conversation,
                    folder,
                    uid,
                    uidvalidity,
                    since=since,
                    warnings=warnings,
                    stats=stats,
                )
                if fatal is not None:
                    # 连接/协议级错误：先把已处理进度落盘，再抛出，避免下次重复下载。
                    if processed_uid is not None:
                        self._persist_state(folder, uidvalidity, processed_uid + 1)
                    raise fatal
                if message is not None:
                    messages.append(message)
                processed_uid = uid

            new_next_uid = next_uid if processed_uid is None else processed_uid + 1
            self._persist_state(folder, uidvalidity, new_next_uid)
            stats.update(
                {
                    "messages_new": len(messages),
                    "next_uid": new_next_uid,
                    "uidvalidity": uidvalidity,
                }
            )
            self._last_warnings = tuple(warnings)
            self._last_stats = stats
            logger.info(
                "IMAP 同步完成 account=%s mailbox=%s seen=%d new=%d next_uid=%d uidvalidity=%s",
                mask_email(self._email),
                folder,
                stats["messages_seen"],
                stats["messages_new"],
                new_next_uid,
                uidvalidity,
            )
            return messages
        except MailCollectorError:
            self._last_warnings = tuple(warnings)
            self._last_stats = stats
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转换为采集器错误码
            self._last_warnings = tuple(warnings)
            self._last_stats = stats
            raise self._translate_exception(exc) from exc
        finally:
            quietly_logout(conn)

    def _process_one_uid(
        self,
        conn: Any,
        conversation: Conversation,
        folder: str,
        uid: int,
        uidvalidity: int | None,
        *,
        since: datetime | None,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> tuple[RawMessage | None, MailCollectorError | None]:
        """处理单个 UID；返回 (消息或 None, 致命错误或 None)。非致命问题记 warnings。"""
        try:
            size = self._fetch_size(conn, uid)
            if size is not None and size > self._max_message_bytes:
                warnings.append(
                    f"UID {uid} 超过大小上限（{size} > {self._max_message_bytes} 字节），已跳过正文"
                )
                stats["skipped_oversize"] += 1
                return None, None
            raw = self._fetch_body(conn, uid)
            if raw is None:
                warnings.append(f"UID {uid} 未取到正文（服务器返回空），已跳过")
                stats["parse_errors"] += 1
                return None, None
            if size is None and len(raw) > self._max_message_bytes:
                warnings.append(
                    f"UID {uid} 超过大小上限（{len(raw)} > {self._max_message_bytes} 字节），已跳过正文"
                )
                stats["skipped_oversize"] += 1
                return None, None
            try:
                parsed = parse_email(
                    raw,
                    max_body_chars=self._max_body_chars,
                    local_tz=self._clock().tzinfo,
                )
            except Exception:  # noqa: BLE001 - 单封解析失败不应中断整批
                warnings.append(f"UID {uid} 解析失败，已跳过")
                stats["parse_errors"] += 1
                return None, None
            if since is not None and parsed.date is not None and parsed.date < since:
                stats["skipped_before_since"] += 1
                return None, None
            for warning in parsed.warnings:
                warnings.append(f"UID {uid}: {warning}")
            return self._build_raw_message(conversation, parsed, uidvalidity, uid, folder), None
        except MailCollectorError as exc:
            return None, exc
        except Exception as exc:  # noqa: BLE001
            return None, self._translate_exception(exc)

    # ------------------------------------------------------------------
    # IMAP 会话操作（全部只读）
    # ------------------------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise MailCollectorClosedError()

    def _open_connection(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(self.host, self.port, self._timeout)
        if self._insecure_plaintext:
            logger.debug("使用明文 IMAP4 连接本机 mock 服务器 %s:%s", self.host, self.port)
            return ReadOnlyIMAP4(self.host, self.port, timeout=self._timeout)
        return ReadOnlyIMAP4SSL(
            self.host,
            self.port,
            ssl_context=self._ssl_context,
            timeout=self._timeout,
        )

    def _login(self, conn: Any, secret: str) -> None:
        try:
            typ, data = conn.login(self._email, secret)
        except MailCollectorError:
            raise
        except imaplib.IMAP4.error as exc:
            raise self._translate_exception(exc) from exc
        except (ssl.SSLError, OSError) as exc:
            raise self._translate_exception(exc) from exc
        if typ != "OK":
            raise MailAuthError("服务器拒绝登录")
        logger.debug("IMAP 登录成功 account=%s", mask_email(self._email))

    def _get_secret(self) -> str:
        """从注入的 CredentialStore 取授权码；空/缺失统一抛 CREDENTIAL_MISSING。"""
        try:
            raw = self._credential_store.get_secret(self._credential_key)
        except Exception:  # noqa: BLE001 - 凭据管理器故障不暴露细节
            raise MailCredentialMissingError("读取凭据失败（Windows 凭据管理器可能不可用）") from None
        secret = (raw or "").strip() if isinstance(raw, str) else ""
        if not secret:
            raise MailCredentialMissingError()
        return secret

    def _select_readonly(self, conn: Any, folder: str) -> int | None:
        """EXAMINE 选择邮箱（只读），返回 UIDVALIDITY。"""
        typ, data = conn.select(_quote_mailbox(folder), readonly=True)
        if typ != "OK":
            text = _decode_first(data)
            if "nonexistent" in text.lower():
                raise MailFolderNotFoundError(f"文件夹不存在: {folder}")
            raise MailProtocolError(f"EXAMINE {folder} 返回 {typ}")
        if not getattr(conn, "is_readonly", False):
            raise ReadOnlyViolation("邮箱未以只读方式打开")
        uidvalidity = self._response_int(conn, "UIDVALIDITY")
        if uidvalidity is None:
            # 极少数服务器不返回 UIDVALIDITY：退化为每次从 1 开始（功能受损但不写入）。
            logger.warning("服务器未返回 UIDVALIDITY，增量同步退化为每次全量读取")
        uidnext = self._response_int(conn, "UIDNEXT")
        logger.debug(
            "EXAMINE 完成 mailbox=%s uidvalidity=%s uidnext=%s", folder, uidvalidity, uidnext
        )
        return uidvalidity

    def _persist_state(self, folder: str, uidvalidity: int | None, next_uid: int) -> None:
        self._sync_state.set(
            MailSyncState(
                account_key=self._account_key,
                mailbox=folder,
                uidvalidity=uidvalidity,
                next_uid=max(1, next_uid),
                last_synced_at=self._clock(),
            )
        )

    def _resolve_next_uid(
        self,
        previous: MailSyncState | None,
        uidvalidity: int | None,
        warnings: list[str],
    ) -> int:
        if previous is None:
            return 1
        if uidvalidity is not None and previous.uidvalidity is not None and uidvalidity != previous.uidvalidity:
            warnings.append(
                f"UIDVALIDITY 从 {previous.uidvalidity} 变为 {uidvalidity}，邮箱可能被重建，已从 UID 1 重新同步"
            )
            return 1
        return max(1, previous.next_uid)

    def _search_uids_from(self, conn: Any, next_uid: int) -> list[int]:
        """``UID SEARCH UID next:*`` 并过滤掉 < next_uid 的结果。

        注意 RFC 3501 的 ``n:*`` 语义：即使 n 大于最大 UID，服务器也会返回最大 UID 那封邮件
        （QQ 邮箱如此），因此这里必须显式过滤，否则会重复同步最后一封邮件。
        """
        typ, data = conn.uid("SEARCH", "UID", f"{next_uid}:*")
        if typ != "OK":
            raise MailProtocolError(f"UID SEARCH 返回 {typ}")
        uids = _parse_uid_list(data)
        return sorted(uid for uid in uids if uid >= next_uid)

    def _fetch_size(self, conn: Any, uid: int) -> int | None:
        """先取 RFC822.SIZE（不下载正文），用于大小限制判断。"""
        typ, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
        if typ != "OK":
            raise MailProtocolError(f"UID FETCH RFC822.SIZE 返回 {typ}")
        for item in data or []:
            chunk = item[0] if isinstance(item, tuple) and item else item
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            match = re.search(rb"RFC822\.SIZE\s+(\d+)", bytes(chunk), re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _fetch_body(self, conn: Any, uid: int) -> bytes | None:
        """``BODY.PEEK[]`` 取整封原始邮件（不设置 \\Seen 标志）。"""
        typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise MailProtocolError(f"UID FETCH BODY.PEEK[] 返回 {typ}")
        for item in data or []:
            if not (isinstance(item, tuple) and len(item) == 2):
                continue
            prefix, literal = item
            prefix_bytes = bytes(prefix) if isinstance(prefix, (bytes, bytearray)) else b""
            if isinstance(literal, (bytes, bytearray)) and b"BODY[]" in prefix_bytes.upper():
                return bytes(literal)
        return None

    def _response_int(self, conn: Any, code: str) -> int | None:
        try:
            _typ, data = conn.response(code)
        except Exception:  # noqa: BLE001
            return None
        for item in data or []:
            if isinstance(item, (bytes, bytearray)):
                try:
                    return int(bytes(item).strip())
                except (TypeError, ValueError):
                    continue
            if isinstance(item, str):
                try:
                    return int(item.strip())
                except ValueError:
                    continue
        return None

    # ------------------------------------------------------------------
    # 数据转换 / 异常翻译
    # ------------------------------------------------------------------
    def _build_raw_message(
        self,
        conversation: Conversation,
        parsed: ParsedMail,
        uidvalidity: int | None,
        uid: int,
        folder: str,
    ) -> RawMessage:
        subject = parsed.subject.strip()
        body = parsed.body_text
        if subject and body:
            text = f"{subject}\n\n{body}"
        elif subject:
            text = subject
        else:
            text = body
        sender = parsed.from_name or parsed.from_address or "（未知发件人）"
        external_id = parsed.message_id or f"imap:{uidvalidity}:{uid}"
        extras: dict[str, Any] = {
            "subject": subject,
            "from_address": parsed.from_address,
            "to": list(parsed.to),
            "message_id": parsed.message_id,
            "mailbox": folder,
            "uid": uid,
            "uidvalidity": uidvalidity,
            "attachment_count": parsed.attachment_count,
            "attachment_bytes": parsed.attachment_bytes,
            "has_attachments": parsed.attachment_count > 0,
            # 只读展示用：MIME 头里的附件文件名（最多 10 个、每个截断 120 字符；不含内容）。
            "attachment_names": list(parsed.attachment_names),
            "content_type": parsed.content_type,
            "body_truncated": parsed.truncated,
            "parse_warnings": list(parsed.warnings),
        }
        return RawMessage(
            conversation_key=conversation.id,
            sender=sender,
            is_self=_same_address(parsed.from_address, self._email),
            text=text,
            timestamp=parsed.date,
            timestamp_source=TimestampSource.UI if parsed.date else TimestampSource.COLLECTED,
            raw_time_text=parsed.date_raw,
            external_id=external_id,
            source_locator=f"IMAP {folder} UID {uid} (UIDVALIDITY {uidvalidity})",
            extras=extras,
        )

    def _translate_exception(self, exc: BaseException) -> MailCollectorError:
        """把网络/协议/SSL 异常映射为稳定错误码（信息脱敏，不含授权码/正文）。"""
        return translate_imap_exception(exc)

    # ------------------------------------------------------------------
    def _conversation_id(self, folder: str) -> str:
        account_part = self.account.id or mask_email(self._email)
        return f"mail:{account_part}:{folder}"

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"MailCollector(account={self.account.id!r}, host={self.host!r}, port={self.port}, "
            f"folders={self._folders!r})"
        )

    def __enter__(self) -> "MailCollector":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def translate_imap_exception(exc: BaseException) -> MailCollectorError:
    """把网络/协议/SSL/imaplib 异常映射为稳定错误码（信息脱敏，不含授权码/正文）。

    独立函数形式便于 ``tools/mail_real_check.py`` 等外部工具复用同一套错误码映射。
    """
    if isinstance(exc, MailCollectorError):
        return exc
    message = f"{type(exc).__name__}: {exc}"
    lowered = message.lower()
    if isinstance(exc, ssl.SSLError):
        return MailTlsError()
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return MailTimeoutError()
    if isinstance(exc, imaplib.IMAP4.abort):
        return MailConnectionError("服务器连接中断")
    if isinstance(exc, imaplib.IMAP4.error):
        if any(marker in lowered for marker in _AUTH_MARKERS):
            return MailAuthError()
        if "nonexistent" in lowered or "unknown mailbox" in lowered:
            return MailFolderNotFoundError()
        return MailProtocolError(message)
    if isinstance(exc, (ConnectionError, OSError)):
        return MailConnectionError(message)
    return MailCollectorError(message, code=MailErrorCode.UNEXPECTED)


def _dedupe(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _quote_mailbox(folder: str) -> str:
    """imaplib 不会自动引用邮箱名；含空格/非 ASCII 的名称需要按 IMAP quoted-string 转义。

    QQ 中文文件夹名需要 modified-UTF-7 编码，MVP 暂不支持（默认只采集 INBOX）。
    """
    if re.search(r"[^\x21-\x7e]", folder) or " " in folder:
        escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return folder


def _exists_count(conn: Any) -> int | None:
    """EXAMINE 之后从 untagged EXISTS 响应里取邮件总数。"""
    responses = getattr(conn, "untagged_responses", None) or {}
    values = responses.get("EXISTS") or []
    if not values:
        return None
    return _int_or_none(values[-1])


def _folder_title(folder: str) -> str:
    return _FOLDER_TITLES.get(folder.upper(), folder)


def _same_address(left: str, right: str) -> bool:
    return bool(left) and bool(right) and left.strip().lower() == right.strip().lower()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, (bytes, bytearray)):
        try:
            return int(bytes(value).strip())
        except (TypeError, ValueError):
            return None
    if isinstance(value, int):
        return value
    return None


def _decode_first(data: Any) -> str:
    if not data:
        return ""
    first = data[0] if isinstance(data, (list, tuple)) else data
    if isinstance(first, (bytes, bytearray)):
        return bytes(first).decode("utf-8", "replace")
    return str(first)


def _parse_uid_list(data: Any) -> list[int]:
    uids: set[int] = set()
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, (bytes, bytearray)):
            text = bytes(item).decode("ascii", "ignore")
        else:
            text = str(item)
        for token in text.split():
            if token.isdigit():
                uids.add(int(token))
    return sorted(uids)


def _tls_summary(conn: Any) -> dict[str, Any] | None:
    """只读会话的 TLS 元信息（不含证书内容；连接不是 SSL 时返回 None）。"""
    sock = getattr(conn, "sock", None)
    if sock is None or not hasattr(sock, "version"):
        return None
    try:
        cipher = sock.cipher()
        return {"version": sock.version(), "cipher": cipher[0] if cipher else None}
    except Exception:  # noqa: BLE001
        return None


# 供 isinstance(collector, MessageSource) 的运行时协议检查（protocol 是 runtime_checkable）。
__all__ = [
    "MailCollector",
    "MessageSource",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_FOLDERS",
    "DEFAULT_MAX_MESSAGE_BYTES",
    "DEFAULT_TIMEOUT",
    "mail_credential_key",
    "mask_email",
    "translate_imap_exception",
]
