"""只读安全审计：静态扫描 ``src/``，断言程序**不存在任何发送能力**。

这是本项目"绝对只读"铁律（AGENTS.md §0.1、README 安全底线）的可执行证明：
测试直接解析源码 AST（不依赖运行、不依赖第三方），在源码层面禁止以下能力：

1. SMTP / 发邮件：``smtplib``、SMTP 类、``sendmail`` 等调用；
2. 点击 QQ "发送"按钮 / 触发发送命令；
3. 向 QQ 消息输入框（``qq-msg-editor``）SetValue / 粘贴 / 按键 / 点击；
4. ``SendKeys`` / ``SendInput`` / ``keybd_event`` 键盘注入（搜索框除外）；
5. UI 中出现"发送"按钮 / 菜单项（"本程序不发送消息"这类提示文案不算）；
6. 读取 QQ 本地消息数据库（``nt_qq`` / ``Tencent Files`` / ``PrivateChatExport``）；
7. IMAP 写操作（STORE/EXPUNGE/DELETE/APPEND/COPY/MOVE）；
8. 使用 QQ 非官方协议库（qqbot / nonebot / onebot / mirai 等）。

允许的能力（白名单，见 AGENTS.md §0.1）：QQ 窗口/控件树读取、文本读取、消息列表
滚动、**仅搜索框**写入待读会话名（优先 ValuePattern，其次剪贴板），IMAP 只读
（EXAMINE/FETCH BODY.PEEK）。

补充的隐私集成测试（app-core 提供对应模块时才运行，否则 skip）：
- 凭据存储不得把明文写到普通文件；
- 日志脱敏工具 ``redact_text`` 不得原样输出敏感串。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/security/test_no_send_capabilities.py -q
"""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = SRC / "social_secretary"

#: 明确允许的"仅搜索框"低层回退原语（AGENTS.md §0.1 允许搜索框剪贴板回退）。
#: 键：(相对仓库根的文件路径, 函数名)；值：允许理由。
#: 只有在这里登记、并且有交叉校验测试（见 test_search_fallback_is_narrow_and_guarded）
#: 的函数，才允许出现 SendKeys / 剪贴板写入。新增条目必须能说明
#: "为什么它不可能把内容写进 QQ 消息编辑框"。
SEARCH_FALLBACK_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "src/social_secretary/collectors/qq/backend.py",
        "paste_text_into_focused_control",
    ): "文档明确只允许搜索框使用；调用方 collector._write_search_query 有焦点校验 + 编辑框拒绝校验",
    (
        "src/social_secretary/collectors/qq/backend.py",
        "clipboard_set_text",
    ): "低层剪贴板写入原语，仅供 paste_text_into_focused_control 的保存/恢复使用",
    (
        "src/social_secretary/collectors/qq/backend.py",
        "send_keys",
    ): "paste_text_into_focused_control 内部的 Ctrl+A/Ctrl+V；调用方已校验焦点在搜索框",
}



# ---------------------------------------------------------------------------
# 扫描框架
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于失败信息
        return f"[{self.rule}] {self.file}:{self.line} {self.message}"


def _py_files() -> list[Path]:
    if not PKG.is_dir():
        pytest.fail(f"源码目录不存在: {PKG}")
    files = sorted(PKG.rglob("*.py"))
    assert len(files) >= 5, f"扫描到的源码文件过少（{len(files)}），审计无效"
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(SRC.parent)).replace("\\", "/")


