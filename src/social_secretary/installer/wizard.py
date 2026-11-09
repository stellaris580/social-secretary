"""安装器/卸载器入口：Tkinter 安装向导 + 无界面（--silent）CLI。

两种运行模式：
1. **安装模式**：由 ``packaging/installer.spec`` 打成的 onefile 安装程序启动
   （payload 内嵌在 ``sys._MEIPASS/payload``），或源码模式 ``python -m social_secretary --install``；
2. **卸载模式**：已安装的 ``SocialSecretary.exe --uninstall``（app.py 转发到 ``main``）。

``main(argv) -> int`` 是唯一入口，返回值即进程退出码：
- 0 成功；1 参数/环境错误；2 payload 校验失败；3 安装/卸载失败。

支持参数（安装）：
    --silent                 无界面静默安装
    --dir=<路径>             安装目录（默认 %LOCALAPPDATA%\\Programs\\SocialSecretary）
    --desktop / --no-desktop 是否创建桌面快捷方式（默认不创建）
    --no-start-menu          不创建开始菜单快捷方式
    --launch / --no-launch   安装完成后是否启动（静默默认为 --launch）
    --log=<文件>             安装元数据 JSONL 日志（只含路径/大小/结果，不含消息内容）
    --result=<文件>          把结果 JSON 写到文件（供自动化验证）
    --selfcheck              校验内嵌 payload 并输出 JSON（构建/验证用）
    --skip-shortcuts         跳过快捷方式（仅沙箱降级验证用，会降低真实验证覆盖）
    --skip-registry          跳过 HKCU 卸载登记项（仅沙箱降级验证用）

支持参数（卸载）：
    --uninstall              进入卸载模式
    --silent                 无界面静默卸载
    --delete-data=0|1        是否删除用户数据（默认 0 = 保留）
    --no-confirm             跳过交互确认（静默模式自动跳过）
    --dir=<路径>             指定程序目录（默认取当前 exe 所在目录）
    --data-dir=<路径>        指定用户数据目录（默认从注册表 UserDataDir 读，否则默认路径）
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import operations as ops_mod
from .operations import (
    APP_EXE_NAME,
    APP_NAME,
    APP_VERSION,
    InstallOptions,
    InstallerOperations,
    StepRecord,
    UninstallOptions,
    build_uninstall_entries,
    default_data_dir,
    default_install_dir,
)

INSTALLER_VERSION = APP_VERSION


# ---------------------------------------------------------------------------
# 参数解析（手写，兼容 --key=value / --key value，且容忍未知参数）
# ---------------------------------------------------------------------------

class _Args:
    def __init__(self) -> None:
        self.silent = False
        self.install_dir: Path | None = None
        self.desktop: bool | None = None
        self.start_menu: bool = True
        self.launch: bool | None = None
        self.delete_data: bool = False
        self.no_confirm = False
        self.uninstall = False
        self.selfcheck = False
        self.skip_shortcuts = False
        self.skip_registry = False
        self.log_path: Path | None = None
        self.result_path: Path | None = None
        self.payload_dir: Path | None = None
        self.data_dir: Path | None = None
        self.install_flag = False


def _parse_args(argv: list[str]) -> _Args:
    args = _Args()
    takes_value = {"--dir", "--log", "--result", "--payload", "--data-dir", "--delete-data"}
    index = 0
    while index < len(argv):
        token = argv[index]
        key, value = token, None
        if token.startswith("--") and "=" in token:
            key, value = token.split("=", 1)
        if key in ("--install", "--installer"):
            args.install_flag = True
        elif key == "--silent":
            args.silent = True
        elif key == "--desktop":
            args.desktop = True
        elif key == "--no-desktop":
            args.desktop = False
        elif key == "--no-start-menu":
            args.start_menu = False
        elif key == "--launch":
            args.launch = True
        elif key == "--no-launch":
            args.launch = False
        elif key == "--delete-data":
            if value is None:
                if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                    value = argv[index + 1]
                    index += 1
                else:
                    value = "1"
            args.delete_data = str(value).strip().lower() in ("1", "true", "yes", "on")
        elif key == "--no-confirm":
            args.no_confirm = True
        elif key == "--uninstall":
            args.uninstall = True
        elif key == "--selfcheck":
            args.selfcheck = True
        elif key == "--skip-shortcuts":
            args.skip_shortcuts = True
        elif key == "--skip-registry":
            args.skip_registry = True
        elif key in takes_value:
            if value is None and index + 1 < len(argv):
                value = argv[index + 1]
                index += 1
            if value is None:
                raise ValueError(f"参数 {key} 需要一个值")
            if key == "--dir":
                args.install_dir = Path(value)
            elif key == "--log":
                args.log_path = Path(value)
            elif key == "--result":
                args.result_path = Path(value)
            elif key == "--payload":
                args.payload_dir = Path(value)
            elif key == "--data-dir":
                args.data_dir = Path(value)
        # 其它参数（如 app.py 透传的 --install）忽略，保持向后兼容
        index += 1
    return args


# ---------------------------------------------------------------------------
# payload / 输出
# ---------------------------------------------------------------------------

def resolve_payload_dir(override: Path | None = None) -> Path | None:
    """定位安装器内嵌（或源码模式下的）应用 payload。

    返回目录（onedir）或 ``payload.zip`` 文件路径（onefile 安装器内嵌 LZMA 压缩包）。
    """
    if override is not None:
        return Path(override)
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for candidate in (base / "payload.zip", base / "payload"):
            if candidate.exists():
                return candidate
    # 源码模式：<repo>/dist/AI社交秘书
    root = Path(__file__).resolve().parents[3]
    for candidate in (root / "dist" / "AI社交秘书", root / "dist" / APP_NAME):
        if candidate.is_dir():
            return candidate
    return None


def payload_is_valid(payload: Path | None) -> bool:
    """payload 是否可用（目录含 exe，或 zip 内含 exe）。"""
    if payload is None:
        return False
    payload = Path(payload)
    if payload.is_dir():
        return (payload / APP_EXE_NAME).is_file()
    if payload.is_file() and payload.name.lower().endswith(".zip"):
        try:
            import zipfile

            with zipfile.ZipFile(payload) as zf:
                return APP_EXE_NAME in {Path(name).name for name in zf.namelist()}
        except Exception:  # noqa: BLE001
            return False
    return False


def _payload_stats(payload: Path) -> tuple[int, int]:
    """返回 (文件数, 解压后字节数)；目录与 zip 都支持。"""
    payload = Path(payload)
    if payload.is_file():
        import zipfile

        try:
            with zipfile.ZipFile(payload) as zf:
                files = 0
                size = 0
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    files += 1
                    size += int(info.file_size)
                return files, size
        except Exception:  # noqa: BLE001
            return 0, 0
    files = 0
    size = 0
    for item in payload.rglob("*"):
        try:
            if item.is_file():
                files += 1
                size += item.stat().st_size
        except OSError:
            continue
    return files, size


def _safe_print(text: str) -> None:
    try:
        if sys.stdout is not None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 - 冻结版无控制台时 stdout 可能不可用
        pass


def _emit(result: dict[str, Any], result_path: Path | None) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    _safe_print(text)
    if result_path is not None:
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(text, encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 默认依赖（便于测试注入 fake）
# ---------------------------------------------------------------------------

def _default_operations() -> InstallerOperations:
    return InstallerOperations()


# ---------------------------------------------------------------------------
# 安装 / 卸载（无界面）
# ---------------------------------------------------------------------------

def _resolve_uninstall_install_dir(args: _Args, operations: InstallerOperations) -> Path:
    if args.install_dir is not None:
        return Path(args.install_dir)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 源码调试：从注册表读安装位置，否则用默认目录
    try:
        entries = operations.registry.read_entries(ops_mod.UNINSTALL_KEY_PATH)
    except Exception:  # noqa: BLE001
        entries = None
    if entries and entries.get("InstallLocation"):
        return Path(str(entries["InstallLocation"]))
    return default_install_dir()


def _resolve_uninstall_data_dir(args: _Args, operations: InstallerOperations) -> Path:
    if args.data_dir is not None:
        return Path(args.data_dir)
    try:
        entries = operations.registry.read_entries(ops_mod.UNINSTALL_KEY_PATH)
    except Exception:  # noqa: BLE001
        entries = None
    if entries and entries.get("UserDataDir"):
        return Path(str(entries["UserDataDir"]))
    return default_data_dir()


def _run_selfcheck(args: _Args) -> int:
    payload = resolve_payload_dir(args.payload_dir)
    result: dict[str, Any] = {
        "mode": "selfcheck",
        "installer_version": INSTALLER_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "payload_dir": str(payload) if payload else None,
        "ok": False,
    }
    if payload is None or not payload_is_valid(payload):
        result["error"] = "payload 不存在或损坏"
        _emit(result, args.result_path)
        return 2
    files, size = _payload_stats(payload)
    exe_present = payload_is_valid(payload)
    result.update(
        {
            "payload_files": files,
            "payload_bytes": size,
            "app_exe_present": exe_present,
            "install_dir_default": str(default_install_dir()),
            "data_dir_default": str(default_data_dir()),
            "ok": bool(exe_present and files > 0),
        }
    )
    _emit(result, args.result_path)
    return 0 if result["ok"] else 2


def run_install_silent(args: _Args, operations: InstallerOperations) -> int:
    payload = resolve_payload_dir(args.payload_dir)
    if payload is None or not payload_is_valid(payload):
        _emit(
            {"mode": "install", "ok": False, "exit_code": 2, "error": "payload 不存在或损坏"},
            args.result_path,
        )
        return 2
    install_dir = Path(args.install_dir) if args.install_dir else default_install_dir()
    options = InstallOptions(
        install_dir=install_dir,
        create_start_menu_shortcut=bool(args.start_menu),
        create_desktop_shortcut=bool(args.desktop),
        launch_after_install=bool(args.launch) if args.launch is not None else True,
        update_registry=not args.skip_registry,
        skip_shortcuts=args.skip_shortcuts,
        log_path=args.log_path,
    )
    result = operations.install(payload, options)
    payload_out = result.as_dict()
    payload_out["installer_version"] = INSTALLER_VERSION
    _emit(payload_out, args.result_path)
    return int(result.exit_code)


def run_uninstall_silent(args: _Args, operations: InstallerOperations) -> int:
    install_dir = _resolve_uninstall_install_dir(args, operations)
    data_dir = _resolve_uninstall_data_dir(args, operations)
    options = UninstallOptions(
        install_dir=install_dir,
        data_dir=data_dir,
        delete_data=bool(args.delete_data),
        remove_shortcuts=bool(args.start_menu),
        update_registry=not args.skip_registry,
        skip_shortcuts=args.skip_shortcuts,
        log_path=args.log_path,
    )
    result = operations.uninstall(options)
    payload_out = result.as_dict()
    payload_out["data_dir"] = str(data_dir)
    payload_out["installer_version"] = INSTALLER_VERSION
    _emit(payload_out, args.result_path)
    return int(result.exit_code)


# ---------------------------------------------------------------------------
# Tkinter 安装向导（交互模式）
# ---------------------------------------------------------------------------

def _enable_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 非 Windows/旧系统忽略
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _run_install_wizard(args: _Args, operations: InstallerOperations) -> int:
    """交互安装向导：欢迎/只读声明 → 选择目录与选项 → 进度 → 完成。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    payload = resolve_payload_dir(args.payload_dir)
    if payload is None or not payload_is_valid(payload):
        messagebox.showerror(APP_NAME, "安装包损坏：找不到内嵌的应用程序文件。")
        return 2

    _enable_dpi_awareness()
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION} 安装程序")
    root.resizable(False, False)
    try:
        if payload.is_dir():
            root.iconbitmap(default=str(payload / "_internal" / "assets" / "social_secretary.ico"))
    except Exception:  # noqa: BLE001
        pass

    state: dict[str, Any] = {
        "exit_code": 1,
        "install_dir": tk.StringVar(value=str(Path(args.install_dir) if args.install_dir else default_install_dir())),
        "desktop": tk.BooleanVar(value=bool(args.desktop)),
        "launch": tk.BooleanVar(value=True),
        "finished": False,
        "result": None,
    }
    content = ttk.Frame(root, padding=18)
    content.pack(fill="both", expand=True)
    root.geometry("560x420")

    def clear() -> None:
        for child in content.winfo_children():
            child.destroy()

    def show_welcome() -> None:
        clear()
        ttk.Label(content, text=f"{APP_NAME}", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(content, text=f"版本 {APP_VERSION} · Windows 10/11 桌面程序", foreground="#555").pack(anchor="w", pady=(2, 12))
        box = tk.Text(content, height=11, width=68, wrap="word", relief="flat", background="#f5f7fb")
        box.insert(
            "1.0",
            "本程序是只读的社交消息整理助手：\n\n"
            "·  只读取你在程序内明确授权的 QQ / QQ 邮箱会话；\n"
            "·  不发送 QQ 消息、不发送邮件、不修改联系人、不删除远端消息；\n"
            "·  所有数据只保存在本机，默认不联网；\n"
            "·  邮箱授权码与 AI Key 只存 Windows 凭据管理器。\n\n"
            "安装为当前用户安装（PerUser），不需要管理员权限。\n"
            "安装位置可自定义；用户数据与程序目录分开存放，卸载默认保留数据。",
        )
        box.config(state="disabled")
        box.pack(fill="both", expand=True, pady=(0, 12))
        buttons = ttk.Frame(content)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=lambda: (state.__setitem__("exit_code", 1), root.destroy())).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="下一步", command=show_options).pack(side="right")

    def show_options() -> None:
        clear()
        ttk.Label(content, text="安装选项", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(content)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="安装目录：").pack(side="left")
        entry = ttk.Entry(row, textvariable=state["install_dir"], width=52)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def browse() -> None:
            chosen = filedialog.askdirectory(title="选择安装目录", initialdir=str(Path(state["install_dir"].get()).parent))
            if chosen:
                state["install_dir"].set(str(Path(chosen) / ops_mod.APP_ID))

        ttk.Button(row, text="浏览…", command=browse).pack(side="left")
        ttk.Checkbutton(content, text="创建桌面快捷方式", variable=state["desktop"]).pack(anchor="w", pady=(10, 2))
        ttk.Checkbutton(content, text="安装完成后启动程序", variable=state["launch"]).pack(anchor="w", pady=(2, 2))
        ttk.Label(
            content,
            text="提示：安装器只写当前用户的程序目录、开始菜单快捷方式和卸载登记项，不需要管理员权限。",
            foreground="#666",
            wraplength=500,
        ).pack(anchor="w", pady=(10, 0))
        buttons = ttk.Frame(content)
        buttons.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(buttons, text="取消", command=lambda: (state.__setitem__("exit_code", 1), root.destroy())).pack(side="right", padx=(8, 0))
        install_button = ttk.Button(buttons, text="安装")
        install_button.pack(side="right")
        install_button.config(command=lambda: show_progress_and_install(install_button))

    def show_progress_and_install(install_button: Any) -> None:
        install_dir = Path(state["install_dir"].get().strip() or str(default_install_dir()))
        options = InstallOptions(
            install_dir=install_dir,
            create_start_menu_shortcut=bool(args.start_menu),
            create_desktop_shortcut=bool(state["desktop"].get()),
            launch_after_install=False,  # 完成页勾选后再启动
            update_registry=not args.skip_registry,
            skip_shortcuts=args.skip_shortcuts,
            log_path=args.log_path,
        )
        if install_dir.exists() and any(install_dir.iterdir()):
            if not messagebox.askyesno(APP_NAME, f"目标目录已存在，是否覆盖安装？\n\n{install_dir}"):
                return
        clear()
        ttk.Label(content, text="正在安装…", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", pady=(0, 12))
        status = ttk.Label(content, text="正在复制程序文件（首次安装约需几秒）…", foreground="#555")
        status.pack(anchor="w", pady=(0, 10))
        bar = ttk.Progressbar(content, mode="indeterminate", length=480)
        bar.pack(fill="x", pady=(0, 10))
        bar.start(12)
        root.update_idletasks()

        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["result"] = operations.install(payload, options)
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        def poll() -> None:
            if thread.is_alive():
                root.after(120, poll)
                return
            bar.stop()
            result = outcome.get("result")
            if outcome.get("error") or result is None or not result.ok:
                message = outcome.get("error") or (result.error if result else "未知错误")
                messagebox.showerror(APP_NAME, f"安装失败：{message}\n\n已自动清理安装半成品。")
                state["exit_code"] = 3
                root.destroy()
                return
            state["result"] = result
            show_finish(result)

        root.after(120, poll)

    def show_finish(result: Any) -> None:
        clear()
        ttk.Label(content, text="安装完成", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            content,
            text=f"{APP_NAME} {APP_VERSION} 已安装到：\n{result.install_dir}\n\n"
            "· 程序目录与用户数据分开存放；用户数据卸载时默认保留。\n"
            "· 可在「设置 → 应用」中找到并卸载。",
            justify="left",
        ).pack(anchor="w", pady=(0, 12))
        launch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(content, text="立即启动程序", variable=launch_var).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(content)
        buttons.pack(fill="x", side="bottom", pady=(16, 0))

        def finish() -> None:
            state["exit_code"] = 0
            root.destroy()
            if launch_var.get():
                try:
                    operations.process.launch(result.install_dir / APP_EXE_NAME, (), result.install_dir)
                except OSError:
                    pass

        ttk.Button(buttons, text="完成", command=finish).pack(side="right")

    show_welcome()
    root.mainloop()
    if state["result"] is not None:
        payload_out = state["result"].as_dict()
        payload_out["interactive"] = True
        _emit(payload_out, args.result_path)
    return int(state["exit_code"])


def _run_uninstall_wizard(args: _Args, operations: InstallerOperations) -> int:
    """交互卸载：确认 → 询问是否删除本地数据（默认不删）→ 执行。"""
    import tkinter as tk
    from tkinter import messagebox

    _enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    install_dir = _resolve_uninstall_install_dir(args, operations)
    try:
        confirmed = messagebox.askyesno(
            f"卸载 {APP_NAME}",
            f"确定要卸载 {APP_NAME} 吗？\n\n程序位置：{install_dir}\n\n"
            "卸载会删除程序文件和开始菜单快捷方式；用户数据默认保留。",
        )
        if not confirmed:
            root.destroy()
            return 1
        delete_data = messagebox.askyesno(
            f"卸载 {APP_NAME}",
            "是否同时删除本机保存的消息记录、摘要、待办与设置？\n\n"
            "选择「否」将保留数据（推荐）；此操作不可恢复。",
        )
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    options = UninstallOptions(
        install_dir=install_dir,
        data_dir=_resolve_uninstall_data_dir(args, operations),
        delete_data=bool(delete_data),
        remove_shortcuts=bool(args.start_menu),
        update_registry=not args.skip_registry,
        skip_shortcuts=args.skip_shortcuts,
        log_path=args.log_path,
    )
    result = operations.uninstall(options)
    payload_out = result.as_dict()
    payload_out["interactive"] = True
    _emit(payload_out, args.result_path)
    if not result.ok:
        try:
            messagebox.showerror(f"卸载 {APP_NAME}", f"卸载失败：{result.error}")
        except Exception:  # noqa: BLE001
            pass
    return int(result.exit_code)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None, *, operations: InstallerOperations | None = None) -> int:
    """安装器/卸载器统一入口；返回进程退出码（0 成功）。"""
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parse_args(raw)
    except ValueError as exc:
        _safe_print(f"参数错误：{exc}")
        return 1
    ops = operations or _default_operations()

    if args.selfcheck:
        return _run_selfcheck(args)

    if args.uninstall:
        if args.silent or args.no_confirm:
            return run_uninstall_silent(args, ops)
        return _run_uninstall_wizard(args, ops)

    if args.silent:
        return run_install_silent(args, ops)
    return _run_install_wizard(args, ops)


__all__ = [
    "INSTALLER_VERSION",
    "main",
    "resolve_payload_dir",
    "run_install_silent",
    "run_uninstall_silent",
]
