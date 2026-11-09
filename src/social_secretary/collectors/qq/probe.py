"""QQ NT 只读 UIA 结构探针。

用途：在 Lead 授权的真机窗口上枚举 QQ 进程/顶层窗口/UIA 控件树结构，验证
- QQ NT 9.9.35 是否暴露完整的 UIA 无障碍树；
- 主面板/聊天窗口/消息区/头部/气泡/时间/发送者等类名是否与选择器基线一致；
- 经典模式 vs 效率模式的窗口形态。

隐私与安全默认值：
- **默认输出完全脱敏**：只输出类名、控件类型、名称长度、节点数、矩形、标题长度；
  不输出窗口标题、联系人名、消息正文。``include_names=True`` 仅在 Lead 明确指定的
  测试会话使用，且必须显式确认。
- 默认不切前台、不点击、不滚动、不碰剪贴板、不读消息内容。
- ``activate=True`` 才会短暂占用前台（结束后恢复原前台窗口）；``launch_qq=True``
  才会在 QQ 未运行时启动 D:\\QQ.exe（默认不启动）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ...config import project_root
from . import nodes
from .backend import NativeBackend, UiaBackend, WindowInfo
from .errors import QqError
from .foreground import ForegroundGuard, activate_window
from .profile import (
    ModeSelectors,
    QqUiMode,
    QqUiProfile,
    VersionCheck,
    compile_pattern,
    detect_file_version,
)

PROBE_SCHEMA_VERSION = 1
PROBE_TOOL = "tools/qq_probe.py"

_LIMITATIONS = [
    "默认脱敏模式只证明 UIA 树结构与类名，不证明消息正文/时间/发送者能被正确解析。",
    "真实登录账号的端到端消息读取尚未验证（需要用户登录测试账号并在 Lead 授权下进行）。",
    "只有前台窗口才构建完整 UIA 树：QQ 在后台时本探针结构可能不完整（默认不切前台）。",
    "消息列表只暴露视口内消息；历史消息需要滚动后重读（本探针不滚动）。",
    "QQ 升级可能改变类名；本文件不能替代真机冒烟测试。",
]


class ProbeError(RuntimeError):
    code = "probe_error"


@dataclass(frozen=True)
class ProbeOptions:
    include_names: bool = False
    mode_filter: QqUiMode | None = None
    title_filter: str | None = None
    activate: bool = False
    launch_qq: bool = False
    launch_timeout_ms: int = 20_000
    max_depth: int = 24
    max_nodes: int = 6_000
    max_windows: int = 40


def make_backend() -> UiaBackend:
    """可被测试 monkeypatch 的后端工厂。"""

    return NativeBackend()


def default_evidence_dir() -> Path:
    return project_root() / "evidence" / "qq"


def default_output_path(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S")
    return default_evidence_dir() / f"qq_probe_{stamp}.json"


def _sha256_short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _safe_num(value: Any) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return 0


def _mode_from_root(root: nodes.UiaNode, profile: QqUiProfile) -> QqUiMode:
    toggle_re = compile_pattern(profile.mode_toggle.class_pattern)
    toggle = nodes.find_first(
        root, lambda node: bool(toggle_re.search(nodes.class_name(node))), max_nodes=8_000
    )
    if toggle is None:
        return QqUiMode.UNKNOWN
    name = nodes.node_name(toggle)
    if profile.mode_toggle.efficiency_hint in name:
        return QqUiMode.EFFICIENCY
    if profile.mode_toggle.classic_hint in name:
        return QqUiMode.CLASSIC
    return QqUiMode.UNKNOWN


def _window_role(window: WindowInfo, profile: QqUiProfile) -> str:
    if profile.panel_title_regex.match(window.title or ""):
        return "panel"
    if (window.title or "").strip():
        return "chat-candidate"
    return "aux"


def _scan_window_tree(
    root: nodes.UiaNode,
    *,
    profile: QqUiProfile,
    selectors: ModeSelectors,
    include_names: bool,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    """一次 DFS 同时产出结构条目、类名/类型计数与选择器命中统计。"""

    classic_selectors = profile.classic
    efficiency_selectors = profile.efficiency
    classic_bubble_re = compile_pattern(classic_selectors.bubble_class_pattern)
    classic_excludes = [compile_pattern(p) for p in classic_selectors.bubble_exclude_class_patterns]
    classic_self_re = compile_pattern(classic_selectors.self_class_pattern)
    efficiency_bubble_re = compile_pattern(efficiency_selectors.bubble_class_pattern)
    efficiency_excludes = [
        compile_pattern(p) for p in efficiency_selectors.bubble_exclude_class_patterns
    ]
    efficiency_self_re = compile_pattern(efficiency_selectors.self_class_pattern)
    header_re = compile_pattern(selectors.header_class_pattern)
    msg_area_re = compile_pattern(selectors.msg_area_class_pattern)
    editor_re = compile_pattern(selectors.editor_class_pattern)
    toggle_re = compile_pattern(profile.mode_toggle.class_pattern)
    exclude_edit_res = [compile_pattern(p) for p in selectors.search_box.exclude_class_patterns]
    time_res = [
        compile_pattern(p)
        for p in (
            *classic_selectors.time_class_patterns,
            *efficiency_selectors.time_class_patterns,
        )
    ]
    sender_res = [
        compile_pattern(p)
        for p in (
            *classic_selectors.sender_class_patterns,
            *efficiency_selectors.sender_class_patterns,
        )
    ]

    entries: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    features = {
        "header_count": 0,
        "msg_area_count": 0,
        "editor_count": 0,
        "search_box_count": 0,
        "edit_count": 0,
        "button_count": 0,
        "list_item_count": 0,
        "bubbles_classic": 0,
        "bubbles_efficiency": 0,
        "self_bubbles_classic": 0,
        "self_bubbles_efficiency": 0,
        "time_node_count": 0,
        "sender_node_count": 0,
        "mode_toggle_found": False,
    }
    truncated = False
    stack: list[tuple[nodes.UiaNode, int, str, bool]] = [(root, 0, "0", False)]
    visited = 0
    while stack:
        node, depth, path, parent_is_bubble = stack.pop()
        if visited >= max_nodes:
            truncated = True
            break
        visited += 1
        cls = nodes.class_name(node)
        ctype = nodes.control_type(node)
        name = nodes.node_name(node)
        rect = nodes.node_rect(node)
        child_list = nodes.safe_children(node) if depth < max_depth else []
        entry: dict[str, Any] = {
            "path": path,
            "depth": depth,
            "class": cls,
            "type": ctype,
            "name_len": len(name),
            "children": len(child_list),
            "offscreen": bool(node.is_offscreen),
            "rect": list(rect) if rect else None,
        }
        if include_names:
            entry["name"] = name
        entries.append(entry)
        class_counts[cls] += 1
        type_counts[ctype] += 1

        if header_re.search(cls):
            features["header_count"] += 1
        if msg_area_re.search(cls):
            features["msg_area_count"] += 1
        if editor_re.search(cls):
            features["editor_count"] += 1
        if toggle_re.search(cls):
            features["mode_toggle_found"] = True
        if ctype.lower() == "edit":
            features["edit_count"] += 1
            if not any(regex.search(cls) for regex in exclude_edit_res):
                features["search_box_count"] += 1
        if ctype.lower() == "button":
            features["button_count"] += 1
        if ctype.lower() == "listitem":
            features["list_item_count"] += 1
        if any(regex.search(cls) for regex in time_res):
            features["time_node_count"] += 1
        if any(regex.search(cls) for regex in sender_res):
            features["sender_node_count"] += 1

        is_classic_bubble = bool(classic_bubble_re.search(cls)) and not any(
            regex.search(cls) for regex in classic_excludes
        )
        is_efficiency_bubble = bool(efficiency_bubble_re.search(cls)) and not any(
            regex.search(cls) for regex in efficiency_excludes
        )
        if is_classic_bubble and not parent_is_bubble:
            features["bubbles_classic"] += 1
            if classic_self_re.search(cls):
                features["self_bubbles_classic"] += 1
        if is_efficiency_bubble and not parent_is_bubble:
            features["bubbles_efficiency"] += 1
            if efficiency_self_re.search(cls):
                features["self_bubbles_efficiency"] += 1
        next_parent_bubble = parent_is_bubble or is_classic_bubble or is_efficiency_bubble
        for index in range(len(child_list) - 1, -1, -1):
            stack.append((child_list[index], depth + 1, f"{path}.{index}", next_parent_bubble))

    top_classes = [
        {"class": cls, "count": count}
        for cls, count in class_counts.most_common(40)
        if cls
    ]
    top_types = [
        {"type": ctype, "count": count} for ctype, count in type_counts.most_common(20) if ctype
    ]
    return {
        "node_count": visited,
        "truncated": truncated,
        "max_depth": max_depth,
        "entries": entries,
        "class_name_counts": top_classes,
        "control_type_counts": top_types,
        "features": features,
    }


def _window_report(
    backend: UiaBackend,
    profile: QqUiProfile,
    window: WindowInfo,
    options: ProbeOptions,
    *,
    assumed_mode: QqUiMode,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "hwnd_present": bool(window.hwnd),
        "pid_present": bool(window.pid),
        "class_name": window.class_name,
        "title_length": len(window.title or ""),
        "title_sha256_12": _sha256_short(window.title or ""),
        "role": _window_role(window, profile),
        "rect": list(window.rect),
        "width": window.width,
        "height": window.height,
        "visible": window.visible,
        "minimized": window.minimized,
        "uia_root_ok": False,
    }
    if options.include_names:
        report["title"] = window.title
    root = backend.root_from_handle(window.hwnd)
    if root is None:
        report["uia_root_ok"] = False
        return report
    report["uia_root_ok"] = True
    detected_mode = _mode_from_root(root, profile)
    report["detected_mode"] = detected_mode.value
    effective_mode = options.mode_filter or (
        detected_mode if detected_mode != QqUiMode.UNKNOWN else assumed_mode
    )
    selectors = profile.mode_selectors(effective_mode)
    report["analysis_mode"] = effective_mode.value
    scan = _scan_window_tree(
        root,
        profile=profile,
        selectors=selectors,
        include_names=options.include_names,
        max_depth=options.max_depth,
        max_nodes=options.max_nodes,
    )
    report.update(scan)
    return report


def _aggregate_features(windows: Sequence[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "windows_scanned": 0,
        "uia_roots_ok": 0,
        "header_count": 0,
        "msg_area_count": 0,
        "editor_count": 0,
        "search_box_count": 0,
        "edit_count": 0,
        "button_count": 0,
        "list_item_count": 0,
        "bubbles_classic": 0,
        "bubbles_efficiency": 0,
        "self_bubbles_classic": 0,
        "self_bubbles_efficiency": 0,
        "time_node_count": 0,
        "sender_node_count": 0,
        "mode_toggle_found": 0,
        "node_count": 0,
    }
    for window in windows:
        totals["windows_scanned"] += 1
        if window.get("uia_root_ok"):
            totals["uia_roots_ok"] += 1
        totals["node_count"] += int(window.get("node_count") or 0)
        for key in (
            "header_count",
            "msg_area_count",
            "editor_count",
            "search_box_count",
            "edit_count",
            "button_count",
            "list_item_count",
            "bubbles_classic",
            "bubbles_efficiency",
            "self_bubbles_classic",
            "self_bubbles_efficiency",
            "time_node_count",
            "sender_node_count",
        ):
            features = window.get("features") or {}
            totals[key] += int(features.get(key) or 0)
        if (window.get("features") or {}).get("mode_toggle_found"):
            totals["mode_toggle_found"] += 1
    return totals


def _launch_qq(
    backend: UiaBackend, profile: QqUiProfile, timeout_ms: int
) -> dict[str, Any]:
    exe = Path(profile.exe_path)
    result: dict[str, Any] = {
        "attempted": True,
        "exe_exists": exe.is_file(),
        "launched": False,
        "error": None,
    }
    if not exe.is_file():
        result["error"] = "QQ 可执行文件不存在"
        return result
    try:
        subprocess.Popen(  # 启动进程不附带任何 UI 自动化/数据读取
            [str(exe)],
            cwd=str(exe.parent),
            close_fds=True,
        )
    except OSError as exc:
        result["error"] = f"启动 QQ 失败（{type(exc).__name__}）"
        return result
    poll_ms = max(250, int(profile.behavior.poll_interval_ms))
    attempts = max(1, int(timeout_ms) // poll_ms)
    for _ in range(attempts):
        if backend.list_processes(profile.process_names):
            result["launched"] = True
            return result
        backend.sleep(poll_ms / 1000.0)
    result["error"] = "已尝试启动 QQ，但在超时时间内未检测到 QQ 进程"
    return result


def collect_probe(
    backend: UiaBackend,
    profile: QqUiProfile,
    options: ProbeOptions | None = None,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """执行一次只读结构探测（不点击/不滚动/不碰剪贴板）。"""

    options = options or ProbeOptions()
    now = generated_at or datetime.now().astimezone()
    report: dict[str, Any] = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "tool": PROBE_TOOL,
        "generated_at": now.isoformat(),
        "desensitized": not options.include_names,
        "include_names": options.include_names,
        "mode_filter": options.mode_filter.value if options.mode_filter else None,
        "title_filter_applied": bool(options.title_filter),
        "activate_requested": options.activate,
        "launch_qq_requested": options.launch_qq,
        "environment": {
            "qq_exe_path_configured": bool(profile.exe_path),
            "qq_exe_exists": Path(profile.exe_path).is_file(),
            "profile_baseline_version": profile.baseline_version,
            "profile_baseline_date": profile.baseline_date,
            "profile_source": profile.source_path,
            "backend": backend.check_environment(),
        },
        "status": "unknown",
        "status_detail": "",
        "safety": {
            "send_capability": "none",
            "actions_taken": ["enumerate-processes", "enumerate-windows", "read-uia-tree-structure"],
            "clicked": False,
            "scrolled": False,
            "clipboard_touched": False,
            "message_text_read": False,
            "names_included": options.include_names,
            "launched_qq": False,
            "foreground_changed": False,
            "foreground_restored": True,
        },
        "notes": list(_LIMITATIONS),
        "processes": [],
        "windows": [],
        "aggregate": {},
        "version_check": {},
    }
    try:
        detected_version = detect_file_version(profile.exe_path)
        check: VersionCheck = profile.version_check(detected_version)
        report["version_check"] = check.to_dict()
        report["environment"]["qq_version_detected"] = detected_version

        processes = backend.list_processes(profile.process_names)
        if not processes and options.launch_qq:
            launch_info = _launch_qq(backend, profile, options.launch_timeout_ms)
            report["launch"] = launch_info
            report["safety"]["launched_qq"] = bool(launch_info.get("launched"))
            processes = backend.list_processes(profile.process_names)
        report["processes"] = [
            {"pid_present": bool(process.pid), "exe_name": process.name} for process in processes
        ]
        if not processes:
            report["status"] = "qq_not_running"
            report["status_detail"] = (
                "未检测到 QQ 进程。结构探测需要 Lead/用户授权后手动启动并登录 QQ；"
                "本探针默认不会启动 QQ（可用 --launch-qq，但会启动客户端）。"
            )
            return report

        windows = backend.list_top_windows(profile.process_names)
        windows = [window for window in windows if window.width > 0 and window.height > 0]
        windows.sort(key=lambda window: (_window_role(window, profile) == "panel", window.area), reverse=True)
        if options.title_filter:
            needle = options.title_filter.casefold()
            windows = [window for window in windows if needle in (window.title or "").casefold()]
            report["title_filter_matched_windows"] = len(windows)
        windows = windows[: max(1, int(options.max_windows))]
        if not windows:
            report["status"] = "qq_running_no_window"
            report["status_detail"] = (
                "检测到 QQ 进程，但没有找到可枚举的顶层窗口"
                "（可能 QQ 仍在启动、窗口隐藏或仅处于托盘状态）。"
            )
            return report

        assumed_mode = options.mode_filter or QqUiMode(profile.behavior.default_mode)
        guard: ForegroundGuard | None = None
        activated = False
        activated_hwnd = 0
        try:
            if options.activate:
                target = _pick_activation_window(windows, profile)
                if target is not None:
                    guard = ForegroundGuard(
                        backend, restore_attempts=profile.behavior.restore_attempts
                    )
                    guard.__enter__()
                    activated = activate_window(
                        backend,
                        target.hwnd,
                        attempts=profile.behavior.activate_attempts,
                        poll_interval=profile.behavior.poll_interval_ms / 1000.0,
                        guard=guard,
                    )
                    activated_hwnd = target.hwnd
                    backend.sleep(profile.behavior.revive_tree_wait_ms / 1000.0)
                    if activated:
                        refreshed = backend.list_top_windows(profile.process_names)
                        if options.title_filter:
                            needle = options.title_filter.casefold()
                            refreshed = [
                                window
                                for window in refreshed
                                if needle in (window.title or "").casefold()
                            ]
                        refreshed = [
                            window
                            for window in refreshed
                            if window.width > 0 and window.height > 0
                        ]
                        refreshed.sort(
                            key=lambda window: (
                                _window_role(window, profile) == "panel",
                                window.area,
                            ),
                            reverse=True,
                        )
                        windows = refreshed[: max(1, int(options.max_windows))]
            for window in windows:
                report["windows"].append(
                    _window_report(
                        backend,
                        profile,
                        window,
                        options,
                        assumed_mode=assumed_mode,
                    )
                )
        finally:
            if guard is not None:
                guard.__exit__(None, None, None)
                report["safety"]["foreground_changed"] = bool(guard.foreground_used or activated)
                report["safety"]["foreground_restored"] = bool(guard.restored)
                report["activate"] = {
                    "attempted": True,
                    "activated": activated,
                    "window_hwnd_present": bool(activated_hwnd),
                    "foreground_used": bool(guard.foreground_used or activated),
                    "foreground_restored": bool(guard.restored),
                }
        report["aggregate"] = _aggregate_features(report["windows"])
        roots_ok = report["aggregate"]["uia_roots_ok"]
        if roots_ok <= 0:
            report["status"] = "uia_tree_unavailable"
            report["status_detail"] = (
                "检测到 QQ 窗口，但无法读取任何 UIA 控件树。"
                "QQ NT 只为前台窗口构建完整树；可在授权后加 --activate 重试。"
            )
        elif report["aggregate"]["node_count"] < 30 and not options.activate:
            report["status"] = "ok_sparse_tree"
            report["status_detail"] = (
                "UIA 树节点很少（QQ 可能在后台，Electron 只给前台窗口构建完整树）。"
                "可在授权后加 --activate 重新探测。"
            )
        else:
            report["status"] = "ok"
            report["status_detail"] = (
                "已读取 QQ 窗口的 UIA 结构统计（默认脱敏：仅类名/控件类型/名称长度/节点数）。"
            )
        return report
    except QqError as exc:
        report["status"] = "error"
        report["status_detail"] = exc.message
        report["error"] = exc.to_dict()
        return report


def _pick_activation_window(windows: Sequence[WindowInfo], profile: QqUiProfile) -> WindowInfo | None:
    if not windows:
        return None
    ordered = sorted(
        windows,
        key=lambda window: (
            bool(profile.panel_title_regex.match(window.title or "")),
            not window.minimized,
            window.area,
        ),
        reverse=True,
    )
    return ordered[0] if ordered else None


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


__all__ = [
    "PROBE_SCHEMA_VERSION",
    "ProbeOptions",
    "ProbeError",
    "collect_probe",
    "default_evidence_dir",
    "default_output_path",
    "make_backend",
    "write_report",
]
