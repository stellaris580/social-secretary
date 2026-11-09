"""测试用 fake collector worker：实现 ``core.collector_worker`` 的 JSON IPC 协议。

只依赖标准库，可直接被 ``run_worker_task`` 当子进程启动；通过环境变量控制行为，
用于覆盖成功/业务错误/崩溃/超时/坏回传等降级路径（不需要真实 QQ/UIA）。

环境变量：
- ``FAKE_WORKER_BEHAVIOR``：``ok``（默认）/ ``crash`` / ``sleep`` / ``garbage`` / ``error``
- ``FAKE_WORKER_LOG_DIR``：把收到的任务 JSON 追加到 ``<dir>/tasks.jsonl``（断言接线用）
- ``FAKE_WORKER_TEXT``：fetch_messages 返回的消息正文
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def _log_task(task: dict) -> None:
    log_dir = os.environ.get("FAKE_WORKER_LOG_DIR")
    if not log_dir:
        return
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # 只记录调度元数据，不记录可能含正文的字段（模拟真实 worker 的隐私约束）
    safe = {
        key: task.get(key)
        for key in ("version", "mode", "allowlist", "limit", "foreground_ack", "since")
    }
    conversation = task.get("conversation")
    if isinstance(conversation, dict):
        safe["conversation_id"] = conversation.get("id")
        safe["conversation_title"] = conversation.get("title")
    if isinstance(task.get("account"), dict):
        safe["account_id"] = task["account"].get("id")
    with (directory / "tasks.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def _conversation(index: int, title: str) -> dict:
    return {
        "id": f"qq:fake:{index}",
        "account_id": "",
        "kind": "private",
        "title": title,
        "external_key": title,
        "enabled": True,
        "priority": "normal",
        "last_scanned_at": None,
        "last_message_at": None,
        "note": "",
    }


def _message(task: dict) -> dict:
    conversation = task.get("conversation") or {}
    return {
        "conversation_key": conversation.get("external_key") or conversation.get("title") or "",
        "sender": "张三",
        "is_self": False,
        "text": os.environ.get("FAKE_WORKER_TEXT", "请明天上午10点前提交周报"),
        "timestamp": datetime.now().astimezone().isoformat(),
        "timestamp_source": "ui",
        "raw_time_text": "今天 09:00",
        "external_id": "fake-ext-1",
        "source_locator": "fake-row-1",
        "extras": {"fake": True},
    }


def main() -> int:
    buffer = getattr(sys.stdin, "buffer", None)
    raw = buffer.read().decode("utf-8", "replace") if buffer is not None else sys.stdin.read()
    task = json.loads(raw)
    output_path = Path(task["output_path"])
    mode = str(task.get("mode") or "")
    _log_task(task)
    behavior = os.environ.get("FAKE_WORKER_BEHAVIOR", "ok")

    if behavior == "crash":
        return 3
    if behavior == "sleep":
        time.sleep(10)
        return 0
    if behavior == "garbage":
        output_path.write_text("{ this is not json", encoding="utf-8")
        return 0
    if behavior == "error":
        payload = {
            "ok": False,
            "mode": mode,
            "error": {
                "code": "fake_worker_error",
                "message": "fake worker 返回业务错误",
                "detail": {"retryable": False},
            },
        }
    elif mode == "detect_current_chat":
        if os.environ.get("FAKE_WORKER_DETECT_FAIL") == "1":
            payload = {
                "ok": False,
                "mode": mode,
                "error": {
                    "code": "no_current_chat",
                    "message": "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天",
                },
            }
        else:
            title = os.environ.get(
                "FAKE_WORKER_DETECT_TITLE", "社交秘书一键读取测试会话"
            )
            payload = {
                "ok": True,
                "mode": mode,
                "title": title,
                "title_len": len(title),
                "window_mode": "classic",
            }
    elif mode == "list_visible_sessions":
        if os.environ.get("FAKE_WORKER_SESSIONS_FAIL") == "1":
            payload = {
                "ok": False,
                "mode": mode,
                "error": {
                    "code": "no_visible_sessions",
                    "message": "没有找到可见的 QQ 会话列表，请先打开 QQ 主面板",
                },
            }
        else:
            raw = os.environ.get("FAKE_WORKER_SESSIONS")
            if raw:
                try:
                    sessions = json.loads(raw)
                except json.JSONDecodeError:
                    sessions = []
            else:
                sessions = [
                    {"title": "测试会话甲", "unread": 2},
                    {"title": "测试会话乙", "unread": None},
                ]
            payload = {"ok": True, "mode": mode, "sessions": sessions}
    elif mode == "test_connection":
        payload = {
            "ok": True,
            "mode": mode,
            "connection": {
                "ok": True,
                "detail": "fake worker 连接成功（未读取任何消息）",
                "latency_ms": 5,
                "discovered": {"fake": True, "need_foreground": True},
            },
        }
    elif mode == "list_conversations":
        allowlist = [str(item) for item in (task.get("allowlist") or [])]
        payload = {
            "ok": True,
            "mode": mode,
            "conversations": [
                _conversation(index, title) for index, title in enumerate(allowlist)
            ],
        }
    elif mode == "fetch_messages":
        payload = {"ok": True, "mode": mode, "messages": [_message(task)], "warnings": []}
    else:
        payload = {"ok": True, "mode": mode}

    output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
