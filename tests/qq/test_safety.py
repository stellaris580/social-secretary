"""只读安全边界的静态审计测试（AST / 源码扫描）。

这些测试防止未来有人「顺手」给 QQ 采集器加上发送能力，或者把 uiautomation
变成 import 时依赖（那会破坏离线测试）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from social_secretary.collectors.qq.profile import default_profile_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QQ_PACKAGE = PROJECT_ROOT / "src" / "social_secretary" / "collectors" / "qq"
PROBE_TOOL = PROJECT_ROOT / "tools" / "qq_probe.py"

_FORBIDDEN_MODULES = {"smtplib", "imaplib", "win32com.client", "wmi"}


def _package_sources() -> dict[str, str]:
    sources = {path.name: path.read_text(encoding="utf-8") for path in QQ_PACKAGE.glob("*.py")}
    sources[PROBE_TOOL.name] = PROBE_TOOL.read_text(encoding="utf-8")
    return sources


def _iter_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node


def test_package_has_no_forbidden_network_or_mail_modules() -> None:
    for name, source in _package_sources().items():
        tree = ast.parse(source)
        for module, _node in _iter_imports(tree):
            assert module not in _FORBIDDEN_MODULES, f"{name} 禁止导入 {module}"


def test_uiautomation_is_not_imported_at_module_level() -> None:
    """uiautomation 只能在函数内部延迟导入（保证离线测试/纯导入安全）。"""

    for name, source in _package_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [alias.name for alias in getattr(node, "names", [])]
                modules.append(getattr(node, "module", "") or "")
                if any(module.split(".")[0] == "uiautomation" for module in modules):
                    parents = [
                        child
                        for child in ast.walk(tree)
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]
                    inside_function = any(
                        node in ast.walk(func) for func in parents
                    )
                    assert inside_function, f"{name} 在模块顶层导入了 uiautomation"


def test_no_send_or_keyboard_injection_helpers_outside_search_paste() -> None:
    forbidden_tokens = [
        "smtplib",
        "SendMessage(",
        "PostMessage(",
        "keybd_event",
        "SendInput",
        "press_enter",
        "send_message(",
    ]
    for name, source in _package_sources().items():
        for token in forbidden_tokens:
            assert token not in source, f"{name} 出现禁止关键字 {token}"

    # uiautomation 的 SendKeys 只允许出现在 backend.py 的搜索框粘贴函数内。
    backend_source = _package_sources()["backend.py"]
    paste_function_start = backend_source.index("def paste_text_into_focused_control")
    sendkeys_occurrences = [
        index for index in range(len(backend_source)) if backend_source.startswith("SendKeys", index)
    ]
    assert sendkeys_occurrences, "backend.py 应包含搜索框粘贴所需的 SendKeys（仅此一处用途）"
    for index in sendkeys_occurrences:
        assert index > paste_function_start, "SendKeys 只能出现在搜索框剪贴板回退函数内"


def test_profile_has_no_send_button_selector() -> None:
    data = json.loads(default_profile_path().read_text(encoding="utf-8"))

    def _walk(node) -> list[str]:  # noqa: ANN001
        keys: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                keys.append(str(key))
                keys.extend(_walk(value))
        elif isinstance(node, list):
            for item in node:
                keys.extend(_walk(item))
        return keys

    selector_keys: list[str] = []
    for section in ("modes", "windows", "mode_toggle", "behavior"):
        selector_keys.extend(_walk(data.get(section)))
    assert not any(
        "send_button" in key.casefold() or key.casefold() == "send" for key in selector_keys
    ), selector_keys
    assert data["safety"]["send_capability"] == "none"
    assert data["safety"]["read_only"] is True
    assert any("发送按钮" in item for item in data["safety"]["forbidden_actions"])


def test_no_qq_local_database_paths_in_source() -> None:
    forbidden_paths = [
        r"D:\path\to\qq-data",
        "nt_qq",
        "Tencent Files",
        "PrivateChatExport",
        "\\AppData\\Roaming\\Tencent",
    ]
    for name, source in _package_sources().items():
        for forbidden in forbidden_paths:
            assert forbidden not in source, f"{name} 出现本地数据库路径 {forbidden}"


def test_package_import_does_not_load_uiautomation() -> None:
    import sys

    import social_secretary.collectors.qq  # noqa: F401
    import social_secretary.collectors.qq.probe  # noqa: F401

    assert "uiautomation" not in sys.modules
    from social_secretary.collectors.qq import QqMessageSource

    assert hasattr(QqMessageSource, "fetch_messages")
