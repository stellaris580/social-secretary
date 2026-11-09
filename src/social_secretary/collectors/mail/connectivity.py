"""真实 IMAP 服务器 TLS / CAPABILITY 连通性探测（**不需要登录、不需要授权码**）。

用途
----
第一阶段可行性验证：证明本机网络与 Python OpenSSL 能完成到 QQ 邮箱真实服务器
``imap.qq.com:993`` 的 TLS 握手，并拿到服务器 CAPABILITY 能力集。
**不发送 LOGIN，不读取任何邮件**，因此证据里只有服务器响应、时间和耗时。

运行方式（用户/开发者均可）::

    .venv\\Scripts\\python.exe -m social_secretary.collectors.mail.connectivity ^
        --out-dir evidence\\mail

证据 JSON 字段见 docs/design/mail-collector.md；不含凭据、正文、邮箱地址。
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_HOST = "imap.qq.com"
DEFAULT_PORT = 993
DEFAULT_TIMEOUT = 15.0
CAPABILITY_COMMAND = b"A1 CAPABILITY\r\n"
_MAX_RESPONSE_BYTES = 64 * 1024


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _sanitize(text: str, *, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def probe_imap_connectivity(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """TLS 握手 + CAPABILITY（不登录），返回可直接落盘的脱敏结果 dict。"""
    started = time.perf_counter()
    result: dict[str, Any] = {
        "schema_version": 1,
        "probe": "imap_tls_capability",
        "ok": False,
        "target": {"host": host, "port": int(port)},
        "started_at": _now_iso(),
        "finished_at": None,
        "latency_ms": None,
        "login_attempted": False,
        "commands_sent": ["CAPABILITY"],
        "read_only": True,
        "tls": None,
        "server_greeting": None,
        "capability_response": None,
        "capabilities": [],
        "timings_ms": {},
        "errors": [],
    }
    timings: dict[str, int] = {}

    try:
        dns_started = time.perf_counter()
        addresses = sorted(
            {
                str(info[4][0])
                for info in socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
            }
        )
        timings["dns_ms"] = _elapsed_ms(dns_started)
        result["resolved_addresses"] = addresses

        connect_started = time.perf_counter()
        with socket.create_connection((host, int(port)), timeout=timeout) as raw_sock:
            context = ssl.create_default_context()
            with context.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                timings["tcp_tls_ms"] = _elapsed_ms(connect_started)
                tls_sock.settimeout(timeout)
                try:
                    peer_cert = tls_sock.getpeercert()
                except Exception:  # noqa: BLE001 - 证书字段只是附加信息
                    peer_cert = {}
                cipher = tls_sock.cipher()
                result["tls"] = {
                    "version": tls_sock.version(),
                    "cipher": cipher[0] if cipher else None,
                    "server_hostname": host,
                    "certificate": _cert_summary(peer_cert),
                }

                reader = tls_sock.makefile("rb")
                try:
                    greeting_started = time.perf_counter()
                    greeting = _read_line(reader)
                    timings["greeting_ms"] = _elapsed_ms(greeting_started)
                    result["server_greeting"] = greeting.decode("utf-8", "replace").strip()

                    capability_started = time.perf_counter()
                    tls_sock.sendall(CAPABILITY_COMMAND)
                    response_lines: list[str] = []
                    total = 0
                    while True:
                        line = _read_line(reader)
                        total += len(line)
                        if total > _MAX_RESPONSE_BYTES:
                            raise ValueError("CAPABILITY 响应超过长度上限")
                        response_lines.append(line.decode("utf-8", "replace").strip())
                        if line.startswith(b"A1 "):
                            break
                    timings["capability_ms"] = _elapsed_ms(capability_started)
                    result["capability_response"] = response_lines
                    result["capabilities"] = _parse_capabilities(response_lines)
                    if not any(line.startswith("A1 OK") for line in response_lines):
                        raise ValueError("CAPABILITY 未返回 OK")
                    result["ok"] = True
                finally:
                    try:
                        reader.close()
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as exc:  # noqa: BLE001 - 探针必须如实记录失败而不是崩溃
        result["errors"].append(
            {
                "type": type(exc).__name__,
                "message": _sanitize(str(exc)),
                "is_timeout": isinstance(exc, (TimeoutError, socket.timeout)),
                "is_tls_error": isinstance(exc, ssl.SSLError),
            }
        )
    finally:
        result["finished_at"] = _now_iso()
        timings["total_ms"] = _elapsed_ms(started)
        result["latency_ms"] = timings["total_ms"]
        result["timings_ms"] = timings
    return result


def _read_line(reader: Any, *, limit: int = 16 * 1024) -> bytes:
    line = reader.readline(limit + 1)
    if not line:
        raise ConnectionError("服务器在响应前关闭了连接")
    if len(line) > limit:
        raise ValueError("服务器响应行超长")
    return line


def _parse_capabilities(lines: list[str]) -> list[str]:
    for line in lines:
        upper = line.upper()
        if upper.startswith("* CAPABILITY "):
            return line.split()[2:]
        if upper.startswith("* OK [CAPABILITY "):
            start = upper.index("[CAPABILITY ") + len("[CAPABILITY ")
            end = upper.find("]", start)
            if end > start:
                return line[start:end].split()
    return []


def _cert_summary(peer_cert: dict[str, Any]) -> dict[str, Any]:
    """只保留证书的公开元数据（这是服务器在 TLS 握手时公开提供的）。"""
    if not peer_cert:
        return {}
    return {
        "subject": _flatten_name(peer_cert.get("subject", ())),
        "issuer": _flatten_name(peer_cert.get("issuer", ())),
        "not_before": peer_cert.get("notBefore"),
        "not_after": peer_cert.get("notAfter"),
    }


def _flatten_name(parts: Any) -> str:
    try:
        flat: list[str] = []
        for rdn in parts:
            for key, value in rdn:
                flat.append(f"{key}={value}")
        return ", ".join(flat)
    except Exception:  # noqa: BLE001
        return ""


def write_evidence(
    result: dict[str, Any],
    out_dir: str | Path | None = None,
    *,
    prefix: str = "imap_qq_com_connectivity",
) -> Path:
    """把探针结果写成 evidence/mail/*.json（默认仓库根目录下）。"""
    if out_dir is None:
        # 延迟导入，避免 collector 包对 config 的循环依赖。
        from social_secretary.config import project_root

        out_path = project_root() / "evidence" / "mail"
    else:
        out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = out_path / f"{prefix}_{timestamp}.json"
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="探测真实 IMAP 服务器 TLS/CAPABILITY（不登录、不读取邮件）"
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--out-dir",
        default=None,
        help="证据输出目录；默认 <仓库根>/evidence/mail",
    )
    parser.add_argument("--no-write", action="store_true", help="只打印不写证据文件")
    args = parser.parse_args(argv)

    result = probe_imap_connectivity(args.host, args.port, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.no_write:
        path = write_evidence(result, args.out_dir)
        print(f"\n[evidence] 已写入: {path}", file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())


__all__ = ["probe_imap_connectivity", "write_evidence", "main"]
