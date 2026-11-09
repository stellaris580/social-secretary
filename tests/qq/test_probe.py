"""qq_probe 只读结构探针的离线测试（fake 后端）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from social_secretary.collectors.qq import probe as probe_module
from social_secretary.collectors.qq.profile import load_profile

from .fake_uia import (
    CHAT_HWND,
    NOTEPAD_HWND,
    PANEL_HWND,
    FakeBackend,
    FakeNode,
    bubble_node,
    chat_window_root,
    make_window,
    panel_root,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _profile():  # noqa: ANN001
    return load_profile()


def _running_backend(*, header: str = "私密联系人", message: str = "秘密消息") -> FakeBackend:
    filler = FakeNode(
        cls="contact-list",
        ctype="Pane",
        children=[FakeNode(name=f"填充项{index}", ctype="Text") for index in range(30)],
    )
    root = panel_root(
        mode="efficiency",
        current_header=header,
        msg_bubbles=[bubble_node([message], mode="efficiency")],
        extra_children=[filler],
    )
    return FakeBackend(windows=[make_window(PANEL_HWND, "QQ", root=root)], foreground=NOTEPAD_HWND)


def test_probe_qq_not_running_reports_cleanly() -> None:
    backend = FakeBackend(no_qq=True, windows=[])
    report = probe_module.collect_probe(backend, _profile())
    assert report["status"] == "qq_not_running"
    assert report["processes"] == []
    assert report["windows"] == []
    assert report["safety"]["clicked"] is False
    json.dumps(report, ensure_ascii=False)  # 必须可序列化


def test_probe_default_is_desensitized() -> None:
    backend = _running_backend()
    report = probe_module.collect_probe(backend, _profile())
    dumped = json.dumps(report, ensure_ascii=False)
    assert report["status"] == "ok"
    assert "私密联系人" not in dumped
    assert "秘密消息" not in dumped
    assert "msg-content-container" in dumped  # 类名可以出现
    assert report["desensitized"] is True
    assert report["safety"]["names_included"] is False
    assert report["safety"]["message_text_read"] is False
    window = report["windows"][0]
    assert "title" not in window
    assert window["title_length"] == 2
    for entry in window["entries"]:
        assert "name" not in entry
        assert isinstance(entry["name_len"], int)
    assert report["aggregate"]["uia_roots_ok"] == 1
    assert report["aggregate"]["header_count"] >= 1
    assert report["aggregate"]["msg_area_count"] >= 1
    assert report["aggregate"]["bubbles_efficiency"] >= 1
    assert report["aggregate"]["mode_toggle_found"] >= 1


def test_probe_include_names_requires_explicit_option_and_includes_names() -> None:
    backend = _running_backend()
    options = probe_module.ProbeOptions(include_names=True)
    report = probe_module.collect_probe(backend, _profile(), options)
    dumped = json.dumps(report, ensure_ascii=False)
    assert "私密联系人" in dumped
    assert "秘密消息" in dumped
    assert report["safety"]["names_included"] is True
    assert "title" in report["windows"][0]
    assert any("name" in entry for entry in report["windows"][0]["entries"])


def test_probe_does_not_click_scroll_or_touch_clipboard() -> None:
    backend = _running_backend()
    backend.clipboard = "用户剪贴板"
    probe_module.collect_probe(backend, _profile())
    assert backend.clicks == []
    assert backend.wheels == []
    assert backend.paste_calls == []
    assert backend.set_value_calls == []
    assert backend.clipboard_sets == []
    assert backend.set_foreground_calls == []
    assert backend.clipboard == "用户剪贴板"
    assert backend.foreground == NOTEPAD_HWND


def test_probe_mode_filter_changes_analysis_mode() -> None:
    backend = _running_backend()
    report = probe_module.collect_probe(
        backend, _profile(), probe_module.ProbeOptions(mode_filter=probe_module.QqUiMode.CLASSIC)
    )
    assert report["mode_filter"] == "classic"
    assert report["windows"][0]["analysis_mode"] == "classic"


def test_probe_title_filter_limits_windows_without_leaking_title() -> None:
    panel = make_window(PANEL_HWND, "QQ", root=panel_root())
    chat = make_window(
        CHAT_HWND,
        "私密会话标题",
        root=chat_window_root(current_header="私密联系人", msg_bubbles=[bubble_node(["x"], mode="classic")]),
    )
    backend = FakeBackend(windows=[panel, chat], foreground=NOTEPAD_HWND)
    report = probe_module.collect_probe(
        backend, _profile(), probe_module.ProbeOptions(title_filter="私密")
    )
    assert report["title_filter_applied"] is True
    assert report["title_filter_matched_windows"] == 1
    assert len(report["windows"]) == 1
    dumped = json.dumps(report, ensure_ascii=False)
    assert "私密会话标题" not in dumped


def test_probe_activate_restores_foreground() -> None:
    backend = _running_backend()
    report = probe_module.collect_probe(
        backend, _profile(), probe_module.ProbeOptions(activate=True)
    )
    assert report["safety"]["foreground_changed"] is True
    assert report["safety"]["foreground_restored"] is True
    assert report["activate"]["activated"] is True
    assert PANEL_HWND in backend.set_foreground_calls
    assert backend.foreground == NOTEPAD_HWND
    # --activate 也只读结构，不点不滚不输入
    assert backend.clicks == []
    assert backend.wheels == []
    assert backend.paste_calls == []


def test_probe_launch_qq_missing_exe_does_not_start_anything(tmp_path: Path) -> None:
    profile = replace(_profile(), exe_path=str(tmp_path / "missing-QQ.exe"))
    backend = FakeBackend(no_qq=True, windows=[])
    report = probe_module.collect_probe(
        backend, profile, probe_module.ProbeOptions(launch_qq=True, launch_timeout_ms=200)
    )
    assert report["safety"]["launched_qq"] is False
    assert report["launch"]["attempted"] is True
    assert report["launch"]["exe_exists"] is False
    assert report["status"] == "qq_not_running"


def test_probe_sparse_tree_status() -> None:
    from .fake_uia import FakeNode

    sparse = FakeNode(name="QQ", cls="Chrome_WidgetWin_1", ctype="Window", children=[])
    backend = FakeBackend(
        windows=[make_window(PANEL_HWND, "QQ", root=sparse)],
        foreground=NOTEPAD_HWND,
    )
    report = probe_module.collect_probe(backend, _profile())
    assert report["status"] == "ok_sparse_tree"
    assert report["aggregate"]["node_count"] < 30


def test_write_report_writes_utf8_json(tmp_path: Path) -> None:
    backend = FakeBackend(no_qq=True, windows=[])
    report = probe_module.collect_probe(backend, _profile())
    target = probe_module.write_report(report, tmp_path / "sub" / "probe.json")
    assert target.is_file()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["status"] == "qq_not_running"
    assert loaded["tool"] == "tools/qq_probe.py"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_cli():
    path = PROJECT_ROOT / "tools" / "qq_probe.py"
    spec = importlib.util.spec_from_file_location("qq_probe_cli_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_no_write_summary(monkeypatch, capsys) -> None:
    cli = _load_cli()
    backend = FakeBackend(no_qq=True, windows=[])
    monkeypatch.setattr(probe_module, "make_backend", lambda: backend)
    assert cli.main(["--no-write"]) == 0
    captured = capsys.readouterr()
    assert "qq_not_running" in captured.out
    assert "状态" in captured.out
    assert backend.clicks == []


def test_cli_writes_json_and_reports_ok(monkeypatch, capsys, tmp_path: Path) -> None:
    cli = _load_cli()
    backend = _running_backend()
    monkeypatch.setattr(probe_module, "make_backend", lambda: backend)
    output = tmp_path / "cli-probe.json"
    assert cli.main(["--json-out", str(output), "--max-nodes", "500"]) == 0
    captured = capsys.readouterr()
    assert "JSON 已写入" in captured.out
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    dumped = output.read_text(encoding="utf-8")
    assert "私密联系人" not in dumped
    assert "秘密消息" not in dumped


def test_cli_include_names_requires_ack(monkeypatch, capsys) -> None:
    cli = _load_cli()

    def _must_not_create_backend():
        raise AssertionError("未确认隐私风险时不应创建后端")

    monkeypatch.setattr(probe_module, "make_backend", _must_not_create_backend)
    assert cli.main(["--include-names", "--no-write"]) == 2
    captured = capsys.readouterr()
    assert "拒绝执行" in captured.err


def test_cli_include_names_with_ack(monkeypatch, capsys) -> None:
    cli = _load_cli()
    backend = _running_backend()
    monkeypatch.setattr(probe_module, "make_backend", lambda: backend)
    assert cli.main(["--include-names", "--i-understand-names-are-private", "--no-write"]) == 0
    captured = capsys.readouterr()
    assert "已启用" in captured.err


def test_cli_activate_requires_no_extra_flag_but_prints_notice(monkeypatch, capsys) -> None:
    cli = _load_cli()
    backend = _running_backend()
    monkeypatch.setattr(probe_module, "make_backend", lambda: backend)
    assert cli.main(["--activate", "--no-write"]) == 0
    captured = capsys.readouterr()
    assert "前台" in captured.err
    assert backend.foreground == NOTEPAD_HWND
