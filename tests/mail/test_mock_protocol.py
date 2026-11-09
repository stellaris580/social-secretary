"""用标准库 ``imaplib``（独立于采集器实现）验证 mock 服务器的 IMAP4rev1 兼容性与只读守卫。

这些测试证明 mock 不是"只跟采集器对得上"的假协议：真实 IMAP 客户端能完成
CAPABILITY / LOGIN / EXAMINE / UID SEARCH / UID FETCH / LOGOUT 全流程。
"""

from __future__ import annotations

import imaplib
import socket

import pytest

from .helpers import make_email


def _read_line(sock: socket.socket, buffer: bytes) -> tuple[bytes, bytes]:
    while b"\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("mock 服务器意外关闭连接")
        buffer += chunk
    line, _, rest = buffer.partition(b"\r\n")
    return line + b"\r\n", rest


def _read_until_tagged(sock: socket.socket, buffer: bytes, tag: bytes) -> tuple[list[bytes], bytes]:
    lines: list[bytes] = []
    while True:
        line, buffer = _read_line(sock, buffer)
        lines.append(line)
        if line.startswith(tag + b" "):
            return lines, buffer


def _first_literal(data) -> bytes:
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    raise AssertionError(f"响应中没有 literal: {data!r}")


def _raw_connect(server) -> tuple[socket.socket, bytes]:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    greeting, buffer = _read_line(sock, b"")
    assert greeting.startswith(b"* OK "), greeting
    return sock, buffer


def test_imaplib_roundtrip(mock_server, mailbox, mock_module):
    """标准 IMAP4 客户端：CAPABILITY → LOGIN → EXAMINE 只读 → SEARCH → FETCH → LOGOUT。"""
    mailbox.messages.append(
        mock_module.MockMessage(uid=1, raw=make_email(subject="协议自检", body="mock imaplib 正文"))
    )
    conn = imaplib.IMAP4("127.0.0.1", mock_server.port, timeout=5)
    try:
        assert "IMAP4REV1" in conn.capabilities
        typ, _ = conn.login("test@qq.com", "mock-auth-code")
        assert typ == "OK"

        typ, data = conn.select("INBOX", readonly=True)
        assert typ == "OK"
        assert conn.is_readonly is True
        assert data[0] == b"1"
        typ, uidvalidity = conn.response("UIDVALIDITY")
        assert typ == "UIDVALIDITY"
        assert uidvalidity[0] == b"100"

        typ, data = conn.uid("SEARCH", "UID", "1:*")
        assert typ == "OK"
        assert data[0] == b"1"

        typ, data = conn.uid("FETCH", "1", "(BODY.PEEK[])")
        assert typ == "OK"
        raw = _first_literal(data)
        assert b"Subject: " in raw or b"Subject:".replace(b":", b":") in raw

        typ, data = conn.uid("FETCH", "1", "(RFC822.SIZE)")
        assert typ == "OK"

        typ, _ = conn.logout()
        assert typ in ("OK", "BYE")
    finally:
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass

    assert "EXAMINE" in mock_server.verbs
    assert mock_server.write_attempts == []
    assert mock_server.non_peek_fetch_uids == []


def test_mock_returns_uidvalidity_and_uidnext(mock_server, mailbox, mock_module):
    mailbox.messages.append(mock_module.MockMessage(uid=7, raw=make_email()))
    conn = imaplib.IMAP4("127.0.0.1", mock_server.port, timeout=5)
    try:
        conn.login("test@qq.com", "mock-auth-code")
        conn.select("INBOX", readonly=True)
        _typ, uidvalidity = conn.response("UIDVALIDITY")
        _typ, uidnext = conn.response("UIDNEXT")
        assert uidvalidity[0] == b"100"
        assert uidnext[0] == b"8"
    finally:
        conn.logout()