def _dotted(node: ast.AST) -> str:
    """把 ``a.b.c`` 形式的表达式还原成点号字符串，其它返回空串。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


class _FileScanner:
    """单个文件的 AST 扫描器，收集所有违规。"""

    #: 模块导入黑名单（按根模块名匹配）
    DENY_IMPORT_ROOTS = {
        "smtplib",
        "aiosmtplib",
        "qqbot",
        "botpy",
        "nonebot",
        "onebot",
        "mirai",
        "go_cqhttp",
    }
    #: 禁止的调用名（小写比较，覆盖 camelCase 变体）
    DENY_CALL_NAMES = {
        "sendmail",
        "send_message",
        "send_email",
        "send_mail",
        "send_qq_message",
        "sendmsg",
        "send_message_to",
        "sendtocontact",
    }
    #: 禁止出现的 SMTP 相关属性名
    DENY_ATTR_NAMES = {"SMTP", "SMTP_SSL", "SMTPClient", "SMTP_SSLClient"}

    CLICK_LIKE = {"click", "click_input", "clickinput", "invoke", "press"}
    KEYBOARD_INJECT = {"sendkeys", "send_keys", "sendinput", "keybd_event", "keybd_event_ex"}
    EDITOR_WRITE = {
        "setvalue",
        "set_value",
        "sendkeys",
        "send_keys",
        "paste",
        "settext",
        "set_text",
        "type",
        "typetext",
        "type_text",
        "keypress",
        "key_press",
        "click",
        "click_input",
        "focus",
        "setfocus",
    }
    CLIPBOARD_WRITE = {"setclipboarddata", "emptyclipboard", "setclipboardtext", "copy", "settext"}
    BUTTON_FACTORIES = {"QPushButton", "QToolButton", "QAction", "QCommandLinkButton"}
    CLAIMED_SEND_WORDS = ("发送",)
    INFORMATIONAL_MARKERS = ("不发送", "禁止发送", "无法发送", "不能发送", "只读", "不会发送")
    EDITOR_MARKERS = ("qq-msg-editor", "msg-editor", "msgeditor", "message_editor", "messageeditor")
    SEARCH_MARKERS = ("search", "搜索")
    QQ_DB_MARKERS = ("nt_qq", "tencent files", "privatechatexport", "\\nt_qq", "tencentfiles")
    IMAP_WRITE = {"store", "expunge", "append", "copy", "move", "rename", "setflags", "set_flags"}
    IMAP_WRITE_COMMAND_LITERALS = {"STORE", "EXPUNGE", "DELETE", "APPEND", "COPY", "MOVE", "RENAME", "SETFLAGS"}
    CLIPBOARD_MODULES = {"win32clipboard", "pyperclip", "clipboard"}

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rel = _rel(path)
        self.text = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            self.tree = ast.parse(self.text, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - 语法错误应被 pytest 其它测试抓到
            raise AssertionError(f"无法解析 {self.rel}: {exc}") from exc
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.imported_modules: set[str] = set()
        self.violations: list[Violation] = []

    # -- 工具 ---------------------------------------------------------------
    def _add(self, rule: str, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 1)
        self.violations.append(Violation(rule, self.rel, line, message))

    def _src(self, node: ast.AST) -> str:
        seg = ast.get_source_segment(self.text, node)
        return seg or ""

    def _enclosing_function_names(self, node: ast.AST) -> list[str]:
        """从内到外的所有外层函数名（含嵌套函数）。"""
        names: list[str] = []
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(cur.name)
            cur = self.parents.get(cur)
        return names

    def _enclosing_docstrings(self, node: ast.AST) -> str:
        """所有外层函数的 docstring 拼接（用于识别"仅搜索框"这类安全声明）。"""
        parts: list[str] = []
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(cur) or ""
                if doc:
                    parts.append(doc)
            cur = self.parents.get(cur)
        return " ".join(parts)

    def _enclosing_names(self, node: ast.AST) -> str:
        return " ".join(reversed(self._enclosing_function_names(node)))

    def _is_search_context(self, node: ast.AST) -> bool:
        blob = (
            self._src(node)
            + " "
            + self._enclosing_names(node)
            + " "
            + self._enclosing_docstrings(node)
        ).lower()
        return any(m in blob for m in self.SEARCH_MARKERS)

    def _in_search_fallback_allowlist(self, node: ast.AST) -> str | None:
        """命中登记过的"仅搜索框"低层回退原语时返回理由，否则 None。"""
        for func_name in self._enclosing_function_names(node):
            reason = SEARCH_FALLBACK_ALLOWLIST.get((self.rel, func_name))
            if reason:
                return reason
        return None


    def _mentions_editor(self, node: ast.AST) -> bool:
        blob = (self._src(node) + " " + self._enclosing_names(node)).lower()
        return any(m in blob for m in self.EDITOR_MARKERS)

    def _clipboard_aliases(self) -> set[str]:
        aliases: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    root = a.name.split(".")[0]
                    if root in self.CLIPBOARD_MODULES:
                        aliases.add(a.asname or root)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in self.CLIPBOARD_MODULES:
                    aliases.add(node.module)
                    for a in node.names:
                        aliases.add(a.asname or a.name)
        return aliases

    # -- 规则 ---------------------------------------------------------------
    def scan(self) -> list[Violation]:
        clipboard_aliases = self._clipboard_aliases()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._check_import(node, alias.name)
            elif isinstance(node, ast.ImportFrom):
                self._check_import(node, node.module or "")

            if isinstance(node, ast.Attribute):
                if node.attr in self.DENY_ATTR_NAMES:
                    self._add("smtp-capability", node, f"出现 SMTP 类属性 {node.attr}（禁止发邮件能力）")

            if isinstance(node, ast.Call):
                self._check_call(node, clipboard_aliases)
        return self.violations

    def _check_import(self, node: ast.AST, module: str) -> None:
        self.imported_modules.add(module.split(".")[0])
        root = module.split(".")[0]
        if root in self.DENY_IMPORT_ROOTS:
            self._add("deny-import", node, f"导入被禁止的模块 {module}（发送/非官方协议能力）")
        lowered = module.lower()
        if "smtp" in lowered:
            self._add("smtp-capability", node, f"导入 SMTP 相关模块 {module}（禁止发邮件能力）")

    def _check_call(self, node: ast.Call, clipboard_aliases: set[str]) -> None:
        dotted = _dotted(node.func)
        name = dotted.split(".")[-1] if dotted else ""
        lname = name.lower()
        segment = self._src(node)
        segment_lower = segment.lower()

        # 1) 发送类函数名
        if lname in self.DENY_CALL_NAMES:
            self._add("send-call", node, f"调用发送类函数 {dotted}（禁止发送消息/邮件）")

        # 2) 点击"发送"按钮
        if lname in self.CLICK_LIKE and any(w in segment for w in self.CLAIMED_SEND_WORDS):
            if not self._is_search_context(node):
                self._add("click-send-button", node, f"疑似点击“发送”按钮：{segment.strip()[:120]}")
        if lname == "click" and re.search(r"""name\s*=\s*['"]发送['"]""", segment):
            if not self._is_search_context(node):
                self._add("click-send-button", node, "疑似按控件名“发送”点击")

        # 3) 键盘注入（SendKeys / SendInput / keybd_event）
        if lname in self.KEYBOARD_INJECT:
            if self._mentions_editor(node):
                self._add("sendkeys-message-editor", node, f"对消息输入框使用键盘注入：{segment.strip()[:120]}")
            elif not self._is_search_context(node) and not self._in_search_fallback_allowlist(node):
                self._add("sendkeys-unscoped", node, f"键盘注入缺少搜索框限定（只允许搜索框）：{segment.strip()[:120]}")


        # 4) 写入 QQ 消息输入框
        if lname in self.EDITOR_WRITE and self._mentions_editor(node):
            self._add("write-message-editor", node, f"向 QQ 消息输入框写入/点击：{segment.strip()[:120]}")
        if lname in self.CLICK_LIKE and any(m in segment_lower for m in self.EDITOR_MARKERS):
            self._add("click-message-editor", node, f"点击 QQ 消息输入框：{segment.strip()[:120]}")

        # 5) 剪贴板写入只允许用于搜索框（或登记过的搜索框回退原语）
        receiver_root = dotted.split(".")[0] if dotted else ""
        is_clipboard_write = lname in self.CLIPBOARD_WRITE and (
            receiver_root in clipboard_aliases
            or any(a.lower() in receiver_root.lower() for a in clipboard_aliases)
            or "clipboard" in dotted.lower()
        )
        if is_clipboard_write:
            if self._mentions_editor(node):
                self._add(
                    "clipboard-write-message-editor",
                    node,
                    f"向 QQ 消息编辑框方向写剪贴板/粘贴：{segment.strip()[:120]}",
                )
            elif not self._is_search_context(node) and not self._in_search_fallback_allowlist(node):
                self._add(
                    "clipboard-write-non-search",
                    node,
                    f"剪贴板写入未限定为搜索框用途（AGENTS 只允许搜索框剪贴板回退）：{segment.strip()[:120]}",
                )


        # 6) UI 发送按钮 / 菜单项
        if name in self.BUTTON_FACTORIES:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    v = arg.value
                    if any(w in v for w in self.CLAIMED_SEND_WORDS) and not any(
                        m in v for m in self.INFORMATIONAL_MARKERS
                    ):
                        self._add("ui-send-button", node, f"UI 中出现“发送”按钮/菜单项：{v!r}（禁止发送能力）")

        # 7) QQ 本地消息数据库路径（只读铁律 §0.2）
        if self.rel.startswith("src/social_secretary/collectors/"):
            if lname in {"open", "read_text", "read_bytes", "glob", "rglob", "iterdir", "exists"}:
                for m in self.QQ_DB_MARKERS:
                    if m in segment_lower:
                        self._add("qq-local-db", node, f"疑似访问 QQ 本地消息数据库路径：{segment.strip()[:120]}")

        # 8) IMAP 写操作（只读铁律 §0.1：不得删除/修改远端消息）
        if self.rel.startswith("src/social_secretary/collectors/mail/"):
            if lname in self.IMAP_WRITE and "imap" in dotted.lower():
                self._add("imap-write", node, f"IMAP 写操作 {dotted}（禁止删除/修改远端邮件）")
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.strip().upper() in self.IMAP_WRITE_COMMAND_LITERALS:
                        self._add("imap-write", node, f"IMAP 写命令字面量 {arg.value!r}")


