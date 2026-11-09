"""安全边界测试：importing 包不得联网 / 读 QQ 本地数据 / 调用 QQ。

这些测试与 AGENTS.md 「铁律」对应，全部为静态 AST 检查或离线行为断言。
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import Any

import pytest

from social_secretary.importing import (
    EXPERIMENTAL,
    ImportOptions,
    parse_chat_text,
)

PKG = Path(__file__).resolve().parents[2] / "src" / "social_secretary" / "importing"

#: 导入包禁止出现的 import 根模块（网络 / QQ 自动化 / 进程调用）。
_BANNED_IMPORT_ROOTS = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "httplib",
    "requests",
    "aiohttp",
    "httpx",
    "ftplib",
    "telnetlib",
    "smtplib",
    "poplib",
    "imaplib",
    "subprocess",
    "ctypes",
    "uiautomation",
    "win32api",
    "win32gui",
    "win32clipboard",
    "PySide6",
    "keyring",
}

#: 禁止在源码字符串里出现的 QQ 本地数据/凭据相关标记（中文描述允许）。
_BANNED_SOURCE_MARKERS = (
    "nt_qq",
    "tencent files",
    "privatechatexport",
    "qq-msg-editor",
    "password",
    "passwd",
    "auth_code",
    "authorization_code",
)


def _importing_py_files() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    assert len(files) >= 4, f"importing 包文件过少（{len(files)}），审计无效"
    return files


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                roots.add(module.split(".")[0])
    return roots


def test_importing_package_has_no_network_or_qq_imports() -> None:
    violations: list[str] = []
    for path in _importing_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots = _import_roots(tree)
        bad = roots & _BANNED_IMPORT_ROOTS
        if bad:
            violations.append(f"{path.name}: {sorted(bad)}")
    assert not violations, "importing 包出现禁止的依赖：" + "; ".join(violations)


def test_importing_source_has_no_qq_local_data_markers() -> None:
    violations: list[str] = []
    for path in _importing_py_files():
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for marker in _BANNED_SOURCE_MARKERS:
            if marker in text:
                violations.append(f"{path.name}: {marker}")
    assert not violations, "importing 源码出现 QQ 本地数据/凭据标记：" + "; ".join(violations)


def test_parser_module_has_no_filesystem_writes() -> None:
    """parser.py 必须是纯函数式：不打开/写入任何文件（由工具负责读文件）。"""
    parser_path = PKG / "parser.py"
    tree = ast.parse(parser_path.read_text(encoding="utf-8"), filename=str(parser_path))
    banned_calls = {"open", "write_text", "write_bytes", "unlink", "mkdir", "rmdir"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in banned_calls:
                found.append(f"{name}@line{node.lineno}")
    assert not found, "parser.py 不应有文件系统副作用：" + ", ".join(found)


def test_parse_works_with_network_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行期把 socket 全部禁用，解析仍必须成功：证明无网络调用。"""

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("importing 解析器不应发起任何网络连接")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    result = parse_chat_text(
        "2024-01-01 12:30:00 张三: 你好",
        options=ImportOptions(now=None, source_name="offline.txt"),
    )
    assert len(result.messages) == 1
    assert result.messages[0].raw.text == "你好"


def test_experimental_notice_required() -> None:
    assert EXPERIMENTAL is True
    from social_secretary.importing import EXPERIMENTAL_NOTICE

    assert "实验性" in EXPERIMENTAL_NOTICE
    assert "不会自动同步" in EXPERIMENTAL_NOTICE
    assert "自动读取 QQ" in EXPERIMENTAL_NOTICE


def test_docs_research_file_marks_experimental_and_unverified() -> None:
    docs = Path(__file__).resolve().parents[2] / "docs" / "research" / "chat-text-import.md"
    if not docs.is_file():
        pytest.skip("研究文档尚未创建")
    content = docs.read_text(encoding="utf-8", errors="replace")
    assert "实验性" in content
    assert "[未验证]" in content
    assert "不得宣称" in content or "不宣称" in content
