"""自研安装器核心操作：复制 payload、创建快捷方式、写 HKCU 卸载登记项、卸载清理。

设计原则（与 Lead 的决策一致）：
1. **不依赖 Inno Setup / Qt**：本机 Inno Setup 运行时在 Windows 11 25H2 (26200) 上无法初始化
   （即使 full-access、HKCU 可写，安装器仍 exit=1/无日志），因此安装器完全自研。
2. **不请求管理员权限**：只写 HKCU 与用户目录（PerUser 安装），不需要 UAC。
3. **依赖可注入**：``RegistryOps`` / ``ShortcutOps`` / ``FileOps`` / ``ProcessOps`` 四个协议，
   生产实现用 winreg/pywin32副，测试使用 fake（沙箱里也能全绿）。
4. **安全**：注册表/日志只写安装元数据（路径、版本、大小），绝不写消息内容、凭据、密钥。
5. **失败可回滚**：复制、快捷方式、注册表任一步失败都会清理已产生的半成品，不留残骸。

安装目录与用户数据目录分离：
- 程序目录：``%LOCALAPPDATA%\\Programs\\SocialSecretary``（可用 ``--dir`` 覆盖）；
- 用户数据：``%LOCALAPPDATA%\\SocialSecretary``（由应用自身创建；卸载默认保留）。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

APP_ID = "SocialSecretary"
APP_NAME = "AI 社交秘书"
APP_PUBLISHER = "SocialSecretary Project"
APP_VERSION = "0.2.5"
APP_EXE_NAME = "SocialSecretary.exe"
APP_DATA_DIR_NAME = "SocialSecretary"

#: HKCU 下的卸载登记项（Windows「设置 → 应用」读取此处）
UNINSTALL_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SocialSecretary"
#: 安装器/卸载器写入的元数据日志（默认不写；--log 显式指定才写）
LOG_MARKER = "[installer]"


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def _localappdata() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value)
    return Path(os.path.expanduser("~")) / "AppData" / "Local"


def default_install_dir() -> Path:
    """默认程序目录；测试/便携场景可用 ``SOCIAL_SECRETARY_INSTALL_DIR`` 覆盖。"""
    override = os.environ.get("SOCIAL_SECRETARY_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    return _localappdata() / "Programs" / APP_ID


def default_data_dir() -> Path:
    """默认用户数据目录（与程序目录分离；卸载默认保留）。"""
    override = os.environ.get("SOCIAL_SECRETARY_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return _localappdata() / APP_DATA_DIR_NAME


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstallOptions:
    install_dir: Path
    create_start_menu_shortcut: bool = True
    create_desktop_shortcut: bool = False
    launch_after_install: bool = True
    update_registry: bool = True
    #: 仅测试/沙箱降级验证使用：跳过快捷方式（没有 APPDATA 写权限时）
    skip_shortcuts: bool = False
    log_path: Path | None = None


@dataclass(frozen=True)
class UninstallOptions:
    install_dir: Path
    data_dir: Path
    delete_data: bool = False
    remove_shortcuts: bool = True
    update_registry: bool = True
    skip_shortcuts: bool = False
    log_path: Path | None = None


@dataclass
class StepRecord:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class InstallResult:
    ok: bool
    exit_code: int
    install_dir: Path
    steps: list[StepRecord] = field(default_factory=list)
    error: str | None = None
    file_count: int = 0
    size_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "install",
            "ok": self.ok,
            "exit_code": self.exit_code,
            "install_dir": str(self.install_dir),
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
            "error": self.error,
            "steps": [s.as_dict() for s in self.steps],
        }


@dataclass
class UninstallResult:
    ok: bool
    exit_code: int
    install_dir: Path
    steps: list[StepRecord] = field(default_factory=list)
    error: str | None = None
    data_deleted: bool = False
    data_preserved: bool = False
    self_delete_scheduled: bool = False
    shortcuts_removed: bool = False
    registry_removed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "uninstall",
            "ok": self.ok,
            "exit_code": self.exit_code,
            "install_dir": str(self.install_dir),
            "data_deleted": self.data_deleted,
            "data_preserved": self.data_preserved,
            "self_delete_scheduled": self.self_delete_scheduled,
            "shortcuts_removed": self.shortcuts_removed,
            "registry_removed": self.registry_removed,
            "error": self.error,
            "steps": [s.as_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# 可注入的操作协议 + 真实实现
# ---------------------------------------------------------------------------

class FileOps(Protocol):
    def exists(self, path: Path) -> bool: ...
    def is_dir(self, path: Path) -> bool: ...
    def make_dirs(self, path: Path) -> None: ...
    def copy_tree(self, src: Path, dst: Path) -> int: ...
    def extract_zip(self, archive: Path, dst: Path) -> int: ...
    def move_dir(self, src: Path, dst: Path) -> None: ...
    def remove_tree(self, path: Path) -> None: ...
    def dir_size_bytes(self, path: Path) -> int: ...
    def write_text(self, path: Path, text: str) -> None: ...
    def append_json_line(self, path: Path, payload: Mapping[str, Any]) -> None: ...


class RealFileOps:
    def exists(self, path: Path) -> bool:
        return Path(path).exists()

    def is_dir(self, path: Path) -> bool:
        return Path(path).is_dir()

    def make_dirs(self, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def copy_tree(self, src: Path, dst: Path) -> int:
        count = 0
        src, dst = Path(src), Path(dst)
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                count += 1
        return count

    def extract_zip(self, archive: Path, dst: Path) -> int:
        """解压 LZMA/DEFLATE 压缩的 payload（onefile 安装器内嵌的 payload.zip）。"""
        import zipfile  # noqa: PLC0415 - 仅安装时需要

        archive, dst = Path(archive), Path(dst)
        dst.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    (dst / info.filename).mkdir(parents=True, exist_ok=True)
                    continue
                target = dst / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                count += 1
        return count

    def move_dir(self, src: Path, dst: Path) -> None:
        src, dst = Path(src), Path(dst)
        try:
            src.rename(dst)
        except OSError:
            # 跨卷/占位回退：复制后删源
            self.copy_tree(src, dst)
            self.remove_tree(src)

    def remove_tree(self, path: Path) -> None:
        path = Path(path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def dir_size_bytes(self, path: Path) -> int:
        total = 0
        for item in Path(path).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def write_text(self, path: Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append_json_line(self, path: Path, payload: Mapping[str, Any]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class RegistryOps(Protocol):
    def write_entries(self, path: str, entries: Mapping[str, object]) -> None: ...
    def delete_key(self, path: str) -> bool: ...
    def read_entries(self, path: str) -> dict[str, Any] | None: ...
    def is_writable(self) -> bool: ...


class WinRegistryOps:
    """HKCU 注册表实现（仅当前用户，不需要管理员）。"""

    def __init__(self) -> None:
        import winreg  # 仅 Windows

        self._winreg = winreg

    def write_entries(self, path: str, entries: Mapping[str, object]) -> None:
        winreg = self._winreg
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE)
        try:
            for name, value in entries.items():
                if isinstance(value, bool):
                    value = int(value)
                if isinstance(value, int):
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        finally:
            winreg.CloseKey(key)

    def delete_key(self, path: str) -> bool:
        winreg = self._winreg
        # 刚安装后立即卸载时，注册表/安全软件可能短暂占用；重试 + reg.exe 兜底。
        for _attempt in range(4):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                return True
            except FileNotFoundError:
                return False
            except OSError:
                time.sleep(0.25)
        try:
            proc = subprocess.run(  # noqa: S603 - 固定命令
                ["reg.exe", "delete", f"HKCU\\{path}", "/f"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return True
        except OSError:
            pass
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            return True
        except OSError:
            pass
        return False

    def read_entries(self, path: str) -> dict[str, Any] | None:
        winreg = self._winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
        except FileNotFoundError:
            return None
        try:
            result: dict[str, Any] = {}
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                result[name] = value
                index += 1
            return result
        finally:
            winreg.CloseKey(key)

    def is_writable(self) -> bool:
        winreg = self._winreg
        probe = r"Software\_social_secretary_installer_probe"
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, probe, 0, winreg.KEY_WRITE)
            winreg.CloseKey(key)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, probe)
            return True
        except OSError:
            return False


class ShortcutOps(Protocol):
    def start_menu_programs_dir(self) -> Path: ...
    def desktop_dir(self) -> Path: ...
    def create_lnk(self, lnk_path: Path, target: str, *, arguments: str = "", working_dir: str = "",
                   icon: str = "", description: str = "") -> None: ...
    def remove_lnk(self, lnk_path: Path) -> bool: ...
    def exists(self, lnk_path: Path) -> bool: ...


def _known_folder(csidl: int) -> Path:
    """用 SHGetFolderPathW 取已知文件夹（不依赖 COM/pywin32）。"""
    import ctypes
    from ctypes import wintypes

    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    shell32 = ctypes.windll.shell32
    CSIDL_FLAG_CREATE = 0x8000
    result = shell32.SHGetFolderPathW(None, csidl | CSIDL_FLAG_CREATE, None, 0, buf)
    if result != 0:
        raise OSError(f"SHGetFolderPathW 失败，csidl={csidl:#x}, hr={result:#x}")
    return Path(buf.value)


class WinShortcutOps:
    """开始菜单/桌面快捷方式实现（WScript.Shell COM，pywin32 已随应用打包）。"""

    CSIDL_PROGRAMS = 0x0002
    CSIDL_DESKTOPDIRECTORY = 0x0010

    def start_menu_programs_dir(self) -> Path:
        return _known_folder(self.CSIDL_PROGRAMS)

    def desktop_dir(self) -> Path:
        return _known_folder(self.CSIDL_DESKTOPDIRECTORY)

    def create_lnk(self, lnk_path: Path, target: str, *, arguments: str = "", working_dir: str = "",
                   icon: str = "", description: str = "") -> None:
        lnk_path = Path(lnk_path)
        lnk_path.parent.mkdir(parents=True, exist_ok=True)
        import pythoncom  # noqa: PLC0415 - 需要 COM 初始化

        try:
            pythoncom.CoInitialize()
        except OSError:
            pass
        try:
            from win32com.client import Dispatch  # noqa: PLC0415

            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(lnk_path))
            shortcut.TargetPath = str(target)
            if arguments:
                shortcut.Arguments = str(arguments)
            shortcut.WorkingDirectory = str(working_dir or Path(target).parent)
            if icon:
                shortcut.IconLocation = str(icon)
            if description:
                shortcut.Description = str(description)
            shortcut.Save()
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    def remove_lnk(self, lnk_path: Path) -> bool:
        lnk_path = Path(lnk_path)
        # Windows 上刚创建/被资源管理器短暂占用的 .lnk 可能瞬时拒绝删除；
        # 增加去只读 + 短暂重试，避免卸载器“报告成功但快捷方式还在”。
        for _attempt in range(4):
            try:
                lnk_path.unlink()
                return True
            except FileNotFoundError:
                return False
            except OSError:
                try:
                    os.chmod(lnk_path, stat.S_IWRITE)
                except OSError:
                    pass
                time.sleep(0.25)
        try:
            return not lnk_path.exists()
        except OSError:
            return False

    def exists(self, lnk_path: Path) -> bool:
        return Path(lnk_path).is_file()


class ProcessOps(Protocol):
    def launch(self, exe: Path, arguments: Sequence[str] = (), cwd: Path | None = None) -> None: ...
    def schedule_delete(self, paths: Sequence[Path], *, delay_seconds: int = 2) -> bool: ...


class RealProcessOps:
    """启动应用 + 延迟自删除（安装目录里的 exe 自己不能删除自己）。"""

    def launch(self, exe: Path, arguments: Sequence[str] = (), cwd: Path | None = None) -> None:
        subprocess.Popen(  # noqa: S603 - 参数固定，均来自本程序
            [str(exe), *[str(a) for a in arguments]],
            cwd=str(cwd) if cwd else str(Path(exe).parent),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    @staticmethod
    def _cleanup_script_lines(targets: Sequence[str], delay_seconds: int) -> list[str]:
        """生成延迟清理批处理内容（等待调用方进程退出后重试 rmdir）。"""
        lines = ["@echo off", "setlocal"]
        lines.append(f"timeout /t {int(delay_seconds)} /nobreak >nul")
        for target in targets:
            lines.append("for /l %%i in (1,1,30) do (")
            lines.append(f'  if exist "{target}" ( timeout /t 1 /nobreak >nul & rmdir /s /q "{target}" )')
            lines.append(")")
        lines.append('del "%~f0" >nul 2>nul')
        return lines

    @staticmethod
    def _script_dir_candidates(target: Path) -> list[Path]:
        """候选脚本目录：真实用户环境用系统 TEMP；沙箱/受限环境退到用户数据目录或目标同级。"""
        candidates: list[Path] = []
        try:
            candidates.append(Path(tempfile.mkdtemp(prefix="ss-uninstall-")))
        except OSError:
            pass
        try:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            candidates.append(Path(base) / APP_DATA_DIR_NAME / "installer-tmp")
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(Path(target).parent / f".ss-uninstall-{os.getpid()}-{int(time.time())}")
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(Path(tempfile.gettempdir()) / "SocialSecretaryUninstall")
        except Exception:  # noqa: BLE001
            pass
        return candidates

    def _spawn_cleanup(self, targets: Sequence[str], delay_seconds: int) -> bool:
        """尝试多个可写目录落 cleanup.cmd 并启动；全部失败时用内联 cmd 兜底。"""
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        lines = self._cleanup_script_lines(targets, delay_seconds)
        for script_dir in self._script_dir_candidates(Path(targets[0])):
            try:
                script_dir.mkdir(parents=True, exist_ok=True)
                script = script_dir / "cleanup.cmd"
                script.write_text("\r\n".join(lines) + "\r\n", encoding="mbcs", errors="replace")
            except (OSError, LookupError):
                continue
            try:
                subprocess.Popen(  # noqa: S603 - 固定命令
                    ["cmd.exe", "/c", str(script)],
                    creationflags=flags,
                    close_fds=True,
                    cwd=str(script_dir),
                )
                return True
            except OSError:
                continue
        # 最后兜底：不落脚本文件，直接内联延迟命令（单次 rmdir，仍在 30s 内重试 5 次）
        for target in targets:
            inline = (
                f'timeout /t {int(delay_seconds)} /nobreak >nul & '
                f'for /l %%i in (1,1,10) do ( if exist "{target}" '
                f'( timeout /t 1 /nobreak >nul & rmdir /s /q "{target}" ) )'
            )
            try:
                subprocess.Popen(  # noqa: S603 - 固定命令
                    ["cmd.exe", "/c", inline],
                    creationflags=flags,
                    close_fds=True,
                )
            except OSError:
                continue
            return True
        return False

    def schedule_delete(self, paths: Sequence[Path], *, delay_seconds: int = 2) -> bool:
        targets = [str(Path(p)) for p in paths]
        if not targets:
            return False
        try:
            return self._spawn_cleanup(targets, delay_seconds)
        except Exception:  # noqa: BLE001 - 延迟删除失败不应让卸载流程崩溃
            return False


# ---------------------------------------------------------------------------
# 卸载登记项
# ---------------------------------------------------------------------------

def build_uninstall_entries(
    install_dir: Path,
    *,
    version: str = APP_VERSION,
    size_bytes: int = 0,
    data_dir: Path | None = None,
    install_date: str | None = None,
) -> dict[str, object]:
    """构造写入 HKCU 的卸载登记项（Windows「设置→应用」使用）。

    只包含安装元数据：程序名/版本/发布者/安装位置/图标/卸载命令/占用大小。
    **不包含任何消息内容、凭据或密钥。**
    """
    install_dir = Path(install_dir)
    exe = install_dir / APP_EXE_NAME
    exe_q = f'"{exe}"'
    date = install_date or datetime.now().strftime("%Y%m%d")
    estimated_kb = max(1, int(size_bytes) // 1024) if size_bytes else 1
    entries: dict[str, object] = {
        "DisplayName": APP_NAME,
        "DisplayVersion": str(version),
        "Publisher": APP_PUBLISHER,
        "InstallLocation": str(install_dir),
        "DisplayIcon": str(exe),
        "UninstallString": f"{exe_q} --uninstall",
        "QuietUninstallString": f"{exe_q} --uninstall --silent --no-confirm",
        "EstimatedSize": estimated_kb,
        "NoModify": 1,
        "NoRepair": 1,
        "InstallDate": date,
        "Comments": "只读整合 QQ 与 QQ 邮箱，不发送任何消息。",
    }
    if data_dir is not None:
        entries["UserDataDir"] = str(Path(data_dir))
    return entries


# ---------------------------------------------------------------------------
# 安装 / 卸载核心逻辑
# ---------------------------------------------------------------------------

class InstallerOperations:
    """安装/卸载编排器；所有副作用通过注入的 4 个 ops 完成。"""

    def __init__(
        self,
        *,
        files: FileOps | None = None,
        registry: RegistryOps | None = None,
        shortcuts: ShortcutOps | None = None,
        process: ProcessOps | None = None,
        clock=datetime.now,
    ) -> None:
        self.files: FileOps = files or RealFileOps()
        self.registry: RegistryOps = registry or WinRegistryOps()
        self.shortcuts: ShortcutOps = shortcuts or WinShortcutOps()
        self.process: ProcessOps = process or RealProcessOps()
        self._clock = clock

    # -- 日志（只写元数据） -------------------------------------------------
    @staticmethod
    def _zip_names(archive: Path) -> set[str] | None:
        """读取 payload.zip 条目名（仅用于校验安装包完整性）。"""
        import zipfile  # noqa: PLC0415

        try:
            with zipfile.ZipFile(archive) as zf:
                return {info.filename for info in zf.infolist()}
        except Exception:  # noqa: BLE001 - 损坏的 zip 交由调用方报错
            return None

    def _log(self, log_path: Path | None, payload: Mapping[str, Any]) -> None:
        if not log_path:
            return
        record = {"ts": self._clock().isoformat(timespec="seconds"), **dict(payload)}
        try:
            self.files.append_json_line(Path(log_path), record)
        except OSError:
            pass

    # -- 安装 ---------------------------------------------------------------
    def install(self, payload_dir: Path, options: InstallOptions) -> InstallResult:
        payload_dir = Path(payload_dir)
        target = Path(options.install_dir)
        result = InstallResult(ok=False, exit_code=1, install_dir=target)
        staging = target.with_name(target.name + ".installing")
        created_shortcuts: list[str] = []
        registry_written = False
        target_swapped = False

        def fail(message: str, code: int = 3) -> InstallResult:
            # 回滚：注册表 → 快捷方式 → 程序目录（仅本次安装产生的）
            if registry_written:
                try:
                    self.registry.delete_key(UNINSTALL_KEY_PATH)
                except Exception:  # noqa: BLE001
                    pass
            for lnk in reversed(created_shortcuts):
                try:
                    self.shortcuts.remove_lnk(Path(lnk))
                except Exception:  # noqa: BLE001
                    pass
            try:
                if self.files.exists(staging):
                    self.files.remove_tree(staging)
                if target_swapped and self.files.exists(target):
                    self.files.remove_tree(target)
            except Exception:  # noqa: BLE001
                pass
            result.ok = False
            result.exit_code = code
            result.error = message
            result.steps.append(StepRecord("rollback", True, "已清理安装半成品"))
            self._log(options.log_path, {"event": "install_failed", "error": message, "exit_code": code})
            return result

        try:
            # 0) payload 校验：支持目录（源码/onedir 模式）或 LZMA 压缩的 payload.zip（onefile 安装器）
            if self.files.is_dir(payload_dir):
                if not self.files.exists(payload_dir / APP_EXE_NAME):
                    return fail(f"payload 内缺少 {APP_EXE_NAME}（安装包损坏？）")
                payload_kind = "directory"
            elif self.files.exists(payload_dir) and str(payload_dir).lower().endswith(".zip"):
                names = self._zip_names(payload_dir)
                if names is None:
                    return fail(f"payload.zip 无法读取：{payload_dir}")
                if APP_EXE_NAME not in names:
                    return fail(f"payload.zip 内缺少 {APP_EXE_NAME}（安装包损坏？）")
                payload_kind = "zip"
            else:
                return fail(f"安装包 payload 缺失：{payload_dir}")
            result.steps.append(StepRecord("validate_payload", True, f"{payload_kind}: {payload_dir}"))
            self._log(options.log_path, {"event": "install_start", "payload": str(payload_dir),
                                         "payload_kind": payload_kind, "target": str(target)})

            # 1) 复制/解压到临时目录（失败可整体回滚）
            self.files.make_dirs(staging.parent)
            if self.files.exists(staging):
                self.files.remove_tree(staging)
            if payload_kind == "zip":
                file_count = self.files.extract_zip(payload_dir, staging)
            else:
                file_count = self.files.copy_tree(payload_dir, staging)
            size = self.files.dir_size_bytes(staging)
            result.file_count = int(file_count)
            result.size_bytes = int(size)
            result.steps.append(StepRecord("copy_payload", True, f"{file_count} 个文件 / {size} 字节"))
            self._log(options.log_path, {"event": "copy_payload", "files": int(file_count), "bytes": int(size)})

            # 2) 替换目标目录（先删旧版再改名）
            if self.files.exists(target):
                self.files.remove_tree(target)
            self.files.make_dirs(target.parent)
            self.files.move_dir(staging, target)
            target_swapped = True
            result.steps.append(StepRecord("replace_install_dir", True, str(target)))

            # 3) 快捷方式（可选）
            if not options.skip_shortcuts and (
                options.create_start_menu_shortcut or options.create_desktop_shortcut
            ):
                exe = target / APP_EXE_NAME
                if options.create_start_menu_shortcut:
                    lnk = self.shortcuts.start_menu_programs_dir() / f"{APP_NAME}.lnk"
                    self.shortcuts.create_lnk(
                        lnk,
                        str(exe),
                        working_dir=str(target),
                        icon=str(exe),
                        description=APP_NAME,
                    )
                    created_shortcuts.append(str(lnk))
                    result.steps.append(StepRecord("start_menu_shortcut", True, str(lnk)))
                if options.create_desktop_shortcut:
                    lnk = self.shortcuts.desktop_dir() / f"{APP_NAME}.lnk"
                    self.shortcuts.create_lnk(
                        lnk,
                        str(exe),
                        working_dir=str(target),
                        icon=str(exe),
                        description=APP_NAME,
                    )
                    created_shortcuts.append(str(lnk))
                    result.steps.append(StepRecord("desktop_shortcut", True, str(lnk)))
            else:
                result.steps.append(StepRecord("shortcuts", True, "已跳过"))
            self._log(options.log_path, {"event": "shortcuts", "created": len(created_shortcuts)})

            # 4) HKCU 卸载登记项（Windows 设置→应用 可见；不需要管理员）
            if options.update_registry:
                entries = build_uninstall_entries(
                    target,
                    version=APP_VERSION,
                    size_bytes=result.size_bytes,
                    data_dir=default_data_dir(),
                    install_date=self._clock().strftime("%Y%m%d"),
                )
                self.registry.write_entries(UNINSTALL_KEY_PATH, entries)
                registry_written = True
                result.steps.append(StepRecord("uninstall_registry", True, UNINSTALL_KEY_PATH))
            else:
                result.steps.append(StepRecord("uninstall_registry", True, "已跳过"))
            self._log(options.log_path, {"event": "registry", "key": UNINSTALL_KEY_PATH})

            # 5) 安装完成（可选启动）
            if options.launch_after_install:
                try:
                    self.process.launch(target / APP_EXE_NAME, (), target)
                    result.steps.append(StepRecord("launch_app", True, str(target / APP_EXE_NAME)))
                except OSError as exc:
                    result.steps.append(StepRecord("launch_app", False, str(exc)))
            else:
                result.steps.append(StepRecord("launch_app", True, "已跳过"))

            result.ok = True
            result.exit_code = 0
            result.steps.append(StepRecord("done", True, ""))
            self._log(options.log_path, {"event": "install_done", "files": result.file_count, "bytes": result.size_bytes})
            return result
        except Exception as exc:  # noqa: BLE001 - 统一回滚并返回错误
            return fail(f"{type(exc).__name__}: {exc}")

    # -- 卸载 ---------------------------------------------------------------
    def uninstall(self, options: UninstallOptions) -> UninstallResult:
        install_dir = Path(options.install_dir)
        data_dir = Path(options.data_dir)
        result = UninstallResult(ok=False, exit_code=1, install_dir=install_dir)

        try:
            # 1) 快捷方式（删除后**重新检查文件是否存在**，避免"报告成功但没删掉"）
            shortcuts_removed = True
            if not options.skip_shortcuts:
                try:
                    shortcut_paths = [
                        self.shortcuts.start_menu_programs_dir() / f"{APP_NAME}.lnk",
                        self.shortcuts.desktop_dir() / f"{APP_NAME}.lnk",
                    ]
                except OSError as exc:
                    shortcut_paths = []
                    shortcuts_removed = False
                    result.steps.append(StepRecord("shortcut_dirs", False, str(exc)))
                for lnk in shortcut_paths:
                    removed = False
                    for _attempt in range(4):
                        try:
                            removed = self.shortcuts.remove_lnk(lnk)
                        except OSError:
                            removed = False
                        try:
                            still_there = self.shortcuts.exists(lnk)
                        except OSError:
                            still_there = False
                        if removed or not still_there:
                            removed = True
                            break
                        time.sleep(0.25)
                    shortcuts_removed = shortcuts_removed and removed
                    result.steps.append(StepRecord("remove_shortcut", removed, f"{lnk} removed={removed}"))
            else:
                shortcuts_removed = True
                result.steps.append(StepRecord("remove_shortcut", True, "已跳过"))
            result.shortcuts_removed = bool(shortcuts_removed)
            self._log(options.log_path, {"event": "uninstall_shortcuts", "removed": bool(shortcuts_removed)})

            # 2) 卸载登记项（同样以"键是否真实消失"为准，重试 + reg.exe 兜底在 WinRegistryOps 内）
            registry_removed = True
            if options.update_registry:
                for _attempt in range(3):
                    try:
                        removed = self.registry.delete_key(UNINSTALL_KEY_PATH)
                    except OSError:
                        removed = False
                    try:
                        still_there = self.registry.read_entries(UNINSTALL_KEY_PATH) is not None
                    except OSError:
                        still_there = True
                    if removed or not still_there:
                        registry_removed = True
                        break
                    time.sleep(0.25)
                else:
                    registry_removed = False
                result.steps.append(StepRecord("remove_uninstall_registry", registry_removed,
                                               f"removed={registry_removed}"))
            else:
                registry_removed = True
                result.steps.append(StepRecord("remove_uninstall_registry", True, "已跳过"))
            result.registry_removed = bool(registry_removed)

            # 3) 用户数据（默认保留；只有显式 delete_data 才删）
            if options.delete_data:
                if self.files.exists(data_dir):
                    self.files.remove_tree(data_dir)
                result.data_deleted = not self.files.exists(data_dir)
                result.data_preserved = not result.data_deleted
                result.steps.append(StepRecord("delete_user_data", result.data_deleted, str(data_dir)))
            else:
                result.data_preserved = self.files.exists(data_dir)
                result.steps.append(StepRecord("keep_user_data", True, str(data_dir)))
            self._log(options.log_path, {"event": "user_data", "delete": options.delete_data,
                                         "deleted": result.data_deleted, "preserved": result.data_preserved})

            # 4) 程序目录：exe 正在运行，必须延迟删除（cmd 等待进程退出后 rmdir）
            scheduled = False
            try:
                scheduled = self.process.schedule_delete([install_dir], delay_seconds=2)
            except Exception:  # noqa: BLE001
                scheduled = False
            result.self_delete_scheduled = bool(scheduled)
            result.steps.append(StepRecord("schedule_delete_install_dir", bool(scheduled), str(install_dir)))

            if result.shortcuts_removed and result.registry_removed:
                result.ok = True
                result.exit_code = 0
                result.steps.append(StepRecord("done", True, ""))
            else:
                result.ok = False
                result.exit_code = 3
                result.error = (
                    "卸载后仍有残留："
                    f"shortcuts_removed={result.shortcuts_removed}, "
                    f"registry_removed={result.registry_removed}"
                )
                result.steps.append(StepRecord("done", False, result.error))
            self._log(options.log_path, {"event": "uninstall_done", "ok": result.ok,
                                         "shortcuts_removed": result.shortcuts_removed,
                                         "registry_removed": result.registry_removed,
                                         "install_dir": str(install_dir)})
            return result
        except Exception as exc:  # noqa: BLE001
            result.ok = False
            result.exit_code = 3
            result.error = f"{type(exc).__name__}: {exc}"
            self._log(options.log_path, {"event": "uninstall_failed", "error": result.error})
            return result


__all__ = [
    "APP_DATA_DIR_NAME",
    "APP_EXE_NAME",
    "APP_ID",
    "APP_NAME",
    "APP_PUBLISHER",
    "APP_VERSION",
    "FileOps",
    "InstallOptions",
    "InstallResult",
    "InstallerOperations",
    "ProcessOps",
    "RealFileOps",
    "RealProcessOps",
    "RegistryOps",
    "ShortcutOps",
    "StepRecord",
    "UNINSTALL_KEY_PATH",
    "UninstallOptions",
    "UninstallResult",
    "WinRegistryOps",
    "WinShortcutOps",
    "build_uninstall_entries",
    "default_data_dir",
    "default_install_dir",
]
