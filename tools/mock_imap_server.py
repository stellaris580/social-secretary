"""本地 IMAP4rev1 子集 mock 服务器（仅用于测试/演示，只监听 127.0.0.1）。

用途
----
在没有真实授权码的情况下，端到端验证 ``collectors/mail`` 只读采集器：
首次同步、UID 增量同步、UIDVALIDITY 变化重同步、认证失败、超时、MIME 解析等。

安全特性
--------
- 只绑定回环地址，不对外提供服务；
- 记录客户端发送的全部命令（LOGIN 的口令参数在日志中脱敏为 ``***``）；
- 对 STORE / EXPUNGE / DELETE / APPEND / COPY / MOVE / CLOSE 等写命令直接返回 ``NO``，
  并记录到 ``write_attempts``，测试据此断言"采集器从未发送写命令"；
- ``BODY[]``（非 PEEK）会被额外记录到 ``non_peek_fetch_uids`` —— 用于证明采集器只用 BODY.PEEK。

支持的命令子集（够用且可断言）::

    CAPABILITY
    NOOP
    LOGIN <user> <password>            # 单行参数，支持 quoted-string
    EXAMINE <mailbox> / SELECT <mailbox>
    UID SEARCH <criteria>              # 支持 UID <set> / ALL
    UID FETCH <set> <items>            # 支持 RFC822.SIZE / BODY.PEEK[] / BODY[] / FLAGS
    FETCH <seq-set> <items>            # 按序号，仅测试用
    LIST / NAMESPACE / ID / LOGOUT

命令行自测::

    .venv\\Scripts\\python.exe tools\\mock_imap_server.py --messages 3
"""

from __future__ import annotations

import argparse
import socketserver
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from typing import Any

#: 明确拒绝的写命令（命中即返回 NO 并记入 write_attempts）。
WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "STORE",
        "EXPUNGE",
        "DELETE",
        "APPEND",
        "COPY",
        "MOVE",
        "CREATE",
        "RENAME",
        "SUBSCRIBE",
        "UNSUBSCRIBE",
        "CLOSE",
        "SETACL",
        "SETQUOTA",
    }
)

#: mock 声明支持的能力。
CAPABILITIES = "IMAP4rev1 AUTH=PLAIN AUTH=LOGIN ID NAMESPACE"
_MAX_LINE = 64 * 1024
_MAX_UID = 2**32 - 1


@dataclass
class MockMessage:
    """脚本化邮箱里的一封邮件（raw 为完整 RFC822 字节）。"""

    uid: int
    raw: bytes
    flags: tuple[str, ...] = ()
    internaldate: str | None = None


@dataclass
class MockMailbox:
    """脚本化邮箱：UIDVALIDITY + 有序列出（按 UID 升序）的邮件。"""

    name: str = "INBOX"
    uidvalidity: int = 1
    messages: list[MockMessage] = field(default_factory=list)

    def next_uid(self) -> int:
        return self.messages[-1].uid + 1 if self.messages else 1

    def sorted_messages(self) -> list[MockMessage]:
        return sorted(self.messages, key=lambda message: message.uid)


@dataclass
class LoggedCommand:
    """一条客户端命令记录（args 已脱敏，不含授权码）。"""

    seq: int
    at: str
    tag: str
    verb: str
    args: str
    redacted: bool = False


@dataclass
class FetchRecord:
    """一次 FETCH 的记录，供测试断言增量范围与 BODY.PEEK 行为。"""

    set_token: str
    items: tuple[str, ...]
    uids: tuple[int, ...]
    uid_mode: bool = True


class _ClientState:
    """单个连接的状态（每个 TCP 连接一份，线程内使用）。"""

    def __init__(self) -> None:
        self.authenticated = False
        self.selected: str | None = None
        self.readonly = False