def collect_violations(files: list[Path] | None = None) -> list[Violation]:
    out: list[Violation] = []
    for path in files if files is not None else _py_files():
        out.extend(_FileScanner(path).scan())
    return out


# ---------------------------------------------------------------------------
# 1) 源码静态审计
# ---------------------------------------------------------------------------

def test_source_tree_contains_no_send_capabilities() -> None:
    """整个 src/ 不得出现任何发送 QQ 消息/发送邮件/修改远端数据的能力。"""
    violations = collect_violations()
    if violations:
        detail = "\n".join(f"  - {v}" for v in violations)
        pytest.fail(f"发现 {len(violations)} 处越界能力（违反只读铁律）：\n{detail}")


def test_search_fallback_is_narrow_and_guarded() -> None:
    """搜索框剪贴板回退白名单必须"窄 + 有安全校验"，防止被滥用成任意输入通道。

    校验三件事：
    1. 白名单只允许 collectors/qq/backend.py 里的低层原语（不允许扩散到 UI/核心层）；
    2. backend.py 中存在"只允许搜索框使用"的显式文档；
    3. collector.py 调用回退原语的位置在外层函数里，且做了"焦点校验 + 拒绝消息编辑框"。
    """
    assert SEARCH_FALLBACK_ALLOWLIST, "允许列表不应为空（项目允许搜索框剪贴板回退）"
    for (rel_path, func_name), reason in SEARCH_FALLBACK_ALLOWLIST.items():
        assert rel_path == "src/social_secretary/collectors/qq/backend.py", (
            f"搜索框回退白名单范围必须限制在 QQ backend 原语：{rel_path}:{func_name}"
        )
        assert reason and len(reason) >= 8, f"白名单条目必须写明理由：{rel_path}:{func_name}"

    backend = PKG / "collectors" / "qq" / "backend.py"
    assert backend.is_file(), "QQ backend 不存在，白名单失效"
    backend_src = backend.read_text(encoding="utf-8-sig", errors="replace")
    assert ("只允许搜索框" in backend_src) or ("仅搜索框" in backend_src), (
        "backend.py 缺少“只允许搜索框”的安全声明，剪贴板/键盘回退不可接受"
    )
    # 白名单函数必须真实存在（防止登记名写错而悄悄变成"放行一切"）
    backend_tree = ast.parse(backend_src)
    defined = {n.name for n in ast.walk(backend_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for (_rel_path, func_name) in SEARCH_FALLBACK_ALLOWLIST:
        assert func_name in defined, f"白名单登记的 {func_name} 在 backend.py 中不存在"

    collector = PKG / "collectors" / "qq" / "collector.py"
    if not collector.is_file():
        pytest.skip("QQ collector 尚未实现")
    collector_src = collector.read_text(encoding="utf-8-sig", errors="replace")
    assert "_assert_not_message_editor" in collector_src, "collector.py 缺少“拒绝消息编辑框”校验"
    assert "paste_text_into_focused_control" in collector_src, "collector.py 未使用搜索框回退原语"
    # 调用点必须位于名字含 search 的函数内，且该函数同时含编辑框拒绝校验
    tree = ast.parse(collector_src)
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    calls_found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if not dotted.endswith("paste_text_into_focused_control"):
            continue
        calls_found += 1
        cur: ast.AST | None = node
        enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = cur
                break
            cur = parent_map.get(cur)
        assert enclosing is not None, "回退原语调用不在任何函数内"
        assert "search" in enclosing.name.lower(), (
            f"搜索框剪贴板回退只能在 search 相关函数内调用，当前在 {enclosing.name}"
        )
        func_src = ast.get_source_segment(collector_src, enclosing) or ""
        assert "_assert_not_message_editor" in func_src, (
            f"{enclosing.name} 调用剪贴板回退前必须校验目标不是消息编辑框"
        )
    assert calls_found >= 1, "未找到搜索框剪贴板回退调用点（白名单可能已过期，应移除）"



def test_qq_collector_never_targets_message_editor_for_input() -> None:
    """QQ 采集器允许搜索框输入，但不得把任何输入/点击/粘贴指向 qq-msg-editor。"""
    qq_dir = PKG / "collectors" / "qq"
    if not qq_dir.is_dir():
        pytest.skip("QQ 采集器尚未实现")
    files = sorted(qq_dir.rglob("*.py"))
    assert files, "QQ 采集器目录为空"
    for path in files:
        for v in _FileScanner(path).scan():
            if v.rule in {"write-message-editor", "click-message-editor", "sendkeys-message-editor"}:
                pytest.fail(f"QQ 采集器存在消息输入框写入：{v}")


def test_mail_collector_is_read_only() -> None:
    """邮件采集器不得出现 SMTP 发送、也不得出现 IMAP 写操作。"""
    mail_dir = PKG / "collectors" / "mail"
    if not mail_dir.is_dir():
        pytest.skip("邮件采集器尚未实现")
    files = sorted(mail_dir.rglob("*.py"))
    assert files, "邮件采集器目录为空"
    violations = collect_violations(files)
    if violations:
        detail = "\n".join(f"  - {v}" for v in violations)
        pytest.fail(f"邮件采集器存在写/发送能力：\n{detail}")


def test_no_network_send_in_ui_layer() -> None:
    """UI 层不得包含任何发送/网络提交能力（UI 只依赖 ServiceAPI 本地对象）。"""
    ui_dir = PKG / "ui"
    if not ui_dir.is_dir():
        pytest.skip("UI 尚未实现")
    allow = {"http", "urllib", "requests", "httpx"}
    for path in sorted(ui_dir.rglob("*.py")):
        scanner = _FileScanner(path)
        for module in scanner.imported_modules & allow:
            # 外部 AI 的 HTTP 调用只能在 intel/external_ai.py，UI 不允许直接联网
            pytest.fail(f"UI 层直接导入网络模块 {module}：{_rel(path)}")
        for v in scanner.scan():
            if v.rule in {"send-call", "smtp-capability", "click-send-button", "ui-send-button"}:
                pytest.fail(f"UI 层存在发送能力：{v}")


# ---------------------------------------------------------------------------
# 2) 隐私集成测试（app-core 模块存在时才有意义，否则 skip）
# ---------------------------------------------------------------------------

def _import_optional(dotted: str):
    try:
        return importlib.import_module(dotted)
    except Exception:  # noqa: BLE001 - 模块未实现/依赖缺失都视为跳过
        return None


def test_log_redaction_does_not_leak_secrets() -> None:
    """日志脱敏工具必须把敏感串替换掉（若 app-core 已提供 log_redaction）。"""
    mod = _import_optional("social_secretary.security.log_redaction")
    if mod is None:
        pytest.skip("app-core 的 log_redaction 尚未实现")
    redact = getattr(mod, "redact_text", None)
    if not callable(redact):
        pytest.skip("log_redaction 未提供 redact_text")
    secret = "SENSITIVE-AUTH-CODE-1234567890"
    rendered = str(redact(f"授权码：{secret} 消息正文不应出现在日志"))
    assert secret not in rendered, f"redact_text 未脱敏：{rendered!r}"


def test_credential_store_does_not_write_plaintext_files(tmp_path) -> None:
    """凭据存储接口若存在，必须支持内存实现且不依赖普通文件保存明文。"""
    mod = _import_optional("social_secretary.security.credential_store")
    if mod is None:
        pytest.skip("app-core 的 credential_store 尚未实现")
    in_memory = getattr(mod, "InMemoryCredentialStore", None)
    assert in_memory is not None, "security.credential_store 应提供 InMemoryCredentialStore 供测试注入"
    store = in_memory()
    key, secret = "mail:qq:test@example.com", "AUTH-CODE-PLAINTEXT-XYZ"
    store.set_secret(key, secret, label="测试")
    assert store.get_secret(key) == secret
    # 内存凭据存储不应在磁盘留下明文：检查模块源码不落文件即可（运行期不落盘由实现负责）
    module_file = Path(mod.__file__).resolve()
    source = module_file.read_text(encoding="utf-8-sig", errors="replace")
    assert "open(" not in source or "json.dump" not in source, "凭据存储疑似写普通文件"


def test_keyring_backend_used_for_real_store() -> None:
    """真实凭据存储必须基于 keyring（Windows 凭据管理器），不得自实现文件加密冒充。"""
    mod = _import_optional("social_secretary.security.credential_store")
    if mod is None:
        pytest.skip("app-core 的 credential_store 尚未实现")
    module_file = Path(mod.__file__).resolve()
    source = module_file.read_text(encoding="utf-8-sig", errors="replace")
    assert "keyring" in source, "真实凭据存储应使用 keyring（Windows 凭据管理器）"
