"""QQ UIA 采集子进程 worker：把 comtypes/uiautomation 的 COM 初始化隔离到独立进程。

背景
----
packaging 阶段发现：GUI 进程内先 import GUI 框架、再 import comtypes/uiautomation 时可能
0xC0000005 崩溃；QQ NT 的 UIA 读取依赖 COM/UIAutomation。因此 **QQ 的 UIA 读取一律在独立
子进程中完成**，主进程（UI/服务层）只通过 JSON 协议接收结构化消息。

协议（版本 1）
--------------
父进程 → 子进程（stdin，UTF-8 JSON）::

    {"version": 1,
     "mode": "ping | test_connection | list_conversations | fetch_messages",
     "output_path": "<子进程写结果的 JSON 文件绝对路径>",
     "account": {...}, "allowlist": ["会话标题", ...],
     # fetch_messages 额外字段：
     "conversation": {...}, "since": "ISO8601|null", "limit": 200, "foreground_ack": false}

子进程 → 父进程：把结果**原子写**入 ``output_path``；任务正常处理（包括业务失败）时退出码 0::

    {"ok": true,  "mode": "...", "connection": {...} | "conversations": [...] | "messages": [...]}
    {"ok": false, "mode": "...", "error": {"code": "...", "message": "...", "detail": {...}}}

父进程把「非 0 退出码 / 超时 / 结果文件缺失 / 非法 JSON / ok=false」统一转换为
:class:`CollectorWorkerError`（子类含超时/协议错误），由流水线降级成 PARTIAL/FAILED，
**绝不让 UI 崩溃**。

隐私
----
- IPC 结果临时文件放在本机数据目录 ``<data_dir>/ipc/``，父进程读完立即删除；
- 消息正文只用于回传落库，**不写日志**；worker 的 stdout/stderr 被父进程捕获后丢弃，
  错误只保留错误码/类型/脱敏截断后的简述；
- 子进程不接收任何凭据；QQ 读取只按父进程给定的白名单标题执行。

冻结（PyInstaller）模式
-----------------------
父进程用 ``sys.executable --collector-worker`` 启动；app.py 负责把该参数转发到
:func:`main`（本模块会同时接受以 ``--collector-worker`` 开头的 argv）。源码模式用
``python -m social_secretary.core.collector_worker``。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts import (
    Account,
    AccountKind,
    ConnectionCheck,
    Conversation,
    ConversationKind,
    Priority,
    RawMessage,
    TimestampSource,
)
from ..security.audit import sanitize_detail
from ..security.log_redaction import redact_text

PROTOCOL_VERSION = 1
WORKER_MODULE = "social_secretary.core.collector_worker"
FROZEN_WORKER_FLAG = "--collector-worker"

#: 各类调用的默认超时（秒）；QQ 读取需要滚动历史，给 fetch 更长预算。
DEFAULT_TIMEOUTS: dict[str, float] = {
    "test_connection": 90.0,
    "list_conversations": 60.0,
    "fetch_messages": 300.0,
    "detect_current_chat": 120.0,
    "list_visible_sessions": 60.0,
}

VALID_MODES = frozenset(
    {
        "ping",
        "test_connection",
        "list_conversations",
        "fetch_messages",
        "detect_current_chat",
        "list_visible_sessions",
    }
)


# ---------------------------------------------------------------------------
# 异常（父进程侧降级用）
# ---------------------------------------------------------------------------

class CollectorWorkerError(RuntimeError):
    """QQ 采集子进程不可用 / 返回业务失败。父进程据此降级为 FAILED/PARTIAL。"""


class CollectorWorkerTimeout(CollectorWorkerError, TimeoutError):
    """子进程超时（进程已被 kill）。"""


class CollectorWorkerProtocolError(CollectorWorkerError):
    """子进程返回结果文件缺失 / 非法 JSON / 字段类型错误。"""


# ---------------------------------------------------------------------------
# 序列化 helpers（父进程与 worker 共用；只依赖标准库 + contracts）
# ---------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo is not None else value.astimezone()).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def encode_account(account: Account) -> dict[str, Any]:
    return {
        "id": account.id,
        "kind": account.kind.value,
        "display_name": account.display_name,
        "enabled": bool(account.enabled),
        "created_at": _iso(account.created_at),
        "identifier": account.identifier or "",
    }


def decode_account(data: Mapping[str, Any] | None) -> Account:
    if not isinstance(data, Mapping) or not data.get("id"):
        raise CollectorWorkerProtocolError("worker 任务缺少合法 account.id")
    return Account(
        id=str(data["id"]),
        kind=AccountKind(str(data.get("kind") or AccountKind.QQ.value)),
        display_name=str(data.get("display_name") or "QQ"),
        enabled=bool(data.get("enabled", True)),
        created_at=_parse_dt(data.get("created_at")),
        identifier=str(data.get("identifier") or ""),
    )


def encode_conversation(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "account_id": conversation.account_id,
        "kind": conversation.kind.value,
        "title": conversation.title,
        "external_key": conversation.external_key,
        "enabled": bool(conversation.enabled),
        "priority": conversation.priority.value,
        "last_scanned_at": _iso(conversation.last_scanned_at),
        "last_message_at": _iso(conversation.last_message_at),
        "note": conversation.note or "",
    }


def decode_conversation(data: Mapping[str, Any] | None) -> Conversation:
    if not isinstance(data, Mapping) or not data.get("id"):
        raise CollectorWorkerProtocolError("worker 结果缺少合法 conversation.id")
    return Conversation(
        id=str(data["id"]),
        account_id=str(data.get("account_id") or ""),
        kind=ConversationKind(str(data.get("kind") or ConversationKind.PRIVATE.value)),
        title=str(data.get("title") or ""),
        external_key=str(data.get("external_key") or data.get("title") or ""),
        enabled=bool(data.get("enabled", True)),
        priority=Priority(str(data.get("priority") or Priority.NORMAL.value)),
        last_scanned_at=_parse_dt(data.get("last_scanned_at")),
        last_message_at=_parse_dt(data.get("last_message_at")),
        note=str(data.get("note") or ""),
    )


def encode_raw_message(raw: RawMessage) -> dict[str, Any]:
    return {
        "conversation_key": raw.conversation_key,
        "sender": raw.sender,
        "is_self": bool(raw.is_self),
        "text": raw.text,
        "timestamp": _iso(raw.timestamp),
        "timestamp_source": raw.timestamp_source.value,
        "raw_time_text": raw.raw_time_text,
        "external_id": raw.external_id,
        "source_locator": raw.source_locator,
        "extras": dict(raw.extras or {}),
    }


def decode_raw_message(data: Mapping[str, Any]) -> RawMessage:
    if not isinstance(data, Mapping):
        raise CollectorWorkerProtocolError("worker 结果 messages 项不是对象")
    try:
        timestamp_source = TimestampSource(
            str(data.get("timestamp_source") or TimestampSource.COLLECTED.value)
        )
    except ValueError:
        timestamp_source = TimestampSource.COLLECTED
    extras = data.get("extras")
    return RawMessage(
        conversation_key=str(data.get("conversation_key") or ""),
        sender=str(data.get("sender") or ""),
        is_self=bool(data.get("is_self", False)),
        text=str(data.get("text") or ""),
        timestamp=_parse_dt(data.get("timestamp")),
        timestamp_source=timestamp_source,
        raw_time_text=data.get("raw_time_text"),
        external_id=data.get("external_id"),
        source_locator=data.get("source_locator"),
        extras=dict(extras) if isinstance(extras, Mapping) else {},
    )


def encode_connection_check(check: ConnectionCheck) -> dict[str, Any]:
    return {
        "ok": bool(check.ok),
        "detail": redact_text(check.detail, max_len=300),
        "latency_ms": check.latency_ms,
        "discovered": sanitize_detail(check.discovered),
    }


def decode_connection_check(data: Mapping[str, Any] | None) -> ConnectionCheck:
    payload = data if isinstance(data, Mapping) else {}
    latency = payload.get("latency_ms")
    discovered = payload.get("discovered")
    return ConnectionCheck(
        ok=bool(payload.get("ok", False)),
        detail=str(payload.get("detail") or ""),
        latency_ms=int(latency) if isinstance(latency, int) else None,
        discovered=dict(discovered) if isinstance(discovered, Mapping) else {},
    )


# ---------------------------------------------------------------------------
# 父进程：启动 worker / 读回结果
# ---------------------------------------------------------------------------

def default_worker_command() -> list[str]:
    """源码模式 ``python -m ...``；PyInstaller 冻结模式 ``exe --collector-worker``。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, FROZEN_WORKER_FLAG]
    return [sys.executable, "-m", WORKER_MODULE]