class MockImapServer:
    """可注入脚本化邮箱的 IMAP4rev1 mock 服务器。

    参数
    ----
    username / password: 登录凭据（测试用合成值，默认 test@qq.com / mock-auth-code）。
    mailbox:           初始邮箱（默认空 INBOX）。
    host / port:       默认 127.0.0.1:0（随机端口）。
    greeting_delay:    接受连接后延迟多少秒再发 greeting（测试客户端超时）。
    drop_after_login:  登录成功后立即断开连接（测试断线降级）。
    """

    def __init__(
        self,
        *,
        username: str = "test@qq.com",
        password: str = "mock-auth-code",
        mailbox: MockMailbox | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        greeting_delay: float = 0.0,
        drop_after_login: bool = False,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("mock 服务器只允许监听 127.0.0.1")
        self.username = username
        self.password = password
        self.mailbox = mailbox or MockMailbox()
        self.host = host
        self.requested_port = port
        self.greeting_delay = max(0.0, float(greeting_delay))
        self.drop_after_login = bool(drop_after_login)

        self._lock = threading.RLock()
        self._clients: set[socketserver.StreamRequestHandler] = set()
        self._server: _MockTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._listen_port: int | None = None

        # 命令日志 / 计数器（测试断言用；不含敏感信息）。
        self.commands: list[LoggedCommand] = []
        self.write_attempts: list[LoggedCommand] = []
        self.fetch_sets: list[FetchRecord] = []
        self.body_fetch_uids: list[int] = []
        self.non_peek_fetch_uids: list[int] = []
        self.login_attempts = 0
        self.login_successes = 0
        self.connection_count = 0
        self.examine_count = 0
        self.select_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> "MockImapServer":
        if self._server is not None:
            raise RuntimeError("MockImapServer 已经启动")
        self._server = _MockTCPServer((self.host, self.requested_port), _MockHandler, self)
        self._listen_port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"mock-imap-{self._listen_port}",
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        if self._listen_port is None:
            raise RuntimeError("MockImapServer 尚未启动")
        return self._listen_port

    def stop(self) -> None:
        self.drop_connections()
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None:
            thread.join(timeout=3.0)

    def __enter__(self) -> "MockImapServer":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # 测试辅助 API
    # ------------------------------------------------------------------
    def clear_log(self) -> None:
        """清空命令日志与计数器（便于分阶段断言）。"""
        with self._lock:
            self.commands.clear()
            self.write_attempts.clear()
            self.fetch_sets.clear()
            self.body_fetch_uids.clear()
            self.non_peek_fetch_uids.clear()
            self.login_attempts = 0
            self.login_successes = 0
            self.connection_count = 0
            self.examine_count = 0
            self.select_count = 0

    def add_message(self, raw: bytes, *, uid: int | None = None) -> MockMessage:
        """追加一封脚本化邮件（uid 省略时自动取 max+1）。"""
        with self._lock:
            next_uid = uid if uid is not None else self.mailbox.next_uid()
            if next_uid <= 0 or next_uid > _MAX_UID:
                raise ValueError("uid 超出范围")
            if any(message.uid == next_uid for message in self.mailbox.messages):
                raise ValueError(f"uid {next_uid} 已存在")
            message = MockMessage(uid=next_uid, raw=bytes(raw))
            self.mailbox.messages.append(message)
            self.mailbox.messages.sort(key=lambda item: item.uid)
            return message

    def set_uidvalidity(self, uidvalidity: int) -> None:
        with self._lock:
            if uidvalidity < 0:
                raise ValueError("uidvalidity 不能为负数")
            self.mailbox.uidvalidity = int(uidvalidity)

    def replace_messages(self, raws: list[bytes], *, uidvalidity: int | None = None) -> None:
        """模拟邮箱重建：整体替换邮件列表（UID 从 1 重新编号），可同时改 UIDVALIDITY。"""
        with self._lock:
            self.mailbox.messages = [
                MockMessage(uid=index + 1, raw=bytes(raw)) for index, raw in enumerate(raws)
            ]
            if uidvalidity is not None:
                self.mailbox.uidvalidity = int(uidvalidity)

    def drop_connections(self) -> None:
        """强制关闭所有客户端连接（模拟断网/服务器重启）。"""
        with self._lock:
            handlers = list(self._clients)
        for handler in handlers:
            try:
                handler.connection.shutdown(2)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            try:
                handler.connection.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    @property
    def verbs(self) -> list[str]:
        with self._lock:
            return [command.verb for command in self.commands]

    # ------------------------------------------------------------------
    # 供 handler 线程调用
    # ------------------------------------------------------------------
    def _register(self, handler: "socketserver.StreamRequestHandler") -> None:
        with self._lock:
            self._clients.add(handler)
            self.connection_count += 1

    def _unregister(self, handler: "socketserver.StreamRequestHandler") -> None:
        with self._lock:
            self._clients.discard(handler)

    def greeting(self) -> bytes:
        return f"* OK [CAPABILITY {CAPABILITIES}] MockImapServer ready\r\n".encode()

    def record(self, tag: str, verb: str, args: str) -> LoggedCommand:
        shown_args = _redact_args(verb, args)
        command = LoggedCommand(
            seq=len(self.commands) + 1,
            at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            tag=tag,
            verb=verb,
            args=shown_args,
            redacted=(verb == "LOGIN"),
        )
        with self._lock:
            self.commands.append(command)
            if verb in WRITE_COMMANDS:
                self.write_attempts.append(command)
        return command

    def process_line(self, state: _ClientState, line: bytes) -> tuple[bytes, bool]:
        """解析并响应一行命令；返回 (要写回的字节, 是否在写完后关闭连接)。"""
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if not text.strip():
            return b"", False
        parts = text.split(" ", 2)
        tag = parts[0]
        if len(parts) < 2:
            return _tagged(tag, "BAD", "缺少命令"), False
        verb = parts[1].upper()
        args = parts[2] if len(parts) > 2 else ""
        self.record(tag, verb, args)

        if verb in WRITE_COMMANDS:
            return _tagged(tag, "NO", f"[READONLY] mock 服务器拒绝写命令 {verb}"), False
        if verb == "CAPABILITY":
            return (
                f"* CAPABILITY {CAPABILITIES}\r\n".encode()
                + _tagged(tag, "OK", "CAPABILITY completed"),
                False,
            )
        if verb == "NOOP":
            return _tagged(tag, "OK", "NOOP completed"), False
        if verb == "LOGIN":
            return self._handle_login(state, tag, args)
        if verb in ("EXAMINE", "SELECT"):
            return self._handle_select(state, tag, verb, args)
        if verb == "UID":
            return self._handle_uid(state, tag, args)
        if verb == "FETCH":
            return self._handle_fetch(state, tag, args, uid_mode=False)
        if verb == "LIST":
            return self._handle_list(tag, args)
        if verb == "NAMESPACE":
            return (
                b'* NAMESPACE (("" "/")) NIL NIL\r\n' + _tagged(tag, "OK", "NAMESPACE completed"),
                False,
            )
        if verb == "ID":
            return (
                b'* ID ("name" "MockImapServer")\r\n' + _tagged(tag, "OK", "ID completed"),
                False,
            )
        if verb == "LOGOUT":
            return (
                b"* BYE mock server logging out\r\n" + _tagged(tag, "OK", "LOGOUT completed"),
                True,
            )
        return _tagged(tag, "BAD", f"Unsupported command {verb}"), False

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------
    def _handle_login(self, state: _ClientState, tag: str, args: str) -> tuple[bytes, bool]:
        if state.authenticated:
            return _tagged(tag, "BAD", "already authenticated"), False
        tokens = _tokenize(args)
        self.login_attempts += 1
        if len(tokens) >= 2 and tokens[0] == self.username and tokens[1] == self.password:
            state.authenticated = True
            self.login_successes += 1
            return _tagged(tag, "OK", "LOGIN completed"), self.drop_after_login
        return _tagged(tag, "NO", "[AUTHENTICATIONFAILED] invalid credentials"), False

    def _handle_select(
        self, state: _ClientState, tag: str, verb: str, args: str
    ) -> tuple[bytes, bool]:
        if not state.authenticated:
            return _tagged(tag, "NO", "[AUTHENTICATIONFAILED] must login first"), False
        tokens = _tokenize(args)
        name = tokens[0] if tokens else self.mailbox.name
        if name.strip('"').upper() != self.mailbox.name.upper():
            return _tagged(tag, "NO", "[NONEXISTENT] Unknown mailbox"), False
        with self._lock:
            messages = self.mailbox.sorted_messages()
            uidvalidity = self.mailbox.uidvalidity
            next_uid = self.mailbox.next_uid()
        state.selected = self.mailbox.name
        state.readonly = verb == "EXAMINE"
        if verb == "EXAMINE":
            self.examine_count += 1
            status = "READ-ONLY"
        else:
            self.select_count += 1
            status = "READ-WRITE"
        lines = [
            b"* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)\r\n",
            f"* {len(messages)} EXISTS\r\n".encode(),
            b"* 0 RECENT\r\n",
            f"* OK [UIDVALIDITY {uidvalidity}] UIDs valid\r\n".encode(),
            f"* OK [UIDNEXT {next_uid}] Predicted next UID\r\n".encode(),
            _tagged(tag, "OK", f"[{status}] {verb} completed"),
        ]
        return b"".join(lines), False

    def _handle_uid(self, state: _ClientState, tag: str, args: str) -> tuple[bytes, bool]:
        if state.selected is None:
            return _tagged(tag, "BAD", "no mailbox selected"), False
        tokens = _tokenize(args)
        if not tokens:
            return _tagged(tag, "BAD", "missing UID subcommand"), False
        sub = tokens[0].upper()
        if sub == "SEARCH":
            return self._handle_search(tag, tokens[1:])
        if sub == "FETCH":
            return self._handle_fetch(state, tag, " ".join(tokens[1:]), uid_mode=True)
        return _tagged(tag, "BAD", f"Unsupported UID subcommand {sub}"), False

    def _handle_search(self, tag: str, criteria: list[str]) -> tuple[bytes, bool]:
        with self._lock:
            uids = [message.uid for message in self.mailbox.sorted_messages()]
        matched: set[int] = set(uids)
        index = 0
        try:
            while index < len(criteria):
                key = criteria[index].upper()
                if key == "ALL":
                    index += 1
                    continue
                if key == "UID":
                    if index + 1 >= len(criteria):
                        return _tagged(tag, "BAD", "UID 缺少参数"), False
                    matched &= set(_expand_uid_set(criteria[index + 1], uids))
                    index += 2
                    continue
                return _tagged(tag, "BAD", f"Unsupported SEARCH criteria {criteria[index]}"), False
        except ValueError:
            return _tagged(tag, "BAD", "invalid UID set"), False
        ordered = [uid for uid in uids if uid in matched]
        search_line = "* SEARCH" + "".join(f" {uid}" for uid in ordered) + "\r\n"
        return search_line.encode() + _tagged(tag, "OK", "SEARCH completed"), False

    def _handle_fetch(
        self, state: _ClientState, tag: str, args: str, *, uid_mode: bool
    ) -> tuple[bytes, bool]:
        if state.selected is None:
            return _tagged(tag, "BAD", "no mailbox selected"), False
        tokens = _tokenize(args)
        if len(tokens) < 2:
            return _tagged(tag, "BAD", "FETCH 缺少参数"), False
        set_token = tokens[0]
        items_text = " ".join(tokens[1:])
        parsed_items = _parse_fetch_items(items_text)
        if parsed_items is None:
            return _tagged(tag, "BAD", f"Unsupported FETCH items {items_text}"), False
        with self._lock:
            messages = self.mailbox.sorted_messages()
        uids = [message.uid for message in messages]
        sequence = {message.uid: position + 1 for position, message in enumerate(messages)}
        try:
            if uid_mode:
                matched_uids = _expand_uid_set(set_token, uids)
            else:
                matched = _expand_sequence_set(set_token, len(messages))
                matched_uids = [uids[index] for index in matched]
        except ValueError:
            return _tagged(tag, "BAD", "invalid message set"), False

        body_requested = any(
            item in ("BODY.PEEK[]", "BODY[]", "RFC822") for item in parsed_items
        )
        self.fetch_sets.append(
            FetchRecord(
                set_token=set_token,
                items=tuple(sorted(parsed_items)),
                uids=tuple(matched_uids),
                uid_mode=uid_mode,
            )
        )
        if body_requested:
            self.body_fetch_uids.extend(matched_uids)
            if any(item in ("BODY[]", "RFC822") for item in parsed_items):
                self.non_peek_fetch_uids.extend(matched_uids)

        by_uid = {message.uid: message for message in messages}
        response = bytearray()
        for uid in matched_uids:
            message = by_uid[uid]
            prefix = f"* {sequence[uid]} FETCH (UID {uid}"
            if "RFC822.SIZE" in parsed_items:
                prefix += f" RFC822.SIZE {len(message.raw)}"
            if "FLAGS" in parsed_items:
                prefix += f" FLAGS ({' '.join(message.flags)})"
            if "INTERNALDATE" in parsed_items and message.internaldate:
                prefix += f' INTERNALDATE "{message.internaldate}"'
            wants_body = body_requested
            if wants_body and any(item in ("BODY[]", "RFC822") for item in parsed_items):
                # 非 PEEK 读取按 RFC 语义会设置 \Seen（采集器不应走到这里）。
                if "\\Seen" not in message.flags:
                    message.flags = tuple(message.flags) + ("\\Seen",)
            if wants_body:
                response += f"{prefix} BODY[] {{{len(message.raw)}}}\r\n".encode()
                response += message.raw
                response += b")\r\n"
            else:
                response += f"{prefix})\r\n".encode()
        return bytes(response) + _tagged(tag, "OK", "FETCH completed"), False

    def _handle_list(self, tag: str, args: str) -> tuple[bytes, bool]:
        name = self.mailbox.name
        line = f'* LIST (\\HasNoChildren) "/" "{name}"\r\n'
        return line.encode() + _tagged(tag, "OK", "LIST completed"), False


# ---------------------------------------------------------------------------
# 协议解析小工具
# ---------------------------------------------------------------------------

def _tagged(tag: str, status: str, text: str) -> bytes:
    return f"{tag} {status} {text}\r\n".encode()


def _tokenize(text: str) -> list[str]:
    """按 IMAP 语法切分参数：支持 quoted-string（含转义）与 atom；括号整体当 atom。"""
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] == " ":
            index += 1
        if index >= length:
            break
        char = text[index]
        if char == '"':
            index += 1
            buffer: list[str] = []
            while index < length:
                current = text[index]
                if current == "\\" and index + 1 < length:
                    buffer.append(text[index + 1])
                    index += 2
                    continue
                if current == '"':
                    index += 1
                    break
                buffer.append(current)
                index += 1
            tokens.append("".join(buffer))
        else:
            start = index
            while index < length and text[index] != " ":
                index += 1
            tokens.append(text[start:index])
    return tokens


