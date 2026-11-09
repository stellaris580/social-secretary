"""QQ UIA 采集子进程隔离测试。

覆盖：
- worker 模块 import 不加载 uiautomation/comtypes/PySide6（COM 隔离）；
- 真实子进程 ping / test_connection 协议；
- 父进程 WorkerQQMessageSource 与 fake worker 的编解码；
- 子进程崩溃 / 超时 / 坏 JSON / 缺结果文件 → 降级为 CollectorWorkerError（流水线 FAILED，不崩 UI）；
- 服务层 QQ scan 默认走 worker（mail 仍主进程，见 test_mail_wiring.py）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from social_secretary.contracts import (
    AccountKind,
    Category,
    ConsentScope,
    ScanStatus,
)
from social_secretary.core.collector_worker import (
    CollectorWorkerError,
    CollectorWorkerProtocolError,
    CollectorWorkerTimeout,
    WorkerQQMessageSource,
    default_worker_command,
    encode_account,
    run_worker_task,
    worker_child_env,
)
from tests.core.fakes import make_account, make_conversation, make_service

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FAKE_WORKER = Path(__file__).with_name("fake_collector_worker.py")
FAKE_COMMAND = [sys.executable, str(FAKE_WORKER)]


@pytest.fixture()
def worker_log(tmp_path) -> Path:
    directory = tmp_path / "worker-logs"
    directory.mkdir()
    return directory


def make_worker_source(
    tmp_path,
    *,
    log_dir: Path | None = None,
    behavior: str | None = None,
    timeout: float = 30.0,
    account=None,
) -> WorkerQQMessageSource:
    env: dict[str, str] = {}
    if log_dir is not None:
        env["FAKE_WORKER_LOG_DIR"] = str(log_dir)
    if behavior is not None:
        env["FAKE_WORKER_BEHAVIOR"] = behavior
    return WorkerQQMessageSource(
        account=account or make_account(),
        allowlist=["张三"],
        command=FAKE_COMMAND,
        output_dir=tmp_path / "ipc",
        env=env,
        timeout=timeout,
    )


def read_tasks(log_dir: Path) -> list[dict]:
    path = log_dir / "tasks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# 隔离 / 协议（真实子进程）
# ---------------------------------------------------------------------------

def test_default_worker_command_uses_module_in_source_mode() -> None:
    command = default_worker_command()
    assert command[0] == sys.executable
    assert any("social_secretary.core.collector_worker" in part for part in command)


def test_worker_module_import_does_not_load_ui_com_libraries() -> None:
    code = (
        "import sys\n"
        "import social_secretary.core.collector_worker  # noqa: F401\n"
        "print(int('uiautomation' in sys.modules))\n"
        "print(int('comtypes' in sys.modules))\n"
        "print(int('PySide6' in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=worker_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["0", "0", "0"]


def test_service_import_does_not_load_uia_or_gui_libraries() -> None:
    """UI 进程 import 核心服务时，不得把 uiautomation/comtypes/PySide6 拉进主进程。"""
    code = (
        "import sys\n"
        "import social_secretary.core.service  # noqa: F401\n"
        "print(int('uiautomation' in sys.modules))\n"
        "print(int('comtypes' in sys.modules))\n"
        "print(int('PySide6' in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=worker_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["0", "0", "0"]


def test_ping_real_worker_subprocess(tmp_path) -> None:
    started = time.monotonic()
    result = run_worker_task({"mode": "ping"}, output_dir=tmp_path / "ipc", timeout=60)
    assert result["ok"] is True
    assert result["mode"] == "ping"
    assert isinstance(result["pid"], int)
    assert time.monotonic() - started < 60


def test_real_worker_test_connection_returns_structured_result(tmp_path) -> None:
    """真实 worker 子进程执行 test_connection（只读结构探测，不读消息）。"""
    task = {
        "mode": "test_connection",
        "account": encode_account(make_account()),
        "allowlist": [],
    }
    try:
        result = run_worker_task(task, output_dir=tmp_path / "ipc", timeout=180)
    except CollectorWorkerError:
        pytest.skip("本机 UIA 环境不可用；worker 已按协议结构化降级，未影响主进程")
    connection = result.get("connection")
    assert isinstance(connection, dict)
    assert isinstance(connection.get("ok"), bool)
    assert isinstance(connection.get("detail"), str)


# ---------------------------------------------------------------------------
# 编解码 / fake worker 子进程
# ---------------------------------------------------------------------------

def test_fetch_messages_via_fake_worker_and_cleanup(tmp_path, worker_log) -> None:
    account = make_account()
    source = make_worker_source(tmp_path, log_dir=worker_log, account=account)
    conversation = make_conversation(
        "conv-1", account_id=account.id, title="张三", external_key="张三"
    )
    raw_messages = source.fetch_messages(
        conversation, since=None, limit=50, foreground_ack=True
    )
    assert len(raw_messages) == 1
    raw = raw_messages[0]
    assert raw.text == "请明天上午10点前提交周报"
    assert raw.sender == "张三"
    assert raw.is_self is False
    assert raw.timestamp is not None and raw.timestamp.tzinfo is not None
    assert raw.external_id == "fake-ext-1"
    assert raw.source_locator == "fake-row-1"
    assert raw.extras == {"fake": True}

    tasks = read_tasks(worker_log)
    assert len(tasks) == 1
    assert tasks[0]["mode"] == "fetch_messages"
    assert tasks[0]["allowlist"] == ["张三"]
    assert tasks[0]["foreground_ack"] is True
    assert tasks[0]["limit"] == 50
    assert tasks[0]["conversation_id"] == "conv-1"
    assert tasks[0]["account_id"] == account.id

    # IPC 临时文件（含正文）用完立即删除
    assert list((tmp_path / "ipc").glob("*.json")) == []


def test_list_conversations_and_test_connection_via_fake_worker(tmp_path, worker_log) -> None:
    source = make_worker_source(tmp_path, log_dir=worker_log)
    check = source.test_connection()
    assert check.ok is True
    assert "fake worker" in check.detail
    conversations = source.list_conversations()
    assert len(conversations) == 1
    assert conversations[0].title == "张三"
    assert conversations[0].kind.value == "private"
    modes = [task["mode"] for task in read_tasks(worker_log)]
    assert modes == ["test_connection", "list_conversations"]


def test_injected_runner_is_used(tmp_path) -> None:
    captured: dict = {}

    def runner(task: dict) -> dict:
        captured.update(task)
        return {
            "ok": True,
            "mode": task["mode"],
            "connection": {"ok": True, "detail": "injected runner", "discovered": {}},
        }

    source = WorkerQQMessageSource(
        account=make_account(), allowlist=["张三"], runner=runner
    )
    check = source.test_connection()
    assert check.ok is True and check.detail == "injected runner"
    assert captured["mode"] == "test_connection"
    assert captured["allowlist"] == ["张三"]


def test_injected_runner_bad_result_raises_protocol_error() -> None:
    source = WorkerQQMessageSource(
        account=make_account(), runner=lambda task: "not-a-dict"  # type: ignore[arg-type]
    )
    with pytest.raises(CollectorWorkerProtocolError):
        source.fetch_messages(make_conversation("conv-1", title="张三", external_key="张三"))


# ---------------------------------------------------------------------------
# 降级：崩溃 / 超时 / 坏 JSON / 缺文件 / 业务错误
# ---------------------------------------------------------------------------

def _fetch(source: WorkerQQMessageSource):
    return source.fetch_messages(
        make_conversation("conv-1", title="张三", external_key="张三"),
        limit=10,
        foreground_ack=True,
    )


def test_worker_crash_raises_collector_worker_error(tmp_path) -> None:
    source = make_worker_source(tmp_path, behavior="crash")
    with pytest.raises(CollectorWorkerError) as excinfo:
        _fetch(source)
    assert "exit=3" in str(excinfo.value)


def test_worker_timeout_kills_and_raises(tmp_path) -> None:
    source = make_worker_source(tmp_path, behavior="sleep", timeout=0.4)
    started = time.monotonic()
    with pytest.raises(CollectorWorkerTimeout):
        _fetch(source)
    assert time.monotonic() - started < 10


def test_worker_garbage_output_raises_protocol_error(tmp_path) -> None:
    source = make_worker_source(tmp_path, behavior="garbage")
    with pytest.raises(CollectorWorkerProtocolError):
        _fetch(source)


def test_worker_missing_output_file_raises_protocol_error(tmp_path) -> None:
    command = [sys.executable, "-c", "import sys; sys.stdin.read(); pass"]
    source = WorkerQQMessageSource(
        account=make_account(),
        allowlist=["张三"],
        command=command,
        output_dir=tmp_path / "ipc",
        timeout=30,
    )
    with pytest.raises(CollectorWorkerProtocolError):
        _fetch(source)


def test_worker_business_error_raises_worker_error(tmp_path) -> None:
    source = make_worker_source(tmp_path, behavior="error")
    with pytest.raises(CollectorWorkerError) as excinfo:
        _fetch(source)
    message = str(excinfo.value)
    assert "fake worker 返回业务错误" in message
    assert "fake_worker_error" in message


# ---------------------------------------------------------------------------
# 服务层集成：QQ 走 worker，失败降级不影响 UI
# ---------------------------------------------------------------------------

def _prepare_qq_account(service, *, allowlist=("张三",)):
    account = service.add_qq_account("QQ 测试", list(allowlist))
    service.storage.upsert_conversation(
        make_conversation(
            "conv-worker-1", account_id=account.id, title="张三", external_key="张三"
        )
    )
    service.grant_consent(ConsentScope.QQ_READ)
    # P0-22：实验性 QQ 读取默认关闭；本组用例验证 worker 行为，先显式开启。
    service.update_settings(qq_read_experimental=True)
    service.resume_collection()
    return account


def test_service_qq_scan_uses_worker_subprocess(tmp_path, worker_log) -> None:
    service = make_service(
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={"FAKE_WORKER_LOG_DIR": str(worker_log)},
        qq_worker_output_dir=tmp_path / "ipc",
    )
    try:
        account = service.add_qq_account("QQ 测试", ["张三"])
        service.grant_consent(ConsentScope.QQ_READ)
        service.update_settings(qq_read_experimental=True)
        check = service.refresh_conversations(account.id)
        assert check.ok is True
        service.resume_collection()

        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.OK
        assert results[0].messages_new == 1

        messages = service.list_messages()
        assert len(messages) == 1
        assert messages[0].text == "请明天上午10点前提交周报"
        assert messages[0].category is Category.IMPORTANT  # 真实 intel RuleClassifier
        assert "请明天上午10点前提交周报" not in str(service.storage.list_audit())

        source = service._make_source(account)
        assert isinstance(source, WorkerQQMessageSource)
        source.close()

        modes = [task["mode"] for task in read_tasks(worker_log)]
        assert modes[0] == "list_conversations"
        assert "fetch_messages" in modes
        assert list((tmp_path / "ipc").glob("*.json")) == []
    finally:
        service.shutdown()


def test_service_qq_worker_crash_degrades_to_failed(tmp_path, worker_log) -> None:
    service = make_service(
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={
            "FAKE_WORKER_LOG_DIR": str(worker_log),
            "FAKE_WORKER_BEHAVIOR": "crash",
        },
        qq_worker_output_dir=tmp_path / "ipc",
    )
    try:
        account = _prepare_qq_account(service)
        # test_account 不抛异常，只返回不可用
        check = service.test_account(account.id)
        assert check.ok is False
        assert "子进程" in check.detail

        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.FAILED
        assert "子进程" in (results[0].error or "")
        assert service.list_messages() == []
    finally:
        service.shutdown()


def test_service_qq_worker_timeout_degrades(tmp_path, worker_log) -> None:
    service = make_service(
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={
            "FAKE_WORKER_LOG_DIR": str(worker_log),
            "FAKE_WORKER_BEHAVIOR": "sleep",
        },
        qq_worker_timeout=0.5,
        qq_worker_output_dir=tmp_path / "ipc",
    )
    started = time.monotonic()
    try:
        account = _prepare_qq_account(service)
        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.FAILED
        assert "超时" in (results[0].error or "")
    finally:
        service.shutdown()
    assert time.monotonic() - started < 20


def test_service_qq_worker_bad_output_degrades(tmp_path, worker_log) -> None:
    service = make_service(
        qq_worker_command=FAKE_COMMAND,
        qq_worker_env={
            "FAKE_WORKER_LOG_DIR": str(worker_log),
            "FAKE_WORKER_BEHAVIOR": "garbage",
        },
        qq_worker_output_dir=tmp_path / "ipc",
    )
    try:
        account = _prepare_qq_account(service)
        results = service.run_scan(account.id)
        assert results[0].status is ScanStatus.FAILED
        assert "JSON" in (results[0].error or "")
    finally:
        service.shutdown()
