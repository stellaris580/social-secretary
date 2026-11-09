"""QQ NT 只读采集器：实现 ``contracts.MessageSource``。

只读能力边界（代码级约束）：
- 读进程/窗口/UIA 控件树/文本；
- 在 QQ 主面板**搜索框**写会话名 / 必要时临时借用剪贴板（用后恢复）；
- 在消息区滚动鼠标滚轮读历史；
- 短暂切前台并在结束后恢复原前台窗口。

**不存在**任何发送消息、点击发送按钮、向消息编辑框输入/粘贴文本的路径。
本采集器只读取用户白名单内的会话；打开会话后必须通过聊天头部的联系人名
校验，校验失败立即放弃读取（避免读错会话）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...contracts import (
    AccountKind,
    ConnectionCheck,
    Conversation,
    ConversationKind,
    RawMessage,
    TimestampSource,
)
from . import nodes
from .backend import (
    NativeBackend,
    UiaBackend,
    WindowInfo,
)
from .bubbles import (
    Bubble,
    MessageAccumulator,
    compute_content_hash,
    read_visible_bubbles,
)
from .errors import (
    QqClosed,
    QqError,
    QqForegroundNotAcknowledged,
    QqHeaderMismatch,
    QqHeaderUnavailable,
    QqNotRunning,
    QqOpenChatFailed,
    QqProfileError,
    QqSafetyViolation,
    QqTreeUnavailable,
    QqTreeNotReady,
    QqUnavailable,
    QqWhitelistViolation,
    QqWindowLost,
    QqWindowUnavailable,
)
from .foreground import ForegroundGuard, activate_window
from .profile import (
    ModeSelectors,
    QqUiMode,
    QqUiProfile,
    compile_pattern,
    detect_file_version,
    load_profile,
)
from .whitelist import TitleWhitelist, normalize_title

_MODE_LABELS = {
    QqUiMode.CLASSIC: "经典模式",
    QqUiMode.EFFICIENCY: "效率模式",
    QqUiMode.UNKNOWN: "未知模式",
}
_NODE_SCAN_LIMIT = 20_000


# ---------------------------------------------------------------------------
# 运行统计（用于证据/诊断；不含聊天正文与原昵称）
# ---------------------------------------------------------------------------

@dataclass
class QqRunStats:
    mode: str = QqUiMode.UNKNOWN.value
    panel_hwnd: int | None = None
    chat_hwnd: int | None = None
    embedded_chat: bool = False
    search_input_method: str = "none"  # none / value_pattern / clipboard
    clipboard_fallback_used: bool = False
    scroll_passes: int = 0
    scroll_notches_total: int = 0
    nodes_scanned: int = 0
    bubbles_seen: int = 0
    bubbles_kept: int = 0
    empty_text_dropped: int = 0
    duplicates_dropped: int = 0
    header_verified_passes: int = 0
    foreground_used: bool = False
    foreground_restored: bool = False
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "panel_hwnd": self.panel_hwnd,
            "chat_hwnd": self.chat_hwnd,
            "embedded_chat": self.embedded_chat,
            "search_input_method": self.search_input_method,
            "clipboard_fallback_used": self.clipboard_fallback_used,
            "scroll_passes": self.scroll_passes,
            "scroll_notches_total": self.scroll_notches_total,
            "nodes_scanned": self.nodes_scanned,
            "bubbles_seen": self.bubbles_seen,
            "bubbles_kept": self.bubbles_kept,
            "empty_text_dropped": self.empty_text_dropped,
            "duplicates_dropped": self.duplicates_dropped,
            "header_verified_passes": self.header_verified_passes,
            "foreground_used": self.foreground_used,
            "foreground_restored": self.foreground_restored,
            "warnings": list(self.warnings),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class ChatTarget:
    """已确认（至少标题层面）的聊天承载窗口。"""

    hwnd: int
    embedded: bool
    title: str
    window_title: str = ""


class QqMessageSource:
    """QQ NT 只读消息源（经典/效率双模式）。"""

    kind = AccountKind.QQ

    def __init__(
        self,
        *,
        backend: UiaBackend | None = None,
        profile: QqUiProfile | None = None,
        profile_path: str | None = None,
        allowlist: Iterable[str] = (),
        conversation_kinds: Mapping[str, ConversationKind] | None = None,
        account_id: str = "qq-local",
        self_label: str | None = None,
        mode_override: QqUiMode | str | None = None,
        warning_callback: Callable[[str], None] | None = None,
        open_chat_timeout_ms: int | None = None,
        scroll_notches: int | None = None,
        max_scroll_passes: int | None = None,
        max_messages: int | None = None,
        sleep_scale: float = 1.0,
    ) -> None:
        if profile is None:
            profile = load_profile(profile_path)
        self._profile: QqUiProfile = profile
        self._backend: UiaBackend | None = backend
        self._owns_backend: bool = backend is None
        self._warning_callback = warning_callback
        self._account_id = account_id
        self._self_label = self_label or profile.behavior.self_label
        self._unknown_sender_label = profile.behavior.unknown_sender_label
        self._sleep_scale = max(0.0, float(sleep_scale))
        self._open_chat_timeout_ms = int(
            open_chat_timeout_ms if open_chat_timeout_ms is not None else profile.behavior.open_chat_timeout_ms
        )
        self._scroll_notches = int(
            scroll_notches if scroll_notches is not None else profile.behavior.scroll_notches_per_pass
        )
        self._max_scroll_passes = int(
            max_scroll_passes if max_scroll_passes is not None else profile.behavior.max_scroll_passes
        )
        self._max_messages = int(
            max_messages if max_messages is not None else profile.behavior.max_messages_hard_cap
        )
        self._allowlist_entries: tuple[str, ...] = tuple(
            entry.strip() for entry in allowlist if entry and entry.strip()
        )
        self._whitelist = TitleWhitelist(self._allowlist_entries)
        self._conversation_kinds: dict[str, ConversationKind] = {
            normalize_title(key): value for key, value in (conversation_kinds or {}).items()
        }
        self._mode: QqUiMode = QqUiMode.UNKNOWN
        self._mode_override: QqUiMode | None = None
        if mode_override is not None:
            try:
                mode_value = (
                    mode_override.value
                    if isinstance(mode_override, QqUiMode)
                    else str(mode_override)
                )
                self._mode_override = QqUiMode(mode_value)
            except ValueError as exc:
                raise QqProfileError(f"未知的 QQ 界面模式：{mode_override}") from exc
        self._stats = QqRunStats()
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        backend = self._backend
        self._backend = None
        if self._owns_backend and backend is not None:
            try:
                backend.close()
            except Exception:  # pragma: no cover - 关闭失败不影响程序退出
                pass

    def __enter__(self) -> "QqMessageSource":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # noqa: ANN001
        self.close()

    @property
    def last_stats(self) -> QqRunStats:
        return self._stats

    @property
    def allowlist(self) -> tuple[str, ...]:
        return self._allowlist_entries

    @property
    def profile(self) -> QqUiProfile:
        return self._profile

    # ------------------------------------------------------------------
    # MessageSource: 连接检测 / 会话列表
    # ------------------------------------------------------------------
    def test_connection(self) -> ConnectionCheck:
        """非侵入式检测：只枚举进程/窗口/控件树，不切前台、不读消息。"""

        started = time.monotonic()
        discovered: dict[str, Any] = {
            "account_kind": self.kind.value,
            "profile_baseline_version": self._profile.baseline_version,
            "qq_exe": self._profile.exe_path,
        }
        try:
            backend = self._backend_or_create()
            processes = backend.list_processes(self._profile.process_names)
            discovered["process_count"] = len(processes)
            if not processes:
                return ConnectionCheck(
                    ok=False,
                    detail="未检测到 QQ 进程：请先手动启动并登录 QQ 后再试。",
                    latency_ms=_elapsed_ms(started),
                    discovered=discovered,
                )
            detected_version = detect_file_version(self._profile.exe_path)
            check = self._profile.version_check(detected_version)
            discovered.update(
                {
                    "qq_version_detected": detected_version,
                    "version_compatible": check.compatible,
                    "version_note": check.note,
                }
            )
            windows = backend.list_top_windows(self._profile.process_names)
            panel = self._pick_panel_window(backend, windows, require_search_box=False)
            if panel is None:
                return ConnectionCheck(
                    ok=False,
                    detail="检测到 QQ 进程，但未找到 QQ 主面板（可能未登录或主面板已隐藏）。"
                    "请手动打开 QQ 主面板后重试。",
                    latency_ms=_elapsed_ms(started),
                    discovered=discovered,
                )
            discovered["panel_found"] = True
            discovered["panel_hwnd"] = panel.hwnd
            discovered["panel_title_length"] = len(panel.title)
            root = backend.root_from_handle(panel.hwnd)
            node_count = 0
            search_box_found = False
            mode = QqUiMode.UNKNOWN
            if root is not None:
                descendants = list(nodes.iter_descendants(root, max_nodes=600))
                node_count = len(descendants) + 1
                search_box_found = self._find_search_box(root, self._selectors(QqUiMode.CLASSIC)) is not None
                mode = self._detect_mode_from_root(root)
            discovered["tree_node_count"] = node_count
            discovered["tree_dense"] = node_count >= 30 or search_box_found
            discovered["mode"] = mode.value
            self._warn_if_version_incompatible(check)
            detail = f"检测到 QQ 主面板（{_MODE_LABELS[mode]}）。"
            if not discovered["tree_dense"]:
                detail += "当前 QQ 可能不在前台，UIA 树不完整；实际采集时会短暂置前并自动恢复。"
            else:
                detail += "实际读取需要短暂占用前台窗口，读取后会自动恢复原前台窗口。"
            return ConnectionCheck(
                ok=True,
                detail=detail,
                latency_ms=_elapsed_ms(started),
                discovered=discovered,
            )
        except QqError as exc:
            discovered["error_code"] = exc.code
            return ConnectionCheck(
                ok=False,
                detail=exc.message,
                latency_ms=_elapsed_ms(started),
                discovered=discovered,
            )
        except Exception as exc:  # pragma: no cover - 防御性兜底
            discovered["error_code"] = "unexpected_error"
            return ConnectionCheck(
                ok=False,
                detail=f"QQ 环境检测失败：{type(exc).__name__}",
                latency_ms=_elapsed_ms(started),
                discovered=discovered,
            )

    def list_conversations(self) -> list[Conversation]:
        """返回白名单配置的会话（不访问 QQ、不读取任何真实会话）。"""

        result: list[Conversation] = []
        seen: set[str] = set()
        for title in self._allowlist_entries:
            key = normalize_title(title)
            if not key or key in seen:
                continue
            seen.add(key)
            kind = self._conversation_kinds.get(key, ConversationKind.PRIVATE)
            result.append(
                Conversation(
                    id=f"qq:{key}",
                    account_id=self._account_id,
                    kind=kind,
                    title=title,
                    external_key=title,
                )
            )
        return result

    # ------------------------------------------------------------------
    # MessageSource: 读取消息
    # ------------------------------------------------------------------
    def fetch_messages(
        self,
        conversation: Conversation,
        *,
        since: datetime | None = None,
        limit: int = 100,
        foreground_ack: bool = False,
    ) -> list[RawMessage]:
        """读取白名单会话的最近消息（只读）。

        失败策略：
        - 环境不满足（QQ 未运行/未找到主面板/未确认前台）-> 抛 QqError 子类，由核心层
          转为 ScanStatus.BLOCKED/FAILED；
        - 读取中途窗口消失/焦点被抢 -> 返回已读取到的部分消息，并通过 ``last_stats.warnings``
          与 ``warning_callback`` 暴露降级信息；
        - 头部联系人校验失败 -> 抛 QqHeaderMismatch，绝不读取错误会话。
        """

        self._stats = QqRunStats()
        started = time.monotonic()
        target = (conversation.external_key or conversation.title or "").strip()
        try:
            self._check_open()
            if not target:
                raise QqWhitelistViolation("目标会话标识为空，已拒绝读取。")
            if not self._whitelist.allows(target):
                raise QqWhitelistViolation(
                    f"会话「{target}」不在白名单内，已拒绝读取（只读采集只允许读取已授权会话）。"
                )
            if int(limit) <= 0:
                return []
            effective_limit = min(int(limit), self._max_messages)
            if not foreground_ack:
                raise QqForegroundNotAcknowledged()
            since_aware = _ensure_aware(since)
            backend = self._backend_or_create()
            processes = backend.list_processes(self._profile.process_names)
            if not processes:
                raise QqNotRunning()
            guard = ForegroundGuard(
                backend,
                warning_callback=self._warn,
                restore_attempts=self._profile.behavior.restore_attempts,
            )
            with guard:
                panel = self._ensure_panel(backend, guard)
                mode = self._detect_mode(backend, panel)
                self._mode = mode
                self._stats.mode = mode.value
                self._stats.panel_hwnd = panel.hwnd
                chat = self._open_chat(backend, guard, panel, mode, target)
                self._stats.chat_hwnd = chat.hwnd
                self._stats.embedded_chat = chat.embedded
                # QQ (Electron) 只在窗口前台时构建完整 UIA 树；树节点过少时绝不能
                # 当作「没有新消息」，而是明确报「树未就绪」让上层重试/提示用户。
                self._ensure_tree_ready(backend, guard, chat)
                self._validate_header(backend, chat, target)
                bubbles = self._read_history(
                    backend,
                    guard,
                    chat,
                    target,
                    conversation.kind,
                    effective_limit,
                    since_aware,
                )
                self._stats.bubbles_kept = len(bubbles)
            self._stats.foreground_used = guard.foreground_used
            self._stats.foreground_restored = guard.restored
            raw_messages = self._to_raw_messages(bubbles, target=target, mode=self._mode)
            if since_aware is not None:
                raw_messages = [
                    message
                    for message in raw_messages
                    if message.timestamp is None
                    or message.timestamp.astimezone() >= since_aware
                ]
            return raw_messages
        finally:
            self._stats.duration_ms = _elapsed_ms(started)

    # ------------------------------------------------------------------
    # 环境 / 白名单
    # ------------------------------------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise QqClosed()

    def _backend_or_create(self) -> UiaBackend:
        self._check_open()
        if self._backend is None:
            try:
                self._backend = NativeBackend(
                    wheel_interval=self._profile.behavior.wheel_interval_ms / 1000.0
                )
            except Exception as exc:  # pragma: no cover - 环境缺失
                raise QqUnavailable(f"UIA 自动化环境不可用：{exc}") from exc
        return self._backend

    def _warn(self, message: str) -> None:
        if message not in self._stats.warnings:
            self._stats.warnings.append(message)
        if self._warning_callback is not None:
            try:
                self._warning_callback(message)
            except Exception:  # pragma: no cover - 回调异常不影响采集
                pass

    def _warn_if_version_incompatible(self, check) -> None:  # noqa: ANN001
        if check.compatible is False:
            self._warn(check.note)

    def _sleep_ms(self, milliseconds: int) -> None:
        backend = self._backend
        if backend is None:
            return
        seconds = max(0.0, milliseconds / 1000.0) * self._sleep_scale
        if seconds > 0:
            backend.sleep(seconds)

    def _attempts(self, timeout_ms: int) -> int:
        poll = max(1, int(self._profile.behavior.poll_interval_ms))
        return max(1, int(timeout_ms) // poll + 1)

    def _selectors(self, mode: QqUiMode | None = None) -> ModeSelectors:
        return self._profile.mode_selectors(mode or self._mode)

    # ------------------------------------------------------------------
    # 主面板发现 / 模式识别
    # ------------------------------------------------------------------
    def _pick_panel_window(
        self,
        backend: UiaBackend,
        windows: Sequence[WindowInfo],
        *,
        require_search_box: bool,
    ) -> WindowInfo | None:
        regex = self._profile.panel_title_regex
        min_size = self._profile.windows.min_window_size
        candidates = [
            window
            for window in windows
            if window.visible
            and window.width >= min_size
            and window.height >= min_size
            and regex.match(window.title or "")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda window: (not window.minimized, window.area), reverse=True)
        if not require_search_box:
            return candidates[0]
        for candidate in candidates[:6]:
            root = backend.root_from_handle(candidate.hwnd)
            if root is None:
                continue
            if self._find_search_box(root, self._selectors(QqUiMode.CLASSIC)) is not None:
                return candidate
        return candidates[0]

    def _revive_candidates(self, windows: Sequence[WindowInfo]) -> list[WindowInfo]:
        min_size = self._profile.windows.min_window_size
        regex = self._profile.panel_title_regex
        usable = [
            window
            for window in windows
            if window.width >= min_size and window.height >= min_size
        ]
        usable.sort(
            key=lambda window: (
                bool(regex.match(window.title or "")),
                not window.minimized,
                window.area,
            ),
            reverse=True,
        )
        return usable[:6]

    def _panel_ready(self, backend: UiaBackend, hwnd: int) -> bool:
        root = backend.root_from_handle(hwnd)
        if root is None:
            return False
        return self._find_search_box(root, self._selectors(QqUiMode.CLASSIC)) is not None

    def _ensure_panel(self, backend: UiaBackend, guard: ForegroundGuard) -> WindowInfo:
        windows = backend.list_top_windows(self._profile.process_names)
        panel = self._pick_panel_window(backend, windows, require_search_box=True)
        if panel is not None and self._panel_ready(backend, panel.hwnd):
            return panel
        # 面板可能最小化/在后台（Electron 只给前台窗口构建完整 UIA 树）：
        # 依次尝试置前候选窗口，并轮询等待无障碍树重建（真机实测后台只有 1~3 个节点）。
        behavior = self._profile.behavior
        ready_attempts = max(1, int(behavior.panel_ready_attempts))
        per_attempt_wait = max(
            int(behavior.poll_interval_ms),
            int(behavior.revive_tree_wait_ms) // ready_attempts,
        )
        for window in self._revive_candidates(windows):
            if not activate_window(
                backend,
                window.hwnd,
                attempts=behavior.activate_attempts,
                poll_interval=behavior.poll_interval_ms / 1000.0,
                guard=guard,
            ):
                continue
            for _attempt in range(ready_attempts):
                self._sleep_ms(per_attempt_wait)
                refreshed = backend.list_top_windows(self._profile.process_names)
                panel = self._pick_panel_window(backend, refreshed, require_search_box=True)
                if panel is not None and self._panel_ready(backend, panel.hwnd):
                    return panel
        raise QqWindowUnavailable(
            "未找到 QQ 主面板搜索框：QQ 可能未登录、主面板已隐藏，或 UIA 无障碍树尚未就绪。"
            "请确认 QQ 已登录且主面板能正常打开后重试；若仍失败，请在 Lead 授权后运行 "
            "tools/qq_probe.py --activate 诊断结构。"
        )

    def _detect_mode_from_root(self, root: nodes.UiaNode) -> QqUiMode:
        toggle_re = compile_pattern(self._profile.mode_toggle.class_pattern)
        toggle = nodes.find_first(
            root,
            lambda node: bool(toggle_re.search(nodes.class_name(node))),
            max_nodes=8_000,
        )
        if toggle is not None:
            name = nodes.node_name(toggle)
            if self._profile.mode_toggle.efficiency_hint in name:
                return QqUiMode.EFFICIENCY
            if self._profile.mode_toggle.classic_hint in name:
                return QqUiMode.CLASSIC
        return QqUiMode.UNKNOWN

    def _detect_mode(self, backend: UiaBackend, panel: WindowInfo) -> QqUiMode:
        if self._mode_override is not None:
            return self._mode_override
        root = backend.root_from_handle(panel.hwnd)
        mode = self._detect_mode_from_root(root) if root is not None else QqUiMode.UNKNOWN
        if mode != QqUiMode.UNKNOWN:
            return mode
        fallback = QqUiMode(self._profile.behavior.default_mode)
        self._warn(
            f"未能识别 QQ 界面模式（未找到 narrow-toggler），暂按{_MODE_LABELS[fallback]}处理；"
            "如读取失败请在 Lead 授权后重新运行 qq_probe 校准选择器。"
        )
        return fallback

    # ------------------------------------------------------------------
    # 搜索框 / 打开会话
    # ------------------------------------------------------------------
    def _find_search_box(self, root: nodes.UiaNode, selectors: ModeSelectors) -> nodes.UiaNode | None:
        wanted = selectors.search_box.control_type.lower()
        exclude_res = [compile_pattern(pattern) for pattern in selectors.search_box.exclude_class_patterns]
        candidates: list[nodes.UiaNode] = []
        for node, _depth in nodes.iter_descendants(root, max_nodes=6_000):
            if nodes.control_type(node).lower() != wanted:
                continue
            cls = nodes.class_name(node)
            if any(regex.search(cls) for regex in exclude_res):
                continue
            candidates.append(node)
        if not candidates:
            return None
        if selectors.search_box.prefer_topmost:
            candidates.sort(key=lambda node: (nodes.node_rect(node) or (0, 1_000_000, 0, 0))[1])
        return candidates[0]

    def _assert_not_message_editor(self, node: nodes.UiaNode, selectors: ModeSelectors) -> None:
        editor_re = compile_pattern(selectors.editor_class_pattern)
        if editor_re.search(nodes.class_name(node)):
            raise QqSafetyViolation(
                "目标控件疑似 QQ 消息编辑框，已拒绝任何输入（只读采集绝不接触消息编辑框）。"
            )

    def _write_search_query(
        self, backend: UiaBackend, search_box: nodes.UiaNode, selectors: ModeSelectors, target: str
    ) -> None:
        self._assert_not_message_editor(search_box, selectors)
        if search_box.set_value(target):
            self._stats.search_input_method = "value_pattern"
            return
        if not self._profile.behavior.clipboard_fallback_enabled:
            raise QqOpenChatFailed(
                "搜索框不支持 ValuePattern 写入，且配置禁用了剪贴板回退，无法打开会话语。"
            )
        # 剪贴板回退前必须做焦点安全校验：焦点必须就在这个搜索框上，
        # 且不能是消息编辑框，否则一个按键就可能把文字打进聊天输入框。
        if not search_box.set_focus():
            raise QqSafetyViolation("无法聚焦 QQ 主面板搜索框，已放弃剪贴板输入。")
        self._sleep_ms(120)
        focused = backend.focused_element()
        if not nodes.same_element(focused, search_box):
            raise QqSafetyViolation(
                "搜索框焦点校验失败（焦点不在搜索框），已放弃剪贴板输入以避免误输入消息编辑框。"
            )
        assert focused is not None
        self._assert_not_message_editor(focused, selectors)
        backend.paste_text_into_focused_control(target)
        self._stats.search_input_method = "clipboard"
        self._stats.clipboard_fallback_used = True
        self._warn(
            "搜索框不支持 ValuePattern，已临时使用系统剪贴板输入会话名；"
            "原剪贴板内容已自动恢复。"
        )

    def _find_search_results(
        self, root: nodes.UiaNode, target: str, selectors: ModeSelectors
    ) -> list[nodes.UiaNode]:
        accepted_types = {value.lower() for value in selectors.search_result.control_types}
        class_res = [compile_pattern(pattern) for pattern in selectors.search_result.class_patterns]
        target_norm = normalize_title(target)
        if not target_norm:
            return []
        exact: list[nodes.UiaNode] = []
        loose: list[nodes.UiaNode] = []
        for node, _depth in nodes.iter_descendants(root, max_nodes=8_000):
            ctype = nodes.control_type(node).lower()
            cls = nodes.class_name(node)
            type_ok = ctype in accepted_types or any(regex.search(cls) for regex in class_res)
            if not type_ok:
                continue
            name_norm = normalize_title(nodes.node_name(node))
            if not name_norm:
                blob = " ".join(nodes.descendant_texts(node, control_types=("Text",)))
                name_norm = normalize_title(blob)
            if not name_norm:
                continue
            if name_norm == target_norm:
                exact.append(node)
            elif target_norm in name_norm:
                loose.append(node)
        return exact + loose

    def _wait_search_result(
        self,
        backend: UiaBackend,
        panel_hwnd: int,
        target: str,
        selectors: ModeSelectors,
    ) -> nodes.UiaNode | None:
        for _attempt in range(self._attempts(self._profile.behavior.search_result_timeout_ms)):
            root = backend.root_from_handle(panel_hwnd)
            if root is not None:
                results = self._find_search_results(root, target, selectors)
                if results:
                    return results[0]
            self._sleep_ms(self._profile.behavior.poll_interval_ms)
        return None

    def _find_active_chat(
        self,
        backend: UiaBackend,
        panel: WindowInfo,
        mode: QqUiMode,
        target: str,
    ) -> ChatTarget | None:
        selectors = self._selectors(mode)
        if mode == QqUiMode.EFFICIENCY:
            root = backend.root_from_handle(panel.hwnd)
            if root is not None:
                header = self._find_header(root, selectors)
                if header is not None and self._header_matches(header, target):
                    return ChatTarget(hwnd=panel.hwnd, embedded=True, title=target)
            return None
        min_size = self._profile.windows.min_window_size
        for window in backend.list_top_windows(self._profile.process_names):
            if window.hwnd == panel.hwnd:
                continue
            if not window.visible or window.width < min_size or window.height < min_size:
                continue
            root = backend.root_from_handle(window.hwnd)
            if root is None:
                continue
            header = self._find_header(root, selectors)
            if header is not None and self._header_matches(header, target):
                return ChatTarget(
                    hwnd=window.hwnd,
                    embedded=False,
                    title=target,
                    window_title=window.title,
                )
        return None

    def _wait_chat_open(
        self,
        backend: UiaBackend,
        panel: WindowInfo,
        mode: QqUiMode,
        target: str,
    ) -> ChatTarget | None:
        attempts = self._attempts(self._open_chat_timeout_ms)
        for _attempt in range(attempts):
            chat = self._find_active_chat(backend, panel, mode, target)
            if chat is not None:
                return chat
            self._sleep_ms(self._profile.behavior.poll_interval_ms)
        return None

    def _conversation_item_title(self, item: nodes.UiaNode) -> str:
        """读取会话列表项标题：recent-contact-item > item__info > 第一个 Text。"""
        try:
            for info, _depth in nodes.iter_descendants(item, max_nodes=80):
                if nodes.class_name(info) != "item__info":
                    continue
                for text, _text_depth in nodes.iter_descendants(info, max_nodes=40):
                    if nodes.control_type(text).lower() != "text":
                        continue
                    title = (nodes.node_name(text) or "").strip()
                    if title:
                        return title
        except Exception:  # noqa: BLE001 - 结构异常按找不到处理
            return ""
        return ""

    def _open_from_conversation_list(
        self,
        backend: UiaBackend,
        panel: WindowInfo,
        mode: QqUiMode,
        target: str,
    ) -> ChatTarget | None:
        """从可见会话列表直接点击匹配标题的会话项（比搜索更可靠，不触碰搜索/编辑框）。

        只允许点击 ``recent-contact-item`` Group；点击后必须通过聊天头部校验。
        找不到或点击后标题不匹配时返回 None，由调用方回退到搜索打开。
        """
        root = backend.root_from_handle(panel.hwnd)
        if root is None:
            return None
        target_norm = normalize_title(target)
        if not target_norm:
            return None
        for item, _depth in nodes.iter_descendants(root, max_nodes=10_000):
            if nodes.class_name(item) != "recent-contact-item":
                continue
            if nodes.control_type(item).lower() != "group":
                continue
            title = self._conversation_item_title(item)
            if not title or normalize_title(title) != target_norm:
                continue
            click = getattr(item, "click", None)
            if not callable(click):
                continue
            if not click():
                continue
            chat = self._wait_chat_open(backend, panel, mode, target)
            if chat is not None:
                return chat
        return None

    def _open_chat(
        self,
        backend: UiaBackend,
        guard: ForegroundGuard,
        panel: WindowInfo,
        mode: QqUiMode,
        target: str,
    ) -> ChatTarget:
        selectors = self._selectors(mode)
        active = self._find_active_chat(backend, panel, mode, target)
        if active is not None:
            if not activate_window(
                backend,
                active.hwnd,
                attempts=self._profile.behavior.activate_attempts,
                poll_interval=self._profile.behavior.poll_interval_ms / 1000.0,
                guard=guard,
            ):
                raise QqWindowUnavailable("无法将 QQ 会话窗口置前，请确认 QQ 未被最小化或锁定。")
            self._sleep_ms(self._profile.behavior.revive_tree_wait_ms)
            header = self._locate_header(backend, active.hwnd, selectors)
            if header is not None and self._header_matches(header, target):
                return active
            # 置前后仍读不到头部：继续走「搜索打开」流程（不读取任何内容）。
        if not activate_window(
            backend,
            panel.hwnd,
            attempts=self._profile.behavior.activate_attempts,
            poll_interval=self._profile.behavior.poll_interval_ms / 1000.0,
            guard=guard,
        ):
            raise QqWindowUnavailable("无法将 QQ 主面板置前，已取消读取。")
        self._sleep_ms(self._profile.behavior.revive_tree_wait_ms)
        root = backend.root_from_handle(panel.hwnd)
        if root is None:
            raise QqTreeUnavailable("无法读取 QQ 主面板的 UIA 控件树。")
        # 优先从可见会话列表直接打开（失败再回退搜索；不向消息编辑框输入）。
        list_chat = self._open_from_conversation_list(backend, panel, mode, target)
        if list_chat is not None:
            if not activate_window(
                backend,
                list_chat.hwnd,
                attempts=self._profile.behavior.activate_attempts,
                poll_interval=self._profile.behavior.poll_interval_ms / 1000.0,
                guard=guard,
            ):
                raise QqWindowUnavailable("无法将 QQ 会话窗口置前，请确认 QQ 未被最小化或锁定。")
            self._sleep_ms(self._profile.behavior.revive_tree_wait_ms)
            return list_chat
        search_box = self._find_search_box(root, selectors)
        if search_box is None:
            raise QqWindowUnavailable(
                "未找到 QQ 主面板搜索框（UIA 树不完整、QQ 可能未登录或无障碍树未就绪）。"
                "已取消打开会话，请重试或运行 qq_probe --activate 诊断。"
            )
        self._write_search_query(backend, search_box, selectors, target)
        result = self._wait_search_result(backend, panel.hwnd, target, selectors)
        if result is None:
            raise QqOpenChatFailed(
                f"在 QQ 搜索中找不到会话「{target}」，请确认名称正确且该会话出现在最近会话或搜索结果中。"
            )
        accepted_types = {value.lower() for value in selectors.search_result.control_types}
        if nodes.control_type(result).lower() not in accepted_types:
            raise QqSafetyViolation("搜索结果控件类型异常，已拒绝点击（避免误点按钮）。")
        result.click()
        chat = self._wait_chat_open(backend, panel, mode, target)
        if chat is None:
            raise QqOpenChatFailed(
                f"已点击搜索结果的会话「{target}」，但未能在超时时间内读到该会话的聊天头部。"
                "可能原因：QQ 窗口未在前台导致 UIA 树未构建、会话仍在加载、或被其他窗口抢占焦点。"
                "请重试；若持续失败请在 Lead 授权后运行 qq_probe 校准。"
            )
        if not activate_window(
            backend,
            chat.hwnd,
            attempts=self._profile.behavior.activate_attempts,
            poll_interval=self._profile.behavior.poll_interval_ms / 1000.0,
            guard=guard,
        ):
            raise QqWindowUnavailable("无法将 QQ 会话窗口置前，已取消读取。")
        self._sleep_ms(self._profile.behavior.revive_tree_wait_ms)
        return chat

    # ------------------------------------------------------------------
    # 头部联系人校验
    # ------------------------------------------------------------------
    def _find_header(self, root: nodes.UiaNode, selectors: ModeSelectors) -> nodes.UiaNode | None:
        header_re = compile_pattern(selectors.header_class_pattern)
        return nodes.find_first(
            root,
            lambda node: bool(header_re.search(nodes.class_name(node))),
            max_nodes=10_000,
        )

    def _locate_header(
        self, backend: UiaBackend, hwnd: int, selectors: ModeSelectors
    ) -> nodes.UiaNode | None:
        root = backend.root_from_handle(hwnd)
        if root is None:
            return None
        return self._find_header(root, selectors)

    def _header_matches(self, header: nodes.UiaNode, target: str) -> bool:
        header_norm = normalize_title(nodes.node_name(header))
        target_norm = normalize_title(target)
        return bool(header_norm) and bool(target_norm) and header_norm == target_norm

    def _tree_node_count(self, backend: UiaBackend, hwnd: int) -> int:
        root = backend.root_from_handle(hwnd)
        if root is None:
            return 0
        return 1 + sum(1 for _node in nodes.iter_descendants(root, max_nodes=600))

    def _ensure_tree_ready(
        self, backend: UiaBackend, guard: ForegroundGuard, chat: ChatTarget
    ) -> None:
        """确认 UIA 树已构建（Electron 后台时只有 1~3 个节点）。

        节点数不足时重试「置前 + 等树重建」；仍然不足就抛 ``QqTreeNotReady``，
        绝不把空/稀疏的树当成「没有新消息」。
        """

        behavior = self._profile.behavior
        minimum = max(1, int(behavior.min_tree_nodes))
        attempts = max(1, int(behavior.tree_ready_attempts))
        node_count = 0
        for attempt in range(attempts):
            node_count = self._tree_node_count(backend, chat.hwnd)
            if node_count >= minimum:
                return
            if attempt >= attempts - 1:
                break
            activate_window(
                backend,
                chat.hwnd,
                attempts=behavior.activate_attempts,
                poll_interval=behavior.poll_interval_ms / 1000.0,
                guard=guard,
            )
            self._sleep_ms(max(behavior.poll_interval_ms, behavior.revive_tree_wait_ms // 2))
        raise QqTreeNotReady(
            detail={
                "node_count": node_count,
                "min_tree_nodes": minimum,
                "mode": self._mode.value,
                "attempts": attempts,
            }
        )

    def _validate_header(self, backend: UiaBackend, chat: ChatTarget, target: str) -> None:
        selectors = self._selectors(self._mode)
        root = backend.root_from_handle(chat.hwnd)
        if root is None:
            raise QqTreeUnavailable("无法读取 QQ 会话窗口的 UIA 控件树，已取消读取。")
        header = self._find_header(root, selectors)
        if header is None:
            raise QqHeaderUnavailable()
        if not self._header_matches(header, target):
            raise QqHeaderMismatch(
                f"当前打开的会话头部与目标会话「{target}」不一致，已取消读取（避免读错会话）。",
                detail={
                    "header_name_length": len(nodes.node_name(header)),
                    "target_length": len(target),
                },
            )
        self._stats.header_verified_passes += 1

    # ------------------------------------------------------------------
    # 历史消息读取（滚动 + 去重）
    # ------------------------------------------------------------------
    def _find_msg_area(self, root: nodes.UiaNode, selectors: ModeSelectors) -> nodes.UiaNode | None:
        area_re = compile_pattern(selectors.msg_area_class_pattern)
        return nodes.find_first(
            root,
            lambda node: bool(area_re.search(nodes.class_name(node))),
            max_nodes=10_000,
        )

    def _read_history(
        self,
        backend: UiaBackend,
        guard: ForegroundGuard,
        chat: ChatTarget,
        target: str,
        conversation_kind: ConversationKind,
        limit: int,
        since: datetime | None,
    ) -> list[Bubble]:
        selectors = self._selectors(self._mode)
        behavior = self._profile.behavior
        accumulator = MessageAccumulator(conversation_key=target, limit=limit)
        scrolled_total = 0
        stale_passes = 0
        first_pass = True
        for pass_index in range(self._max_scroll_passes + 1):
            if pass_index > 0:
                if backend.foreground_hwnd() != chat.hwnd:
                    self._warn("检测到 QQ 前台被抢占，正在重新置前以继续读取。")
                    if not activate_window(
                        backend,
                        chat.hwnd,
                        attempts=behavior.activate_attempts,
                        poll_interval=behavior.poll_interval_ms / 1000.0,
                        guard=guard,
                    ):
                        self._warn("无法恢复 QQ 前台，已停止继续读取历史消息。")
                        break
                root = backend.root_from_handle(chat.hwnd)
                if root is None:
                    self._warn("读取过程中 QQ 窗口/UIA 树消失，已停止并返回已读取内容。")
                    break
                header = self._find_header(root, selectors)
                if header is None:
                    self._warn("聊天头部控件消失（UIA 树不完整），已停止继续读取。")
                    break
                if not self._header_matches(header, target):
                    self._warn("检测到当前会话已切换，已停止读取（避免读错会话）。")
                    break
                self._stats.header_verified_passes += 1
            else:
                root = backend.root_from_handle(chat.hwnd)
                if root is None:
                    raise QqWindowLost()
            snapshot = read_visible_bubbles(
                root,
                selectors,
                conversation_title=target,
                conversation_kind=conversation_kind,
                self_label=self._self_label,
                unknown_sender_label=self._unknown_sender_label,
                now=backend.now(),
                max_nodes=_NODE_SCAN_LIMIT,
            )
            self._stats.nodes_scanned += snapshot.nodes_scanned
            self._stats.bubbles_seen += snapshot.bubble_nodes_seen
            self._stats.empty_text_dropped += snapshot.empty_text_dropped
            self._stats.duplicates_dropped += snapshot.duplicates_dropped
            if not snapshot.msg_area_found:
                if pass_index == 0:
                    # 树已过 _ensure_tree_ready 检查却找不到消息区：说明结构未就绪，
                    # 绝不能把空树当成「没有新消息」。
                    raise QqTreeNotReady(
                        "在 QQ 会话窗口中找到了聊天头部，但未找到消息区（chat-msg-area）。"
                        "UIA 树可能尚未就绪或 QQ 版本选择器已变化；本次读取不会被当作“没有新消息”。"
                        "请重试，若持续失败请在 Lead 授权后运行 qq_probe 校准。"
                    )
                self._warn(
                    "读取历史时消息区消失（UIA 树重建），已停止继续读取并返回已读取内容。"
                )
                break
            new_bubbles = accumulator.add_visible(snapshot.bubbles, scrolled_up=not first_pass)
            first_pass = False
            self._stats.scroll_passes = accumulator.passes
            self._stats.duplicates_dropped = (
                snapshot.duplicates_dropped + accumulator.duplicates_dropped
            )
            if since is not None and new_bubbles and all(
                bubble.timestamp is not None and bubble.timestamp < since for bubble in new_bubbles
            ):
                break
            if len(accumulator.messages) >= limit:
                break
            if pass_index >= self._max_scroll_passes:
                break
            if not new_bubbles:
                stale_passes += 1
                if stale_passes >= behavior.max_stale_passes:
                    break
            else:
                stale_passes = 0
            center = nodes.rect_center(snapshot.msg_area_rect)
            if center is None:
                self._warn("消息区没有可滚动矩形区域，无法读取更早历史。")
                break
            backend.wheel_at(center[0], center[1], self._scroll_notches)
            scrolled_total += self._scroll_notches
            self._stats.scroll_notches_total = scrolled_total
            self._sleep_ms(behavior.scroll_wait_ms)
        if scrolled_total:
            self._scroll_back_to_bottom(backend, chat, selectors, scrolled_total, behavior)
        return accumulator.latest(limit)

    def _scroll_back_to_bottom(
        self,
        backend: UiaBackend,
        chat: ChatTarget,
        selectors: ModeSelectors,
        scrolled_total: int,
        behavior,
    ) -> None:  # noqa: ANN001
        root = backend.root_from_handle(chat.hwnd)
        if root is None:
            return
        area = self._find_msg_area(root, selectors)
        center = nodes.rect_center(nodes.node_rect(area)) if area is not None else None
        if center is None:
            return
        try:
            backend.wheel_at(center[0], center[1], -abs(scrolled_total))
            self._sleep_ms(behavior.scroll_wait_ms)
        except Exception:  # pragma: no cover - 回滚失败不影响已读数据
            self._warn("读取结束后未能自动把消息列表滚回底部，请手动滚动聊天窗口。")

    # ------------------------------------------------------------------
    # 转换输出
    # ------------------------------------------------------------------
    def _to_raw_messages(
        self, bubbles: Sequence[Bubble], *, target: str, mode: QqUiMode
    ) -> list[RawMessage]:
        messages: list[RawMessage] = []
        for bubble in bubbles:
            content_hash = compute_content_hash(
                conversation_key=target,
                sender=bubble.sender,
                is_self=bubble.is_self,
                text=bubble.text,
                timestamp=bubble.timestamp,
            )
            extras: dict[str, Any] = {
                "bubble_class": bubble.class_name,
                "sender_resolved": bubble.sender_resolved,
                "content_hash": content_hash,
                "ui_mode": mode.value,
                "parser": "qq_uia",
            }
            messages.append(
                RawMessage(
                    conversation_key=target,
                    sender=bubble.sender,
                    is_self=bubble.is_self,
                    text=bubble.text,
                    timestamp=bubble.timestamp,
                    timestamp_source=bubble.timestamp_source
                    if bubble.timestamp is not None
                    else TimestampSource.COLLECTED,
                    raw_time_text=bubble.raw_time_text,
                    external_id=f"qq:{content_hash[:24]}:{bubble.index + 1}",
                    source_locator=f"QQ UIA · {_MODE_LABELS[mode]} · 第 {bubble.index + 1} 条可见气泡",
                    extras=extras,
                )
            )
        return messages


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["QqMessageSource", "QqRunStats", "ChatTarget"]
