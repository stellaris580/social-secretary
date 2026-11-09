"""QQ UIA 选择器配置（config/qq_ui_profile.json）加载与版本校准。

设计目标：
- 选择器集中在一个 JSON 里，QQ 升级后只改配置 + 重跑探针，不用改代码。
- 配置缺失/格式错误时给出明确中文提示（而不是静默用错选择器）。
- 版本校准只「警告」不「硬阻断」：同主/次版本视为兼容，差异过大提示重新校准。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...config import project_root
from .errors import QqProfileError


class QqUiMode(str, Enum):
    CLASSIC = "classic"
    EFFICIENCY = "efficiency"
    UNKNOWN = "unknown"


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise QqProfileError(f"QQ UI 配置缺少字段 {where}.{key}，请检查 config/qq_ui_profile.json")
    return mapping[key]


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise QqProfileError(f"QQ UI 配置字段 {where} 必须是字符串或字符串数组")
    return tuple(str(item) for item in value)


def _opt_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


@lru_cache(maxsize=256)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """缓存编译后的正则（配置里的选择器模式都会被反复使用）。"""

    try:
        return re.compile(pattern)
    except re.error as exc:  # pragma: no cover - 配置错误路径
        raise QqProfileError(f"QQ UI 选择器正则无效：{pattern!r}（{exc}）") from exc


@dataclass(frozen=True)
class SearchBoxSpec:
    control_type: str = "Edit"
    exclude_class_patterns: tuple[str, ...] = ("qq-msg-editor", "ExEditor", "ProseMirror")
    prefer_topmost: bool = True


@dataclass(frozen=True)
class SearchResultSpec:
    control_types: tuple[str, ...] = ("ListItem",)
    class_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModeSelectors:
    """单一界面模式（经典/效率）的消息区选择器。"""

    bubble_class_pattern: str
    bubble_exclude_class_patterns: tuple[str, ...]
    self_class_pattern: str
    text_control_types: tuple[str, ...]
    sender_class_patterns: tuple[str, ...]
    time_class_patterns: tuple[str, ...]
    msg_area_class_pattern: str
    header_class_pattern: str
    editor_class_pattern: str
    search_box: SearchBoxSpec
    search_result: SearchResultSpec
    text_join: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], mode: str) -> "ModeSelectors":
        where = f"modes.{mode}"
        if not isinstance(data, dict):
            raise QqProfileError(f"QQ UI 配置字段 {where} 必须是对象")
        search_box = _require(data, "search_box", where)
        search_result = _require(data, "search_result", where)
        if not isinstance(search_box, dict) or not isinstance(search_result, dict):
            raise QqProfileError(f"QQ UI 配置字段 {where}.search_box/search_result 必须是对象")
        return cls(
            bubble_class_pattern=str(_require(data, "bubble_class_pattern", where)),
            bubble_exclude_class_patterns=_opt_str_tuple(
                _require(data, "bubble_exclude_class_patterns", where)
            ),
            self_class_pattern=str(_require(data, "self_class_pattern", where)),
            text_control_types=_str_tuple(_require(data, "text_control_types", where), f"{where}.text_control_types"),
            sender_class_patterns=_opt_str_tuple(_require(data, "sender_class_patterns", where)),
            time_class_patterns=_opt_str_tuple(_require(data, "time_class_patterns", where)),
            msg_area_class_pattern=str(_require(data, "msg_area_class_pattern", where)),
            header_class_pattern=str(_require(data, "header_class_pattern", where)),
            editor_class_pattern=str(_require(data, "editor_class_pattern", where)),
            search_box=SearchBoxSpec(
                control_type=str(search_box.get("control_type", "Edit")),
                exclude_class_patterns=_opt_str_tuple(search_box.get("exclude_class_patterns")),
                prefer_topmost=bool(search_box.get("prefer_topmost", True)),
            ),
            search_result=SearchResultSpec(
                control_types=_str_tuple(
                    search_result.get("control_types", ["ListItem"]), f"{where}.search_result.control_types"
                ),
                class_patterns=_opt_str_tuple(search_result.get("class_patterns")),
            ),
            text_join=str(data.get("text_join", "")),
        )


@dataclass(frozen=True)
class WindowSpec:
    panel_title_pattern: str = "^QQ$"
    min_window_size: int = 60
    max_scan_windows: int = 40


@dataclass(frozen=True)
class ModeToggleSpec:
    class_pattern: str = "narrow-toggler"
    efficiency_hint: str = "经典"
    classic_hint: str = "效率"


@dataclass(frozen=True)
class BehaviorSpec:
    foreground_ack_required: bool = True
    default_mode: str = "classic"
    self_label: str = "我"
    unknown_sender_label: str = "未知成员"
    clipboard_fallback_enabled: bool = True
    scroll_notches_per_pass: int = 3
    scroll_wait_ms: int = 450
    max_scroll_passes: int = 40
    max_stale_passes: int = 2
    max_messages_hard_cap: int = 500
    open_chat_timeout_ms: int = 8000
    search_result_timeout_ms: int = 4000
    poll_interval_ms: int = 250
    revive_tree_wait_ms: int = 800
    activate_attempts: int = 4
    restore_attempts: int = 3
    wheel_interval_ms: int = 60
    text_time_future_tolerance_seconds: int = 120
    active_chat_header_wait_attempts: int = 8
    min_tree_nodes: int = 8
    tree_ready_attempts: int = 6
    panel_ready_attempts: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BehaviorSpec":
        defaults = cls()
        if not isinstance(data, dict):
            raise QqProfileError("QQ UI 配置字段 behavior 必须是对象")
        kwargs: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            default = getattr(defaults, name)
            value = data.get(name, default)
            if isinstance(default, bool):
                kwargs[name] = bool(value)
            elif isinstance(default, int):
                try:
                    kwargs[name] = int(value)
                except (TypeError, ValueError) as exc:
                    raise QqProfileError(f"QQ UI 配置字段 behavior.{name} 必须是整数") from exc
            else:
                kwargs[name] = str(value)
        return cls(**kwargs)


@dataclass(frozen=True)
class QqUiProfile:
    schema_version: int
    process_names: tuple[str, ...]
    exe_path: str
    baseline_version: str
    baseline_date: str
    windows: WindowSpec
    mode_toggle: ModeToggleSpec
    classic: ModeSelectors
    efficiency: ModeSelectors
    behavior: BehaviorSpec
    source_path: str = ""
    # 探针参数与安全声明（仅展示/记录，采集器不依赖）
    probe: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)

    def mode_selectors(self, mode: QqUiMode) -> ModeSelectors:
        if mode == QqUiMode.EFFICIENCY:
            return self.efficiency
        return self.classic

    @property
    def panel_title_regex(self) -> re.Pattern[str]:
        return compile_pattern(self.windows.panel_title_pattern)

    def version_check(self, detected: str | None) -> "VersionCheck":
        return version_check(detected, self.baseline_version)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source_path: str = "") -> "QqUiProfile":
        if not isinstance(data, dict):
            raise QqProfileError("QQ UI 配置文件顶层必须是 JSON 对象")
        qq = _require(data, "qq", "root")
        windows = _require(data, "windows", "root")
        mode_toggle = _require(data, "mode_toggle", "root")
        modes = _require(data, "modes", "root")
        behavior = _require(data, "behavior", "root")
        if not isinstance(qq, dict) or not isinstance(windows, dict) or not isinstance(modes, dict):
            raise QqProfileError("QQ UI 配置文件的 qq/windows/modes 必须是对象")
        classic = _require(modes, "classic", "modes")
        efficiency = _require(modes, "efficiency", "modes")
        mode_toggle_spec = ModeToggleSpec(
            class_pattern=str(mode_toggle.get("class_pattern", "narrow-toggler")),
            efficiency_hint=str(mode_toggle.get("efficiency_hint", "经典")),
            classic_hint=str(mode_toggle.get("classic_hint", "效率")),
        )
        window_spec = WindowSpec(
            panel_title_pattern=str(windows.get("panel_title_pattern", "^QQ$")),
            min_window_size=int(windows.get("min_window_size", 60)),
            max_scan_windows=int(windows.get("max_scan_windows", 40)),
        )
        profile = cls(
            schema_version=int(data.get("schema_version", 1)),
            process_names=_str_tuple(qq.get("process_names", ["QQ"]), "qq.process_names"),
            exe_path=str(qq.get("exe_path", r"D:\path\to\QQ.exe")),
            baseline_version=str(qq.get("baseline_version", "0.0.0")),
            baseline_date=str(qq.get("baseline_date", "")),
            windows=window_spec,
            mode_toggle=mode_toggle_spec,
            classic=ModeSelectors.from_dict(classic, "classic"),
            efficiency=ModeSelectors.from_dict(efficiency, "efficiency"),
            behavior=BehaviorSpec.from_dict(behavior),
            source_path=source_path,
            probe=dict(data.get("probe") or {}),
            safety=dict(data.get("safety") or {}),
        )
        # 提前编译关键正则，配置错误在加载阶段就暴露。
        compile_pattern(profile.windows.panel_title_pattern)
        for selectors in (profile.classic, profile.efficiency):
            compile_pattern(selectors.bubble_class_pattern)
            compile_pattern(selectors.self_class_pattern)
            compile_pattern(selectors.msg_area_class_pattern)
            compile_pattern(selectors.header_class_pattern)
            compile_pattern(selectors.editor_class_pattern)
            for pattern in selectors.bubble_exclude_class_patterns:
                compile_pattern(pattern)
            for pattern in (*selectors.sender_class_patterns, *selectors.time_class_patterns):
                compile_pattern(pattern)
        if profile.behavior.default_mode not in ("classic", "efficiency"):
            raise QqProfileError("QQ UI 配置 behavior.default_mode 只能是 classic/efficiency")
        return profile


def default_profile_path() -> Path:
    return project_root() / "config" / "qq_ui_profile.json"


def load_profile(path: str | Path | None = None) -> QqUiProfile:
    """加载选择器配置；缺失或格式错误抛 ``QqProfileError``（中文可读）。"""

    profile_path = Path(path) if path is not None else default_profile_path()
    if not profile_path.is_file():
        raise QqProfileError(
            f"未找到 QQ UI 选择器配置：{profile_path}。"
            "请确认 config/qq_ui_profile.json 随程序一起安装，或重新运行 qq_probe 校准。"
        )
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QqProfileError(f"无法读取 QQ UI 选择器配置：{profile_path}（{exc}）") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QqProfileError(
            f"QQ UI 选择器配置 JSON 格式错误：{profile_path} 第 {exc.lineno} 行（{exc.msg}）"
        ) from exc
    return QqUiProfile.from_dict(data, source_path=str(profile_path))


# ---------------------------------------------------------------------------
# QQ 版本探测与校准
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str | None) -> tuple[int, int, int, int] | None:
    if not text:
        return None
    match = _VERSION_RE.search(str(text))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


@dataclass(frozen=True)
class VersionCheck:
    detected: str | None
    baseline: str
    compatible: bool | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "baseline": self.baseline,
            "compatible": self.compatible,
            "note": self.note,
        }


def version_check(detected: str | None, baseline: str) -> VersionCheck:
    detected_tuple = parse_version(detected)
    baseline_tuple = parse_version(baseline)
    if detected is None:
        return VersionCheck(
            detected=None,
            baseline=baseline,
            compatible=None,
            note="未检测到 QQ 版本（文件不存在或版本资源不可读），无法校准",
        )
    if detected_tuple is None or baseline_tuple is None:
        return VersionCheck(
            detected=detected,
            baseline=baseline,
            compatible=None,
            note="QQ 版本号格式无法解析，无法校准",
        )
    if detected_tuple == baseline_tuple:
        return VersionCheck(
            detected=detected,
            baseline=baseline,
            compatible=True,
            note="QQ 版本与基线一致，选择器按基线校准",
        )
    if detected_tuple[:2] == baseline_tuple[:2]:
        return VersionCheck(
            detected=detected,
            baseline=baseline,
            compatible=True,
            note="QQ 与基线同主/次版本，选择器应可用；建议重跑探针确认",
        )
    return VersionCheck(
        detected=detected,
        baseline=baseline,
        compatible=False,
        note=(
            "QQ 版本与基线不一致，UIA 类名可能已变化：请在 Lead 授权后重新运行 "
            "tools/qq_probe.py 校准选择器，再更新 config/qq_ui_profile.json"
        ),
    )


def detect_file_version(exe_path: str | Path | None) -> str | None:
    """用 Win32 版本资源读取 exe 文件版本（只读；不加载/不启动该程序）。"""

    if not exe_path:
        return None
    path = Path(exe_path)
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        version_dll = ctypes.WinDLL("version")
        version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        size = version_dll.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        version_dll.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
        if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        version_dll.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version_dll.VerQueryValueW.restype = wintypes.BOOL
        info_ptr = ctypes.c_void_p()
        info_len = wintypes.UINT()
        if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(info_ptr), ctypes.byref(info_len)):
            return None
        info = ctypes.cast(info_ptr, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # pragma: no cover - 依赖本机 version.dll
        return None


__all__ = [
    "QqUiMode",
    "SearchBoxSpec",
    "SearchResultSpec",
    "ModeSelectors",
    "WindowSpec",
    "ModeToggleSpec",
    "BehaviorSpec",
    "QqUiProfile",
    "VersionCheck",
    "compile_pattern",
    "default_profile_path",
    "load_profile",
    "parse_version",
    "version_check",
    "detect_file_version",
]