def test_login_failure_is_rejected(mock_server):
    conn = imaplib.IMAP4("127.0.0.1", mock_server.port, timeout=5)
    try:
        with pytest.raises(imaplib.IMAP4.error):
            conn.login("test@qq.com", "wrong-code")
        assert mock_server.login_attempts == 1
        assert mock_server.login_successes == 0
    finally:
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass
    # 授权码明文绝不能进入命令日志（mock 内部也已脱敏）。
    for command in mock_server.commands:
        assert "wrong-code" not in command.args
        assert "wrong-code" not in command.verb


def test_unknown_mailbox_rejected(mock_server):
    conn = imaplib.IMAP4("127.0.0.1", mock_server.port, timeout=5)
    try:
        conn.login("test@qq.com", "mock-auth-code")
        typ, data = conn.select("NoSuchFolder", readonly=True)
        assert typ == "NO"
        assert b"NONEXISTENT" in data[0].upper()
    finally:
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass


def test_bodystructure_peek_does_not_set_seen_but_body_does(mock_server, mailbox, mock_module):
    """证明 mock 的 \\Seen 语义与真实 IMAP 一致：BODY.PEEK 不置已读，BODY[] 置已读。"""
    mailbox.messages.append(mock_module.MockMessage(uid=1, raw=make_email()))
    conn = imaplib.IMAP4("127.0.0.1", mock_server.port, timeout=5)
    try:
        conn.login("test@qq.com", "mock-auth-code")
        conn.select("INBOX", readonly=True)
        conn.uid("FETCH", "1", "(BODY.PEEK[])")
        assert mailbox.messages[0].flags == ()
        assert mock_server.non_peek_fetch_uids == []

        conn.uid("FETCH", "1", "(BODY[])")
        assert "\\Seen" in mailbox.messages[0].flags
        assert mock_server.non_peek_fetch_uids == [1]
    finally:
        conn.logout()


def test_raw_socket_write_commands_are_rejected(mock_server):
    """绕过客户端直接发写命令：mock 返回 NO，记录 write_attempts，邮件 flags 不变。"""
    sock, buffer = _raw_connect(mock_server)
    try:
        sock.sendall(b"t1 LOGIN test@qq.com mock-auth-code\r\n")
        lines, buffer = _read_until_tagged(sock, buffer, b"t1")
        assert lines[-1].startswith(b"t1 OK")

        sock.sendall(b"t2 STORE 1 +FLAGS (\\Seen)\r\n")
        lines, buffer = _read_until_tagged(sock, buffer, b"t2")
        assert lines[-1].startswith(b"t2 NO"), lines
        assert [command.verb for command in mock_server.write_attempts] == ["STORE"]
        assert mock_server.write_attempts[0].args == "1 +FLAGS (\\Seen)"

        for command in (
            b"t3 EXPUNGE\r\n",
            b"t4 DELETE INBOX\r\n",
            b"t5 APPEND INBOX {5}\r\n",
            b"t6 CLOSE\r\n",
        ):
            sock.sendall(command)
            tag = command.split(b" ", 1)[0]
            lines, buffer = _read_until_tagged(sock, buffer, tag)
            assert lines[-1].split(b" ", 2)[1] == b"NO"
        verbs = [command.verb for command in mock_server.write_attempts]
        assert verbs == ["STORE", "EXPUNGE", "DELETE", "APPEND", "CLOSE"]

        sock.sendall(b"t7 LOGOUT\r\n")
        _read_until_tagged(sock, buffer, b"t7")
    finally:
        sock.close()


def test_uid_set_rfc_quirk(mock_server, mailbox, mock_module):
    """``n:*`` 且 n 大于最大 UID 时，服务器仍需返回最后一封（RFC 3501 6.4.8）。"""
    for uid in (1, 2, 3):
        mailbox.messages.append(mock_module.MockMessage(uid=uid, raw=make_email(subject=f"m{uid}")))
    conn = imaplib.IMAP4("127.0.0.1", mock_server.port, timeout=5)
    try:
        conn.login("test@qq.com", "mock-auth-code")
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("SEARCH", "UID", "9:*")
        assert typ == "OK"
        assert data[0] == b"3"  # 客户端/采集器必须再按 next_uid 过滤
    finally:
        conn.logout()
