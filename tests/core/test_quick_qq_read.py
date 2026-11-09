"""P0-24 一键读取当前 QQ 聊天（服务层 + worker 协议）测试。

真实 QQ/UIA 不参与：通过 `tests/core/fake_collector_worker.py` 子进程实现
`detect_current_chat`/`list_conversations`/`fetch_messages` 的 fake 数据，验证：
- 未确认时拒绝、不启动 worker；
- detect 失败/超时/崩溃的结构化错误；
- 成功路径完成设置/授权/账号/白名单/刷新/扫描并返回统计；
- 已有 QQ 账号时复用并合并白名单；
- 标题不进入返回值/日志/审计；worker 常量与标题扫描 helper 的纯逻辑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from social_secretary.contracts import AccountKind, ConsentScope, ScanStatus
from social_secretary.core.collector_worker import (
    DEFAULT_TIMEOUTS,
    VALID_MODES,
    _detect_window_mode,
    _find_visible_chat_title,
)
from social_secretary.core.service import SocialSecretaryService
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import make_service

FAKE_WORKER = Path(__file__).with_name("fake_collector_worker.py")
FAKE_COMMAND = [sys.executable, str(FAKE_WORKER)]
TITLE = "一键读取测试会话"


def read_tasks(log_dir: Path) -> list[dict]:
    path = log_dir / "tasks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def make_worker_service(
    tmp_path: Path,
    log_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
    runner=None,
) -> SocialSecretaryService:
    env = {"FAKE_WORKER_LOG_DIR": str(log_dir), "FAKE_WORKER_DETECT_TITLE": TITLE}
    if extra_env:
        env.update(extra_env)
    kwargs = dict(
        credential_store=InMemoryCredentialStore(),
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env=env,
        qq_worker_output_dir=tmp_path / "ipc",
    )
    if timeout is not None:
        kwargs["qq_worker_timeout"] = timeout
    if runner is not None:
        kwargs["qq_worker_runner"] = runner
    return make_service(**kwargs)


# ---------------------------------------------------------------------------
# worker 协议常量 / 标题扫描纯逻辑
# ---------------------------------------------------------------------------

def test_detect_mode_is_valid_and_has_generous_timeout() -> None:
    assert "detect_current_chat" in VALID_MODES
    assert DEFAULT_TIMEOUTS["detect_current_chat"] >= 100


class _Node:
    def __init__(self, name: str, class_name: str, children=()) -> None:
        self.name = name
        self.class_name = class_name
        self.control_type = "Text"
        self.children = list(children)
        self.rect = None
        self.is_offscreen = False
        self.is_enabled = True

    def get_value(self) -> str | None:
        return None

    def set_value(self, value: str) -> bool:  # pragma: no cover - 只读测试
        return False

    def set_focus(self) -> bool:  # pragma: no cover
        return False


class _Window:
    def __init__(self, hwnd: int = 1) -> None:
        self.hwnd = hwnd
        self.width = 800
        self.height = 600
        self.area = 480_000
        self.title = "QQ"
        self.minimized = False


class _Backend:
    def __init__(self, root) -> None:
        self._root = root

    def root_from_handle(self, hwnd: int):
        return self._root


def _load_profile():
    from social_secretary.collectors.qq.profile import load_profile

    return load_profile()


def test_find_visible_chat_title_classic_and_efficiency() -> None:
    profile = _load_profile()
    header = _Node("目标测试会话", "chat-header__contact-name")
    toggle = _Node("经典", "narrow-toggler")  # 名称含“经典” → 效率模式
    root = _Node("QQ", "QQ", [header, toggle])
    found = _find_visible_chat_title(_Backend(root), profile, [_Window()])
    assert found is not None
    assert found["title"] == "目标测试会话"
    assert found["window_mode"] == "efficiency"
    assert _detect_window_mode(root, profile) == "efficiency"

    classic_root = _Node("QQ", "QQ", [header, _Node("效率", "narrow-toggler")])
    found_classic = _find_visible_chat_title(_Backend(classic_root), profile, [_Window()])
    assert found_classic["window_mode"] == "classic"


def test_find_visible_chat_title_none_when_no_header() -> None:
    profile = _load_profile()
    root = _Node("QQ", "QQ", [_Node("消息区", "chat-msg-area")])
    assert _find_visible_chat_title(_Backend(root), profile, [_Window()]) is None
    assert _find_visible_chat_title(_Backend(None), profile, [_Window()]) is None


# ---------------------------------------------------------------------------
# 未确认 / detect 失败
# ---------------------------------------------------------------------------

def test_quick_read_requires_user_confirmation(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_worker_service(tmp_path, log_dir)
    try:
        result = service.quick_qq_read(user_confirmed=False)
        assert result["ok"] is False
        assert result["error"] == "需要用户确认只读授权"
        assert result["detected_title_len"] == 0
        assert read_tasks(log_dir) == []
        # 未确认不开启实验开关、不写同意
        assert service.get_settings().qq_read_experimental is False
        assert not service._consent.is_granted(ConsentScope.QQ_READ)
        assert service.list_accounts() == []
    finally:
        service.shutdown()


def test_quick_read_detect_failure_is_structured_and_no_state_change(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_worker_service(
        tmp_path, log_dir, extra_env={"FAKE_WORKER_DETECT_FAIL": "1"}
    )
    try:
        result = service.quick_qq_read(user_confirmed=True)
        assert result["ok"] is False
        assert result["error"] == "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天"
        assert result["detected_title_len"] == 0
        assert service.get_settings().qq_read_experimental is False
        assert service.list_accounts() == []
        assert [task["mode"] for task in read_tasks(log_dir)] == ["detect_current_chat"]
    finally:
        service.shutdown()


def test_quick_read_detect_worker_crash_is_structured(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_worker_service(
        tmp_path, log_dir, extra_env={"FAKE_WORKER_BEHAVIOR": "crash"}
    )
    try:
        result = service.quick_qq_read(user_confirmed=True)
        assert result["ok"] is False
        assert "子进程" in (result["error"] or "")
        assert service.list_accounts() == []
    finally:
        service.shutdown()


def test_quick_read_detect_timeout_is_structured(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_worker_service(
        tmp_path,
        log_dir,
        extra_env={"FAKE_WORKER_BEHAVIOR": "sleep"},
        timeout=0.5,
    )
    try:
        result = service.quick_qq_read(user_confirmed=True)
        assert result["ok"] is False
        assert "超时" in (result["error"] or "")
        assert service.list_accounts() == []
    finally:
        service.shutdown()


def test_quick_read_injected_runner_detect_error(tmp_path) -> None:
    def runner(task):
        assert task["mode"] == "detect_current_chat"
        return {
            "ok": False,
            "mode": "detect_current_chat",
            "error": {
                "code": "no_current_chat",
                "message": "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天",
            },
        }

    service = make_service(
        credential_store=InMemoryCredentialStore(), qq_worker_runner=runner
    )
    try:
        result = service.quick_qq_read(user_confirmed=True)
        assert result["ok"] is False
        assert result["error"] == "没有找到当前 QQ 聊天窗口，请先在 QQ 打开一个聊天"
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------

def test_quick_read_success_creates_account_and_scans(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_worker_service(tmp_path, log_dir)
    try:
        result = service.quick_qq_read(user_confirmed=True)

        assert result["ok"] is True, result
        assert result["added_account"] is True
        assert result["detected_title_len"] == len(TITLE)
        assert result["refresh_ok"] is True
        assert result["scan_status"] == "ok"
        assert result["messages_seen"] >= 1
        assert result["messages_new"] >= 1
        assert result["conversations_scanned"] >= 1
        assert result["error"] is None
        # 标题原文不出现在返回值里
        assert TITLE not in json.dumps(result, ensure_ascii=False)

        account = service.list_accounts()[0]
        assert account.kind is AccountKind.QQ
        assert result["account_id"] == account.id
        assert TITLE in service._get_allowlist(account.id)
        assert service.get_settings().qq_read_experimental is True
        assert service.get_settings().collection_paused is False
        assert service._consent.is_granted(ConsentScope.QQ_READ)

        conversations = service.list_conversations(account.id)
        target = next(c for c in conversations if c.external_key == TITLE)
        assert target.enabled is True
        messages = service.list_messages()
        assert messages and messages[0].conversation_id == target.id

        # 标题只用于白名单/会话，不进入审计
        audit_text = str(service.storage.list_audit())
        assert TITLE not in audit_text
        assert "quick_qq_read" in audit_text
        # worker 调用顺序：detect → list_conversations → fetch_messages
        modes = [task["mode"] for task in read_tasks(log_dir)]
        assert modes[0] == "detect_current_chat"
        assert "list_conversations" in modes
        assert "fetch_messages" in modes
    finally:
        service.shutdown()


def test_quick_read_reuses_existing_qq_account_and_merges_allowlist(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_worker_service(tmp_path, log_dir)
    try:
        existing = service.add_qq_account("已有 QQ", ["旧测试会话"])
        result = service.quick_qq_read(user_confirmed=True)

        assert result["ok"] is True, result
        assert result["added_account"] is False
        assert result["account_id"] == existing.id
        accounts = [a for a in service.list_accounts() if a.kind is AccountKind.QQ]
        assert len(accounts) == 1
        allowlist = service._get_allowlist(existing.id)
        assert "旧测试会话" in allowlist
        assert TITLE in allowlist
        target = service.storage.find_conversation_by_external_key(existing.id, TITLE)
        assert target is not None and target.enabled is True
        assert service.list_messages()
    finally:
        service.shutdown()


def test_quick_read_response_has_no_raw_title_on_scan_failure() -> None:
    """detect 成功但后续 worker 调用失败时，返回值只含长度，不含标题。"""
    title = "不应泄漏的标题"

    def runner(task):
        if task["mode"] == "detect_current_chat":
            return {
                "ok": True,
                "mode": "detect_current_chat",
                "title": title,
                "title_len": len(title),
                "window_mode": "classic",
            }
        raise RuntimeError("worker 不可用")

    service = make_service(
        credential_store=InMemoryCredentialStore(), qq_worker_runner=runner
    )
    try:
        result = service.quick_qq_read(user_confirmed=True)
        # 结构化返回，不抛裸异常；标题原文不外泄
        assert result["ok"] is False
        assert result["detected_title_len"] == len(title)
        assert title not in json.dumps(result, ensure_ascii=False)
        assert title not in str(service.storage.list_audit())
    finally:
        service.shutdown()
