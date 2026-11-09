"""tools/mail_acceptance.py 的自动化测试（mock IMAP 全流程；不依赖真实凭据/真实邮箱）。"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from .helpers import REPO_ROOT

TOOL_PATH = REPO_ROOT / "tools" / "mail_acceptance.py"


@pytest.fixture(scope="module")
def tool():
    module_name = "ss_mail_acceptance_tool"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_input_validation(tool):
    with pytest.raises(tool.AcceptanceInputError):
        tool.run_mail_acceptance(email="not-an-email", auth_code="x", write_evidence_file=False)
    with pytest.raises(tool.AcceptanceInputError):
        tool.run_mail_acceptance(email="user@qq.com", auth_code="", write_evidence_file=False)
    with pytest.raises(tool.AcceptanceInputError):
        tool.run_mail_acceptance(
            email="user@qq.com", auth_code="abcdef", count=0, write_evidence_file=False
        )
    with pytest.raises(tool.AcceptanceInputError):
        tool.run_mail_acceptance(
            email="user@qq.com", auth_code="abcdef", count=201, write_evidence_file=False
        )
    # 域名少字母（如 qq.cm）必须被拦截并给出中文提示（用户真实验收曾因此失败）
    with pytest.raises(tool.AcceptanceInputError) as exc_info:
        tool.run_mail_acceptance(
            email="123456789@qq.cm", auth_code="abcdef", write_evidence_file=False
        )
    assert "qq.cm" in str(exc_info.value)
    assert "qq.com" in str(exc_info.value)


def test_mask_email_and_domain_helpers(tool):
    """mask_email 只脱敏本地部分，域名必须完整保留（曾疑似域名被截断，回归锁定）。"""
    assert tool.mask_email("123456789@qq.com") == "1***@qq.com"
    assert tool.mask_email("user@vip.qq.com") == "u***@vip.qq.com"
    assert tool.mask_email("user@foxmail.com") == "u***@foxmail.com"
    assert tool._domain_of("123456789@qq.com") == "qq.com"
    assert tool.validate_acceptance_input("123456789@qq.com", "abcdef", 20) is None
    for domain in ("qq.com", "vip.qq.com", "foxmail.com"):
        tool.validate_acceptance_input(f"user@{domain}", "abcdef", 1)


def test_mock_full_acceptance_flow(tool, tmp_path):
    """mock IMAP 下完整验收：授权→首次同步最近 N 封→增量 0 新增→摘要→负向不误报。"""
    data_dir = tmp_path / "data"
    evidence_dir = tmp_path / "evidence"
    result = tool.run_mock_acceptance(
        data_dir=data_dir, evidence_dir=evidence_dir, count=3
    )

    assert result["ok"] is True, result.get("errors")
    checks = result["checks"]
    for key in (
        "credential_saved",
        "consent_granted",
        "connection_ok",
        "sync_ok",
        "data_present",
        "incremental_ok",
        "no_duplicate_ok",
        "summary_ok",
        "negative_ok",
        "readonly_ok",
    ):
        assert checks.get(key) is True, key

    # 首次同步：按"最近 N 封"读取 UID 4..6（mock 共 6 封，N=3）
    first = result["first_sync"]
    assert first["messages_seen"] == 3
    assert first["messages_new"] == 3
    assert first["uid_range"]["min"] == 4
    assert first["uid_range"]["max"] == 6

    # 增量：0 新增、入库条数不变、无重复哈希
    incremental = result["incremental_sync"]
    assert incremental["messages_new"] == 0
    assert incremental["stored_before"] == incremental["stored_after"] == 3
    assert incremental["duplicate_content_hashes"] == 0

    # 摘要：真实入库的合成消息生成计数/分类/待办
    summary = result["summary"]["today"]
    assert summary["counts"]["total"] == 3
    assert summary["open_todos"] >= 1
    assert summary["is_demo"] is False

    # 负向：错误授权码明确失败、0 条入库、不误报成功
    negative = result["negative"]
    assert negative["ok"] is True
    assert negative["auth_check_ok"] is False
    assert negative["misreported_success"] is False
    assert negative["messages_stored"] == 0
    assert negative["error_code"] == "MAIL_AUTH_FAILED"

    # 只读性（EXAMINE 只读结果 + mock 服务器记录）
    assert result["checks"]["readonly_ok"] is True
    assert result["readonly"]["examine_readonly"] is True
    assert result["readonly"]["server_verified"] is True
    assert result["readonly"]["write_commands_seen"] == []
    assert result["readonly"]["non_peek_fetch_uids"] == []
    assert set(result["readonly"]["observed_verbs"]) <= {
        "CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT"
    }

    # 证据：脱敏（无授权码、无完整邮箱、无正文/主题原文）
    assert result["evidence_path"]
    text = (evidence_dir / result["evidence_path"].split("\\")[-1]).read_text(encoding="utf-8")
    assert "mock-auth-code" not in text
    assert "wrong-code" not in text
    assert "test@qq.com" not in text
    assert "合成验收邮件" not in text
    assert "任务：请在明天" not in text
    payload = json.loads(text)
    assert payload["mode"] == "mock"
    assert payload["ok"] is True
    assert payload["privacy_note"]


def test_format_lines_failure_has_actionable_hints(tool):
    failure = {
        "ok": False,
        "mode": tool.MODE_REAL,
        "email_masked": "1***@qq.com",
        "email_domain": "qq.com",
        "checks": {"credential_saved": True, "connection_ok": False},
        "errors": ["邮箱连接/认证失败（MAIL_AUTH_FAILED）"],
    }
    text = "\n".join(tool._format_result_lines(failure))
    assert "验收未通过" in text
    assert "MAIL_AUTH_FAILED" in text
    assert "分项检查" in text
    assert "123456789@qq.com" not in text


def test_gui_self_test(tool):
    try:
        import tkinter

        probe = tkinter.Tk()
        probe.destroy()
    except Exception:  # noqa: BLE001 - 无图形环境则跳过（CI/SSH）
        pytest.skip("当前环境无法创建 Tkinter 窗口")
    assert tool.run_self_test() == 0