def _project_src_path() -> str:
    # core/collector_worker.py -> core -> social_secretary -> src
    return str(Path(__file__).resolve().parents[2])


def worker_child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """子进程环境：确保源码模式下 ``src`` 在 PYTHONPATH、stdin/stdout 用 UTF-8。

    Windows 管道默认按本地代码页（如 GBK）解码；父进程以 UTF-8 写 JSON，必须显式让子进程
    也按 UTF-8 处理，否则中文会话名/消息正文会被解码成乱码。
    """
    env = {str(key): str(value) for key, value in os.environ.items()}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if not getattr(sys, "frozen", False):
        src = _project_src_path()
        existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
        ordered: list[str] = []
        for part in [src, *existing]:
            if part not in ordered:
                ordered.append(part)
        env["PYTHONPATH"] = os.pathsep.join(ordered)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def run_worker_task(
    task: Mapping[str, Any],
    *,
    command: Sequence[str] | None = None,
    timeout: float = DEFAULT_TIMEOUTS["fetch_messages"],
    output_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """启动一次 worker 子进程并返回其结构化结果。

    子进程崩溃/超时/回传非法时抛 :class:`CollectorWorkerError` 子类；调用方（流水线）
    负责降级为 PARTIAL/FAILED。
    """
    directory = Path(output_dir) if output_dir is not None else Path(tempfile.mkdtemp(prefix="qq-worker-"))
    directory.mkdir(parents=True, exist_ok=True)
    handle, output_name = tempfile.mkstemp(prefix="qq-worker-", suffix=".json", dir=str(directory))
    os.close(handle)
    output_path = Path(output_name)
    try:
        output_path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 极端文件系统
        pass

    payload = dict(task)
    payload["version"] = PROTOCOL_VERSION
    payload["output_path"] = str(output_path)
    child_command = list(command) if command else default_worker_command()

    process: subprocess.Popen[str] | None = None
    try:
        try:
            process = subprocess.Popen(
                child_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=worker_child_env(env),
                cwd=str(cwd) if cwd is not None else None,
            )
        except OSError as exc:
            raise CollectorWorkerError(
                f"无法启动 QQ 采集子进程（{type(exc).__name__}）"
            ) from None
        try:
            # stdout/stderr 只接收不记录：避免把任何私有内容带进日志
            process.communicate(
                json.dumps(payload, ensure_ascii=False, default=str), timeout=timeout
            )
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - 已被 kill，忽略回收异常
                pass
            raise CollectorWorkerTimeout(
                f"QQ 采集子进程超时（{timeout:.1f}s），已终止"
            ) from None

        if process.returncode != 0:
            raise CollectorWorkerError(
                f"QQ 采集子进程异常退出（exit={process.returncode}）"
            )
        try:
            raw = output_path.read_text(encoding="utf-8")
        except OSError:
            raise CollectorWorkerProtocolError("QQ 采集子进程未回传结果文件") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise CollectorWorkerProtocolError("QQ 采集子进程回传结果不是合法 JSON") from None
        if not isinstance(data, dict):
            raise CollectorWorkerProtocolError("QQ 采集子进程回传结果不是 JSON 对象")
        if data.get("ok") is not True:
            error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
            code = str(error.get("code") or "worker_error")[:80]
            message = redact_text(str(error.get("message") or "QQ 采集子进程返回失败"), max_len=200)
            raise CollectorWorkerError(f"{message}（{code}）")
        return data
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不影响主流程
            pass
        if process is not None and process.poll() is None:  # pragma: no cover - 防御性
            process.kill()


class WorkerQQMessageSource:
    """父进程侧 ``contracts.MessageSource``：每次读取都委托 worker 子进程。

    构造本身不 import collectors.qq / uiautomation，因此 UI 进程不会被 COM 库污染。
    """

    kind = AccountKind.QQ

    def __init__(
        self,
        *,
        account: Account,
        allowlist: Sequence[str] = (),
        command: Sequence[str] | None = None,
        timeout: float | None = None,
        output_dir: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        runner: Any | None = None,
    ) -> None:
        self._account = account
        self._allowlist = tuple(str(item).strip() for item in allowlist if str(item).strip())
        self._command = list(command) if command else None
        self._output_dir = Path(output_dir) if output_dir is not None else None
        self._env = dict(env) if env else None
        #: 测试注入：``runner(task_json_dict) -> worker_result_dict``
        self._runner = runner
        self._timeouts = dict(DEFAULT_TIMEOUTS)
        if timeout is not None and timeout > 0:
            self._timeouts = {mode: float(timeout) for mode in DEFAULT_TIMEOUTS}

    # -- 只读属性（诊断/测试用）------------------------------------------
    @property
    def worker_command(self) -> list[str]:
        return list(self._command) if self._command else default_worker_command()

    @property
    def allowlist(self) -> tuple[str, ...]:
        return self._allowlist

    # -- contracts.MessageSource ----------------------------------------
    def test_connection(self) -> ConnectionCheck:
        try:
            data = self._call("test_connection")
        except CollectorWorkerError as exc:
            return ConnectionCheck(ok=False, detail=f"QQ 采集子进程不可用：{exc}")
        return decode_connection_check(data.get("connection"))

    def list_conversations(self) -> list[Conversation]:
        data = self._call("list_conversations")
        items = data.get("conversations")
        if not isinstance(items, list):
            raise CollectorWorkerProtocolError("worker 结果缺少 conversations 数组")
        return [decode_conversation(item) for item in items]

    def fetch_messages(
        self,
        conversation: Conversation,
        *,
        since: datetime | None = None,
        limit: int = 100,
        foreground_ack: bool = False,
    ) -> list[RawMessage]:
        data = self._call(
            "fetch_messages",
            extra={
                "conversation": encode_conversation(conversation),
                "since": _iso(since),
                "limit": max(1, int(limit)),
                "foreground_ack": bool(foreground_ack),
            },
        )
        items = data.get("messages")
        if not isinstance(items, list):
            raise CollectorWorkerProtocolError("worker 结果缺少 messages 数组")
        messages: list[RawMessage] = []
        for item in items:
            messages.append(decode_raw_message(item))
        return messages

    def close(self) -> None:
        # 每次调用都是独立子进程，无需常驻连接
        return None

    # -- 内部 -----------------------------------------------------------
    def _call(self, mode: str, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        task: dict[str, Any] = {
            "version": PROTOCOL_VERSION,
            "mode": mode,
            "account": encode_account(self._account),
            "allowlist": list(self._allowlist),
        }
        if extra:
            task.update(extra)
        try:
            data = self._run(task)
        except CollectorWorkerError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何注入 runner 异常都按子进程不可用降级
            raise CollectorWorkerError(
                f"QQ 采集子进程调用失败（{type(exc).__name__}）"
            ) from exc
        if not isinstance(data, dict):
            raise CollectorWorkerProtocolError("worker 结果不是 JSON 对象")
        return data

    def _run(self, task: Mapping[str, Any]) -> dict[str, Any]:
        if self._runner is not None:
            return self._runner(dict(task))
        return run_worker_task(
            task,
            command=self._command,
            timeout=self._timeouts.get(str(task.get("mode")), DEFAULT_TIMEOUTS["fetch_messages"]),
            output_dir=self._output_dir,
            env=self._env,
        )


# ---------------------------------------------------------------------------
# 子进程侧：一键读取当前聊天（detect_current_chat）
# ---------------------------------------------------------------------------

_QQ_VISIBLE_WINDOW_LIMIT = 40
_NO_CURRENT_CHAT_ERROR = {
    "code": "no_current_chat",
    "message": "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天",
}


def _visible_qq_windows(backend: Any, profile: Any) -> list[Any]:
    try:
        windows = list(backend.list_top_windows(list(profile.process_names)))
    except Exception:  # noqa: BLE001 - 窗口枚举失败按“没有当前聊天”处理
        return []
    visible = [
        window
        for window in windows
        if int(getattr(window, "width", 0)) > 0 and int(getattr(window, "height", 0)) > 0
    ]
    panel_re = profile.panel_title_regex
    visible.sort(
        key=lambda window: (
            bool(panel_re.match(str(getattr(window, "title", "") or ""))),
            not bool(getattr(window, "minimized", False)),
            int(getattr(window, "area", 0)),
        ),
        reverse=True,
    )
    limit = max(
        1,
        min(
            int(getattr(profile.windows, "max_scan_windows", _QQ_VISIBLE_WINDOW_LIMIT)),
            _QQ_VISIBLE_WINDOW_LIMIT,
        ),
    )
    return visible[:limit]


def _detect_window_mode(root: Any, profile: Any) -> str:
    """用 narrow-toggler 判断经典/效率模式；识别不了返回 unknown。"""
    from social_secretary.collectors.qq import nodes
    from social_secretary.collectors.qq.profile import compile_pattern

    try:
        toggle_re = compile_pattern(profile.mode_toggle.class_pattern)
        toggle = nodes.find_first(
            root,
            lambda node: bool(toggle_re.search(nodes.class_name(node))),
            max_nodes=8_000,
        )
        if toggle is not None:
            name = nodes.node_name(toggle)
            if profile.mode_toggle.efficiency_hint in name:
                return "efficiency"
            if profile.mode_toggle.classic_hint in name:
                return "classic"
    except Exception:  # noqa: BLE001 - 模式识别失败不影响标题读取
        pass
    return "unknown"


def _find_visible_chat_title(
    backend: Any, profile: Any, windows: Sequence[Any]
) -> dict[str, Any] | None:
    """在可见 QQ 窗口中用经典/效率两模式查找聊天头部控件，返回标题与模式。"""
    from social_secretary.collectors.qq import nodes
    from social_secretary.collectors.qq.profile import QqUiMode, compile_pattern

    for window in windows:
        try:
            root = backend.root_from_handle(int(getattr(window, "hwnd", 0)))
        except Exception:  # noqa: BLE001
            root = None
        if root is None:
            continue
        window_mode = _detect_window_mode(root, profile)
        for mode in (QqUiMode.CLASSIC, QqUiMode.EFFICIENCY):
            try:
                selectors = profile.mode_selectors(mode)
                header_re = compile_pattern(selectors.header_class_pattern)
                header = nodes.find_first(
                    root,
                    lambda node: bool(header_re.search(nodes.class_name(node))),
                    max_nodes=10_000,
                )
            except Exception:  # noqa: BLE001
                header = None
            if header is None:
                continue
            try:
                title = str(nodes.node_name(header) or "").strip()
            except Exception:  # noqa: BLE001
                title = ""
            if not title:
                continue
            return {
                "title": title,
                "window_mode": window_mode if window_mode != "unknown" else mode.value,
            }
    return None


def _run_detect_current_chat() -> dict[str, Any]:
    """让 QQ 前台并读取“当前打开的聊天”的头部标题（只读结构，不读消息正文）。"""
    from social_secretary.collectors.qq import probe
    from social_secretary.collectors.qq.foreground import ForegroundGuard, activate_window
    from social_secretary.collectors.qq.profile import load_profile

    profile = load_profile()
    backend = probe.make_backend()
    try:
        # 1) 复用只读探针：激活 QQ 前台并等待 UIA 树就绪（不点击/不滚动/不读正文）。
        try:
            probe.collect_probe(
                backend,
                profile,
                probe.ProbeOptions(activate=True, include_names=False),
            )
        except Exception:  # noqa: BLE001 - 探针失败仍尝试一次窗口枚举
            pass

        windows = _visible_qq_windows(backend, profile)
        found: dict[str, Any] | None = None
        guard = None
        try:
            # 2) collect_probe 结束后会恢复前台；为确保 UIA 树完整，这里再短暂置前读取标题，
            #    读取后由 ForegroundGuard 恢复用户原前台窗口。
            target = windows[0] if windows else None
            if target is not None:
                guard = ForegroundGuard(
                    backend, restore_attempts=profile.behavior.restore_attempts
                )
                guard.__enter__()
                try:
                    activate_window(
                        backend,
                        int(getattr(target, "hwnd", 0)),
                        attempts=profile.behavior.activate_attempts,
                        poll_interval=profile.behavior.poll_interval_ms / 1000.0,
                        guard=guard,
                    )
                except Exception:  # noqa: BLE001 - 置前失败仍尝试读取当前树
                    pass
                try:
                    backend.sleep(profile.behavior.revive_tree_wait_ms / 1000.0)
                except Exception:  # noqa: BLE001
                    pass
                windows = _visible_qq_windows(backend, profile)
            found = _find_visible_chat_title(backend, profile, windows)
        finally:
            if guard is not None:
                try:
                    guard.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 - 恢复前台失败不影响返回值
                    pass

        if found is None:
            return {
                "ok": False,
                "mode": "detect_current_chat",
                "error": dict(_NO_CURRENT_CHAT_ERROR),
            }
        title = str(found.get("title") or "").strip()
        if not title:
            return {
                "ok": False,
                "mode": "detect_current_chat",
                "error": dict(_NO_CURRENT_CHAT_ERROR),
            }
        return {
            "ok": True,
            "mode": "detect_current_chat",
            "title": title,
            "title_len": len(title),
            "window_mode": str(found.get("window_mode") or "unknown"),
        }
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass


_NO_VISIBLE_SESSIONS_ERROR = {
    "code": "no_visible_sessions",
    "message": "没有找到可见的 QQ 会话列表，请先打开 QQ 主面板",
}

#: 只返回“当前可见”的前若干个会话；不滚动，避免一次性暴露大量历史会话。
_MAX_VISIBLE_SESSIONS = 30


def _conversation_item_title(item: Any) -> str:
    """读取会话列表项标题：recent-contact-item > item__info > 第一个 Text。"""
    from social_secretary.collectors.qq import nodes

    try:
        for info, _depth in nodes.iter_descendants(item, max_nodes=80):
            if nodes.class_name(info) != "item__info":
                continue
            for text, _text_depth in nodes.iter_descendants(info, max_nodes=40):
                if nodes.control_type(text).lower() != "text":
                    continue
                title = str(nodes.node_name(text) or "").strip()
                if title:
                    return title
    except Exception:  # noqa: BLE001 - 结构异常按找不到标题处理
        return ""
    return ""


def _parse_unread(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not digits:
        return None
    try:
        return min(int(digits), 9_999)
    except ValueError:  # pragma: no cover - 理论上不可达
        return None


def _conversation_item_unread(item: Any) -> int | None:
    """读取会话项未读徽章（q-badge 类控件）；没有徽章/非数字返回 None。"""
    from social_secretary.collectors.qq import nodes

    try:
        for node, _depth in nodes.iter_descendants(item, max_nodes=120):
            if "q-badge" not in nodes.class_name(node).casefold():
                continue
            unread = _parse_unread(str(nodes.node_name(node) or ""))
            if unread:
                return unread
    except Exception:  # noqa: BLE001 - 徽章异常不影响标题列表
        pass
    return None


def _read_visible_sessions(root: Any, _profile: Any) -> list[dict[str, Any]]:
    """从 QQ 主面板 UIA 树读取当前可见会话项（只读标题/未读数，不读预览/正文）。"""
    from social_secretary.collectors.qq import nodes

    try:
        items = [
            node
            for node, _depth in nodes.iter_descendants(root, max_nodes=10_000)
            if nodes.class_name(node) == "recent-contact-item"
            and nodes.control_type(node).lower() == "group"
        ]
    except Exception:  # noqa: BLE001
        items = []
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        # 系统/服务会话（头像子树含 avatar-sys）通常没有可正常打开的聊天头部，
        # 放进选择器会让用户选中后读取失败；默认不展示，避免“选好几个只读到一半”。
        try:
            if any(
                "avatar-sys" in nodes.class_name(child)
                for child, _child_depth in nodes.iter_descendants(item, max_nodes=80)
            ):
                continue
        except Exception:  # noqa: BLE001
            pass
        title = _conversation_item_title(item)
        if not title or title in seen:
            continue
        seen.add(title)
        sessions.append({"title": title, "unread": _conversation_item_unread(item)})
        if len(sessions) >= _MAX_VISIBLE_SESSIONS:
            break
    return sessions


def _find_panel_window(windows: Sequence[Any], profile: Any) -> Any | None:
    panel_re = profile.panel_title_regex
    for window in windows:
        try:
            if panel_re.match(str(getattr(window, "title", "") or "")):
                return window
        except Exception:  # noqa: BLE001
            continue
    return None


def _run_list_visible_sessions() -> dict[str, Any]:
    """让 QQ 前台并列出“当前可见会话”的标题/未读数；不点击、不滚动、不读正文。"""
    from social_secretary.collectors.qq import probe
    from social_secretary.collectors.qq.foreground import ForegroundGuard, activate_window
    from social_secretary.collectors.qq.profile import load_profile

    profile = load_profile()
    backend = probe.make_backend()
    try:
        try:
            probe.collect_probe(
                backend,
                profile,
                probe.ProbeOptions(activate=True, include_names=False),
            )
        except Exception:  # noqa: BLE001 - 探针失败仍尝试一次窗口枚举
            pass

        windows = _visible_qq_windows(backend, profile)
        panel = _find_panel_window(windows, profile)
        if panel is None:
            return {
                "ok": False,
                "mode": "list_visible_sessions",
                "error": dict(_NO_VISIBLE_SESSIONS_ERROR),
            }

        sessions: list[dict[str, Any]] = []
        guard = None
        try:
            guard = ForegroundGuard(
                backend, restore_attempts=profile.behavior.restore_attempts
            )
            guard.__enter__()
            try:
                activate_window(
                    backend,
                    int(getattr(panel, "hwnd", 0)),
                    attempts=profile.behavior.activate_attempts,
                    poll_interval=profile.behavior.poll_interval_ms / 1000.0,
                    guard=guard,
                )
            except Exception:  # noqa: BLE001 - 置前失败仍尝试读取当前树
                pass
            try:
                backend.sleep(profile.behavior.revive_tree_wait_ms / 1000.0)
            except Exception:  # noqa: BLE001
                pass
            root = backend.root_from_handle(int(getattr(panel, "hwnd", 0)))
            if root is not None:
                sessions = _read_visible_sessions(root, profile)
        finally:
            if guard is not None:
                try:
                    guard.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 - 恢复前台失败不影响返回值
                    pass

        if not sessions:
            return {
                "ok": False,
                "mode": "list_visible_sessions",
                "error": dict(_NO_VISIBLE_SESSIONS_ERROR),
            }
        return {"ok": True, "mode": "list_visible_sessions", "sessions": sessions}
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 子进程侧：执行任务
# ---------------------------------------------------------------------------

def execute_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """在 worker 子进程内执行任务；返回符合协议的 dict（不负责写文件）。"""
    mode = str(task.get("mode") or "")
    if mode == "ping":
        return {"ok": True, "mode": mode, "pid": os.getpid()}
    if mode not in VALID_MODES:
        raise ValueError(f"未知 worker mode: {mode!r}")
    if mode == "detect_current_chat":
        # 一键读取当前聊天：只检测标题，不读消息正文；不记录标题到日志。
        return _run_detect_current_chat()
    if mode == "list_visible_sessions":
        # 会话选择器：只列出当前可见会话标题/未读数；不点击、不滚动、不读正文、不写日志。
        return _run_list_visible_sessions()

    # 延迟 import：worker 模块被（父进程）import 时绝不加载 uiautomation/comtypes
    from social_secretary.collectors.qq.collector import QqMessageSource

    account = decode_account(task.get("account"))
    allowlist = [
        str(item).strip()
        for item in (task.get("allowlist") or [])
        if str(item).strip()
    ]
    source = QqMessageSource(allowlist=allowlist, account_id=account.id)
    try:
        if mode == "test_connection":
            check = source.test_connection()
            return {
                "ok": True,
                "mode": mode,
                "connection": encode_connection_check(check),
            }
        if mode == "list_conversations":
            conversations = source.list_conversations() or []
            return {
                "ok": True,
                "mode": mode,
                "conversations": [encode_conversation(item) for item in conversations],
            }

        conversation = decode_conversation(task.get("conversation"))
        messages = source.fetch_messages(
            conversation,
            since=_parse_dt(task.get("since")),
            limit=max(1, int(task.get("limit") or 100)),
            foreground_ack=bool(task.get("foreground_ack")),
        )
        stats = source.last_stats
        return {
            "ok": True,
            "mode": mode,
            "messages": [encode_raw_message(item) for item in (messages or [])],
            "warnings": [
                redact_text(str(warning), max_len=200)
                for warning in (getattr(stats, "warnings", ()) or [])
            ],
            "foreground_used": bool(getattr(stats, "foreground_used", False)),
        }
    finally:
        try:
            source.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结果
            pass


def _error_payload(exc: BaseException) -> dict[str, Any]:
    """把异常转换为符合协议的脱敏错误（不含消息正文/凭据）。"""
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, Mapping):
            detail = data.get("detail")
            return {
                "code": redact_text(str(data.get("code") or "qq_error"), max_len=80),
                "message": redact_text(str(data.get("message") or ""), max_len=300),
                "detail": sanitize_detail(detail if isinstance(detail, Mapping) else {}),
            }
    return {
        "code": type(exc).__name__,
        "message": redact_text(str(exc), max_len=300) or type(exc).__name__,
        "detail": {},
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Sequence[str] | None = None) -> int:
    """worker 入口：stdin 读 JSON 任务，结果写 output_path。退出码 0 = 协议处理完成。"""
    args = list(sys.argv[1:] if argv is None else argv)
    # 冻结模式 app.py 可能把 --collector-worker 原样转进来；也接受直接带其它参数
    if args and args[0] == FROZEN_WORKER_FLAG:
        args = args[1:]
    try:
        # 显式按 UTF-8 从字节流读取：不依赖 Windows 管道/控制台的本地代码页
        buffer = getattr(sys.stdin, "buffer", None)
        raw = buffer.read().decode("utf-8", "replace") if buffer is not None else sys.stdin.read()
        task = json.loads(raw) if raw.strip() else {}
        if not isinstance(task, dict):
            raise ValueError("任务必须是 JSON 对象")
    except Exception:  # noqa: BLE001 - 无法解析任务时无法得知 output_path，只能非 0 退出
        return 2

    output_path_text = task.get("output_path")
    if not output_path_text:
        return 2
    output_path = Path(str(output_path_text))

    try:
        result = execute_task(task)
    except Exception as exc:  # noqa: BLE001 - 任何任务异常都结构化回传，父进程降级
        result = {
            "ok": False,
            "mode": str(task.get("mode") or ""),
            "error": _error_payload(exc),
        }
    try:
        _write_json_atomic(output_path, result)
    except OSError:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())


__all__ = [
    "CollectorWorkerError",
    "CollectorWorkerProtocolError",
    "CollectorWorkerTimeout",
    "DEFAULT_TIMEOUTS",
    "FROZEN_WORKER_FLAG",
    "PROTOCOL_VERSION",
    "WorkerQQMessageSource",
    "WORKER_MODULE",
    "decode_connection_check",
    "decode_conversation",
    "decode_raw_message",
    "default_worker_command",
    "encode_account",
    "encode_connection_check",
    "encode_conversation",
    "encode_raw_message",
    "execute_task",
    "main",
    "run_worker_task",
    "worker_child_env",
]