def _redact_args(verb: str, args: str) -> str:
    """LOGIN 参数脱敏：保留用户名，口令一律替换为 ***（日志/证据绝不出现授权码）。"""
    if verb != "LOGIN":
        return args
    tokens = _tokenize(args)
    if not tokens:
        return "***"
    return f'"{tokens[0]}" "***"'


def _expand_uid_set(token: str, uids: list[int]) -> list[int]:
    """展开 UID set；实现 RFC 3501 ``n:*`` 语义（n 超过最大 UID 时仍包含最后一封）。"""
    if not uids:
        return []
    max_uid = uids[-1]
    selected: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start_text, end_text = part.split(":", 1)
            start = max_uid if start_text == "*" else int(start_text)
            end = max_uid if end_text == "*" else int(end_text)
            if end_text == "*" and start > max_uid:
                # RFC 3501 6.4.8: n:* 且 n 大于最大 UID 时仍包含最后一封。
                selected.add(max_uid)
                continue
            if start > end:
                raise ValueError("invalid range")
            selected.update(uid for uid in uids if start <= uid <= end)
        else:
            value = int(part)
            selected.update(uid for uid in uids if uid == value)
    return [uid for uid in uids if uid in selected]


def _expand_sequence_set(token: str, count: int) -> list[int]:
    """展开序号 set（1-based），返回 0-based 下标列表；``*`` 表示最后一封。"""
    if count == 0:
        return []
    last = count
    selected: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start_text, end_text = part.split(":", 1)
            start = last if start_text == "*" else int(start_text)
            end = last if end_text == "*" else int(end_text)
            if start > end:
                raise ValueError("invalid range")
            selected.update(index for index in range(start, end + 1) if 1 <= index <= count)
        else:
            value = int(part)
            if 1 <= value <= count:
                selected.add(value)
    return [index - 1 for index in sorted(selected)]


