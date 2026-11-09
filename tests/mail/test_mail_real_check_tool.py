"""tools/mail_real_check.py 的核心逻辑测试（无 GUI、无网络、无真实授权码）。

GUI 交互由 `tools/mail_real_check.py --self-test`（真实创建 Tkinter 窗口）覆盖，
结果写入 evidence/mail/；pytest 里只测可离线、确定性的核心逻辑。
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from .helpers import REPO_ROOT

TOOL_PATH = REPO_ROOT / "tools" / "mail_real_check.py"


@pytest.fixture(scope="module")
def tool():
    """把 tools/mail_real_check.py 作为独立模块导入（tools 不是 Python 包）。"""
    module_name = "ss_mail_real_check_tool"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_invalid_email_makes_no_connection(tool):
    result = tool.check_qq_mail_readonly("not-an-email", "whatever")
    assert result["ok"] is False
    assert result["errors"][0]["code"] == tool.TOOL_INPUT_INVALID
    assert result["login_attempted"] is False
    assert result["commands"] == []


def test_missing_auth_code_makes_no_connection(tool):
    result = tool.check_qq_mail_readonly("123456789@qq.com", "")
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MAIL_CREDENTIAL_MISSING"
    assert result["login_attempted"] is False
    assert result["commands"] == []
    assert "授权码" in result["errors"][0]["message"]


def test_evidence_is_redacted(tool, tmp_path):
    auth_sentinel = "AUTHCODE-SECRET-SENTINEL"
    body_sentinel = "机密正文原文-SENTINEL"
    result = {
        "schema_version": 1,
        "tool": "tools/mail_real_check.py",
        "ok": True,
        "account_masked": tool.mask_email("123456789@qq.com"),
        "target": {"host": "imap.qq.com", "port": 993, "mailbox": "INBOX"},
        "read_only": True,
        "commands": ["CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"],
        "emails": [
            {
                "uid": 1,
                "subject_len": 4,
                "subject_sha256_16": tool._sha256_16("合成主题"),
                "body_len": len(body_sentinel),
                "body_sha256_16": tool._sha256_16(body_sentinel),
                "from_domain": "example.com",
            }
        ],
        "errors": [],
    }
    path = tool.write_evidence(result, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert auth_sentinel not in text
    assert body_sentinel not in text
    assert "123456789@qq.com" not in text
    assert "1***@qq.com" in text
    parsed = json.loads(text)
    assert parsed["read_only"] is True
    assert len(parsed["emails"][0]["subject_sha256_16"]) == 16
    assert "LOGIN" in parsed["commands"]


def test_result_lines_have_chinese_hints(tool):
    success = {
        "ok": True,
        "account_masked": "1***@qq.com",
        "target": {"host": "imap.qq.com", "port": 993, "mailbox": "INBOX"},
        "tls": {"version": "TLSv1.2", "cipher": "ECDHE-RSA-AES128-GCM-SHA256"},
        "capabilities": ["IMAP4REV1"],
        "mailbox": {"exists": 2, "uidvalidity": 1, "uidnext": 3},
        "fetched_count": 1,
        "uid_range": {"min": 1, "max": 1},
        "emails": [],
    }
    success_text = "\n".join(tool._format_result_lines(success))
    assert "只读验证成功" in success_text
    assert "1***@qq.com" in success_text
    assert "TLSv1.2" in success_text

    failure = {
        "ok": False,
        "errors": [
            {
                "code": "MAIL_AUTH_FAILED",
                "message": tool.USER_MESSAGES[tool.MailErrorCode.AUTH_FAILED],
                "action": tool._ACTION_HINTS[tool.MailErrorCode.AUTH_FAILED],
            }
        ],
    }
    failure_text = "\n".join(tool._format_result_lines(failure))
    assert "邮箱登录失败" in failure_text
    assert "授权码" in failure_text
    assert "处理建议" in failure_text


def test_core_self_test_passes(tool):
    assert tool.run_core_self_test() == 0
