"""P0-25 QQ 会话选择器（隐私优先）测试。

用户先确认“只读取会话列表”，程序列出当前可见会话标题；用户勾选后只把这些标题写入白名单，
不打开会话、不读正文/预览、不自动扫描。标题只在内存/UI 显示，不进入日志/审计。

所有 QQ 操作均使用 `tests/core/fake_collector_worker.py` 子进程，不接触真实 QQ/UIA。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from social_secretary.contracts import AccountKind, ConsentScope
from social_secretary.core.collector_worker import (
    DEFAULT_TIMEOUTS,
    VALID_MODES,
    _parse_unread,
    _read_visible_sessions,
)
from social_secretary.core.service import SocialSecretaryService
from social_secretary.security.credential_store import InMemoryCredentialStore
from tests.core.fakes import make_conversation, make_service

FAKE_WORKER = Path(__file__).with_name("fake_collector_worker.py")
FAKE_COMMAND = [sys.executable, str(FAKE_WORKER)]

DEFAULT_SESSIONS = [
    {"title": "测试会话甲", "unread": 2},
    {"title": "测试会话乙", "unread": None},
]


def read_tasks(log_dir: Path) -> list[dict]:
    path = log_dir / "tasks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def make_picker_service(
    tmp_path: Path,
    log_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
    runner=None,
) -> SocialSecretaryService:
    env = {"FAKE_WORKER_LOG_DIR": str(log_dir)}
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
# worker 协议 / 会话列表读取纯逻辑
# ---------------------------------------------------------------------------

def test_worker_mode_and_timeout_registered() -> None:
    assert "list_visible_sessions" in VALID_MODES
    assert DEFAULT_TIMEOUTS["list_visible_sessions"] >= 30


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


class _Group(_Node):
    def __init__(self, children=()) -> None:
        super().__init__("", "recent-contact-item", children)
        self.control_type = "Group"


class _ItemInfo(_Node):
    def __init__(self, children=()) -> None:
        super().__init__("", "item__info", children)


def _session_item(title: str, *, unread: str | None = None, preview: str = "预览内容") -> _Group:
    texts: list[_Node] = [_Node(title, "item__title")]
    children: list[_Node] = [_ItemInfo(texts), _Node(preview, "item__preview")]
    if unread is not None:
        children.append(_Node(unread, "q-badge__text"))
    return _Group(children)


def test_read_visible_sessions_only_titles_and_unread() -> None:
    profile = object()  # _read_visible_sessions 当前不需要 profile 字段
    root = _Node(
        "QQ",
        "Root",
        [
            _session_item("会话甲", unread="3"),
            _session_item("会话乙"),
            _session_item("会话甲", unread="9"),  # 重复标题去重
        ],
    )
    sessions = _read_visible_sessions(root, profile)
    assert sessions == [
        {"title": "会话甲", "unread": 3},
        {"title": "会话乙", "unread": None},
    ]
    # 预览文本 / 正文绝不被当作标题
    assert all("预览" not in item["title"] for item in sessions)


def test_parse_unread_handles_badges() -> None:
    assert _parse_unread("3") == 3
    assert _parse_unread("99+") == 99
    assert _parse_unread("") is None
    assert _parse_unread("abc") is None
    assert _parse_unread("99999") == 9_999


# ---------------------------------------------------------------------------
# list_qq_visible_sessions
# ---------------------------------------------------------------------------

def test_list_sessions_requires_confirmation_without_worker(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(tmp_path, log_dir)
    try:
        result = service.list_qq_visible_sessions(user_confirmed=False)
        assert result["ok"] is False
        assert result["error"] == "需要用户确认只读取会话列表"
        assert result["sessions"] == []
        assert read_tasks(log_dir) == []
        assert not service._consent.is_granted(ConsentScope.QQ_READ)
        assert service.get_settings().qq_read_experimental is False
    finally:
        service.shutdown()


def test_list_sessions_success_returns_titles_and_audits_count_only(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(tmp_path, log_dir)
    try:
        result = service.list_qq_visible_sessions(user_confirmed=True)
        assert result["ok"] is True, result
        assert result["sessions"] == DEFAULT_SESSIONS
        assert result["error"] is None
        assert [task["mode"] for task in read_tasks(log_dir)] == ["list_visible_sessions"]
        # 用户确认后授予 QQ_READ；但不开启实验开关、不读取任何会话
        assert service._consent.is_granted(ConsentScope.QQ_READ)
        assert service.get_settings().qq_read_experimental is False
        assert service.list_accounts() == []
        audit = str(service.storage.list_audit())
        assert "qq_sessions_listed" in audit
        for item in DEFAULT_SESSIONS:
            assert item["title"] not in audit
    finally:
        service.shutdown()


def test_list_sessions_sanitizes_worker_output(tmp_path) -> None:
    raw = json.dumps(
        [
            {"title": " 会话甲 ", "unread": 3},
            {"title": "会话甲", "unread": 8},
            {"title": "", "unread": 1},
            {"title": "会话乙", "unread": True},
            {"title": 123, "unread": 1},
        ],
        ensure_ascii=False,
    )
    log_dir = tmp_path / "logs"
    service = make_picker_service(
        tmp_path, log_dir, extra_env={"FAKE_WORKER_SESSIONS": raw}
    )
    try:
        result = service.list_qq_visible_sessions(user_confirmed=True)
        assert result["ok"] is True, result
        assert result["sessions"] == [
            {"title": "会话甲", "unread": 3},
            {"title": "会话乙", "unread": None},
        ]
        assert "会话甲" not in str(service.storage.list_audit())
    finally:
        service.shutdown()


def test_list_sessions_no_visible_window_is_structured(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(
        tmp_path, log_dir, extra_env={"FAKE_WORKER_SESSIONS_FAIL": "1"}
    )
    try:
        result = service.list_qq_visible_sessions(user_confirmed=True)
        assert result["ok"] is False
        assert result["error"] == "没有找到可见的 QQ 会话列表，请先打开 QQ 主面板"
        assert result["sessions"] == []
        assert "qq_sessions_listed" not in str(service.storage.list_audit())
    finally:
        service.shutdown()


def test_list_sessions_worker_crash_is_structured(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(
        tmp_path, log_dir, extra_env={"FAKE_WORKER_BEHAVIOR": "crash"}
    )
    try:
        result = service.list_qq_visible_sessions(user_confirmed=True)
        assert result["ok"] is False
        assert "子进程" in (result["error"] or "")
    finally:
        service.shutdown()


def test_list_sessions_worker_timeout_is_structured(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(
        tmp_path,
        log_dir,
        extra_env={"FAKE_WORKER_BEHAVIOR": "sleep"},
        timeout=0.5,
    )
    try:
        result = service.list_qq_visible_sessions(user_confirmed=True)
        assert result["ok"] is False
        assert "超时" in (result["error"] or "")
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# apply_qq_session_selection
# ---------------------------------------------------------------------------

def test_apply_selection_creates_account_and_only_enables_selected(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(tmp_path, log_dir)
    try:
        result = service.apply_qq_session_selection(
            ["会话甲", " 会话乙 ", "会话甲", "", 123]  # type: ignore[list-item]
        )
        assert result["ok"] is True, result
        assert result["selected_count"] == 2
        assert result["discovered_count"] == 2
        assert result["enabled_count"] == 2
        assert result["error"] is None

        account = service.list_accounts()[0]
        assert account.kind is AccountKind.QQ
        assert account.display_name == "我的 QQ"
        assert result["account_id"] == account.id
        assert service._get_allowlist(account.id) == ["会话甲", "会话乙"]
        conversations = service.list_conversations(account.id)
        assert {c.title for c in conversations} == {"会话甲", "会话乙"}
        assert all(c.enabled for c in conversations)
        assert service.get_settings().qq_read_experimental is True
        assert service.get_settings().collection_paused is False
        assert service._consent.is_granted(ConsentScope.QQ_READ)

        # 不自动扫描：worker 只被 refresh 调用 list_conversations，绝无 fetch_messages
        modes = [task["mode"] for task in read_tasks(log_dir)]
        assert "list_visible_sessions" not in modes  # apply 不再读取会话列表
        assert "fetch_messages" not in modes
        assert service.list_messages() == []
        # 审计只记数量，无标题
        audit = str(service.storage.list_audit())
        assert "qq_session_selection_applied" in audit
        assert "会话甲" not in json.dumps(result, ensure_ascii=False)
        assert "会话甲" not in audit
    finally:
        service.shutdown()


def test_apply_selection_replaces_allowlist_and_disables_unselected(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(tmp_path, log_dir)
    try:
        account = service.add_qq_account("已有 QQ", ["旧会话", "会话甲"])
        service.storage.upsert_conversation(
            make_conversation(
                "conv-old", account_id=account.id, title="旧会话", external_key="旧会话"
            )
        )
        service.storage.upsert_conversation(
            make_conversation(
                "conv-a", account_id=account.id, title="会话甲", external_key="会话甲"
            )
        )

        result = service.apply_qq_session_selection(["会话乙"])
        assert result["ok"] is True, result
        assert result["account_id"] == account.id
        assert result["selected_count"] == 1
        assert service._get_allowlist(account.id) == ["会话乙"]
        # 旧会话仍存在于本地，但被禁用；新选中的会话启用
        conversations = {
            c.external_key: c for c in service.list_conversations(account.id)
        }
        assert conversations["旧会话"].enabled is False
        assert conversations["会话甲"].enabled is False
        assert conversations["会话乙"].enabled is True
        assert result["discovered_count"] == 3
        assert result["enabled_count"] == 1
        # 只有第一个 QQ 账号被复用，未新建
        assert len([a for a in service.list_accounts() if a.kind is AccountKind.QQ]) == 1
    finally:
        service.shutdown()


def test_apply_selection_limits_twenty_titles(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(tmp_path, log_dir)
    try:
        titles = [f"会话{index:02d}" for index in range(25)]
        result = service.apply_qq_session_selection(titles)
        assert result["ok"] is True, result
        assert result["selected_count"] == 20
        account_id = result["account_id"]
        assert service._get_allowlist(account_id) == titles[:20]
        assert result["discovered_count"] == 20
    finally:
        service.shutdown()


def test_apply_empty_selection_is_rejected_without_side_effects(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    service = make_picker_service(tmp_path, log_dir)
    try:
        result = service.apply_qq_session_selection(["  ", "", 123])  # type: ignore[list-item]
        assert result["ok"] is False
        assert result["error"] == "没有选择要读取的 QQ 会话"
        assert result["selected_count"] == 0
        assert service.list_accounts() == []
        assert service.get_settings().qq_read_experimental is False
        assert read_tasks(log_dir) == []
    finally:
        service.shutdown()


def test_apply_selection_failure_is_structured(tmp_path) -> None:
    def runner(task):
        raise RuntimeError("worker 不可用")

    service = make_service(
        credential_store=InMemoryCredentialStore(), qq_worker_runner=runner
    )
    try:
        result = service.apply_qq_session_selection(["会话甲"])
        # refresh 失败不阻塞白名单保存，仍应结构化返回（ok=True 或可操作 error）
        assert isinstance(result, dict)
        assert set(result) == {
            "ok",
            "account_id",
            "selected_count",
            "discovered_count",
            "enabled_count",
            "error",
        }
        assert "会话甲" not in json.dumps(result, ensure_ascii=False)
    finally:
        service.shutdown()