def _parse_fetch_items(text: str) -> set[str] | None:
    """解析 FETCH 数据项；未知项返回 None（mock 返回 BAD）。"""
    value = text.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if not value.strip():
        return None
    items: set[str] = set()
    for token in value.split():
        upper = token.upper()
        if upper in ("RFC822.SIZE", "FLAGS", "UID", "INTERNALDATE"):
            items.add(upper)
        elif upper == "BODY.PEEK[]":
            items.add("BODY.PEEK[]")
        elif upper in ("BODY[]", "RFC822"):
            items.add(upper)
        else:
            return None
    return items


# ---------------------------------------------------------------------------
# socketserver 实现
# ---------------------------------------------------------------------------

class _MockTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler: type, mock: MockImapServer) -> None:
        self.mock = mock
        super().__init__(server_address, handler)


class _MockHandler(socketserver.StreamRequestHandler):
    """每个连接的协议循环：读一行 → 交给 MockImapServer.process_line → 写回。"""

    timeout = 30.0

    def handle(self) -> None:
        server: MockImapServer = self.server.mock  # type: ignore[attr-defined]
        state = _ClientState()
        server._register(self)
        try:
            if self.server.mock.greeting_delay:  # type: ignore[attr-defined]
                time.sleep(self.server.mock.greeting_delay)  # type: ignore[attr-defined]
            if not self._send(server.greeting()):
                return
            while True:
                line = self.rfile.readline(_MAX_LINE)
                if not line:
                    return
                data, close = server.process_line(state, line)
                if data and not self._send(data):
                    return
                if close:
                    return
        except (OSError, ConnectionError):
            return
        finally:
            server._unregister(self)

    def _send(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (OSError, ValueError, ConnectionError):
            return False


# ---------------------------------------------------------------------------
# 合成邮件 & CLI
# ---------------------------------------------------------------------------

def build_sample_message(
    subject: str,
    body: str,
    *,
    from_addr: str = "sender@example.com",
    from_name: str = "合成发件人",
    to_addr: str = "test@qq.com",
    date: datetime | None = None,
) -> bytes:
    """构造一封合成邮件（测试/CLI 演示用；无真实送达地址）。"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{from_name} <{from_addr}>"
    message["To"] = to_addr
    message["Message-ID"] = f"<mock-{abs(hash((subject, body))) % 10**10}@example.com>"
    stamp = date or datetime.now(timezone.utc)
    message["Date"] = format_datetime(stamp)
    message.set_content(body)
    return message.as_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 IMAP4rev1 mock 服务器（只读测试用）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--messages", type=int, default=3)
    parser.add_argument("--uidvalidity", type=int, default=1)
    parser.add_argument("--user", default="test@qq.com")
    args = parser.parse_args(argv)

    mailbox = MockMailbox(uidvalidity=args.uidvalidity)
    for index in range(max(0, args.messages)):
        mailbox.messages.append(
            MockMessage(
                uid=index + 1,
                raw=build_sample_message(
                    f"合成邮件 #{index + 1}",
                    f"这是第 {index + 1} 封合成邮件正文，仅用于本机 mock 测试。",
                ),
            )
        )
    server = MockImapServer(mailbox=mailbox, username=args.user)
    server.start()
    print(f"Mock IMAP 已启动: 127.0.0.1:{server.port}")
    print(f"用户名: {args.user}  口令: ***（见 --user / 默认 mock-auth-code）")
    print("按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        print("Mock IMAP 已停止。")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())


__all__ = [
    "CAPABILITIES",
    "WRITE_COMMANDS",
    "MockMessage",
    "MockMailbox",
    "LoggedCommand",
    "FetchRecord",
    "MockImapServer",
    "build_sample_message",
    "main",
]
