r"""生成脚本安装器 ZIP：`dist/installer/AI社交秘书-0.2.5-安装包.zip`。

ZIP 内容：
    安装.cmd / 安装.ps1        安装入口（PowerShell 脚本安装器，见 packaging/installer_zip/）
    卸载.cmd / 卸载.ps1        卸载入口
    README-安装说明.txt         面向非程序员的安装说明
    app/（onedir）                应用本体（复制自 dist\AI社交秘书）
    payload-manifest.json       构建元数据（版本、构建时间、文件数、大小、exe sha256）

为什么不再用 Inno/PyInstaller onefile 安装器（2026-10-31 Lead 决策）：
- 本机 Windows 11 25H2 (26200) 上 Inno Setup 运行时无法初始化（exit=1/挂起无日志）；
- PyInstaller onefile GUI 安装器在本机运行时解包失败（exit=-1，`_MEI` 解包被安全限制）。
脚本安装器完全受控、可读、无需管理员权限，且能被 verify_install_custom.ps1 脚本化验证。

用法：
    .venv\\Scripts\\python.exe packaging\\make_installer_zip.py ^
        [--app dist/AI社交秘书] [--out dist/installer/AI社交秘书-0.2.5-安装包.zip]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "packaging" / "installer_zip"
DEFAULT_APP = ROOT / "dist" / "AI社交秘书"
DEFAULT_OUT = ROOT / "dist" / "installer" / "AI社交秘书-0.2.5-安装包.zip"
APP_VERSION = "0.2.5"


def _project_version() -> str:
    """从 contracts.APP_VERSION 读版本（packaging 生成 ZIP 时以它为准，避免版本漂移）。"""
    contracts = ROOT / "src" / "social_secretary" / "contracts.py"
    try:
        text = contracts.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return APP_VERSION

#: ZIP 内的中文脚本文件名 -> 模板文件；.cmd 需要转成系统 ANSI（GBK）编码，
#: 否则中文 Windows 的 cmd.exe 会按 OEM 代码页解析导致乱码/路径失效。
CMD_TEMPLATES = {"安装.cmd": "安装.cmd", "卸载.cmd": "卸载.cmd"}
PS1_TEMPLATES = {"安装.ps1": "安装.ps1", "卸载.ps1": "卸载.ps1"}
TEXT_TEMPLATES = {"README-安装说明.txt": "README-安装说明.txt"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _encode_cmd(text: str) -> bytes:
    """批处理文件按系统 ANSI（中文 Windows = GBK）编码，保证 cmd.exe 解析中文路径。"""
    for encoding in ("gbk", "mbcs", "cp936", "utf-8"):
        try:
            return text.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            continue
    return text.encode("utf-8", errors="replace")


def _encode_ps1(text: str) -> bytes:
    """PowerShell 5.1 需要 UTF-8 with BOM 才能正确解析中文。"""
    return b"\xef\xbb\xbf" + text.replace("\ufeff", "").encode("utf-8")


def build_installer_zip(app_dir: Path, out_zip: Path, *, templates: Path = TEMPLATE_DIR) -> dict:
    app_dir = Path(app_dir).resolve()
    out_zip = Path(out_zip).resolve()
    app_exe = app_dir / "SocialSecretary.exe"
    if not app_exe.is_file():
        raise SystemExit(f"[installer-zip] app 目录缺少 SocialSecretary.exe：{app_dir}")
    for name in list(CMD_TEMPLATES) + list(PS1_TEMPLATES) + list(TEXT_TEMPLATES):
        if not (templates / name).is_file():
            raise SystemExit(f"[installer-zip] 缺少模板文件：{templates / name}")

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()

    app_files = [p for p in sorted(app_dir.rglob("*")) if p.is_file()]
    app_bytes = sum(p.stat().st_size for p in app_files)
    manifest = {
        "kind": "SocialSecretary-script-installer",
        "app_name": "AI 社交秘书",
        "app_version": _project_version(),
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app_exe": "SocialSecretary.exe",
        "app_exe_sha256": _sha256(app_exe),
        "app_files": len(app_files),
        "app_bytes": app_bytes,
        "installer": "PowerShell script (安装.ps1) + app/ onedir",
        "default_install_dir": r"%LOCALAPPDATA%\Programs\SocialSecretary",
        "user_data_dir": r"%LOCALAPPDATA%\SocialSecretary (uninstall keeps by default)",
        "uninstall": r'"<install>\SocialSecretary.exe" --uninstall --silent [--delete-data=1]',
    }

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for zh_name, template in CMD_TEMPLATES.items():
            zf.writestr(zh_name, _encode_cmd(_read_text(templates / template)))
        for zh_name, template in PS1_TEMPLATES.items():
            zf.writestr(zh_name, _encode_ps1(_read_text(templates / template)))
        for zh_name, template in TEXT_TEMPLATES.items():
            zf.writestr(zh_name, b"\xef\xbb\xbf" + _read_text(templates / template).encode("utf-8"))
        zf.writestr("payload-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        for path in app_files:
            rel = path.relative_to(app_dir).as_posix()
            zf.write(path, arcname="app/" + rel)

    summary = {
        "out": str(out_zip),
        "zip_bytes": out_zip.stat().st_size,
        "app_files": len(app_files),
        "app_bytes": app_bytes,
        "app_exe_sha256": manifest["app_exe_sha256"],
        "sha256": _sha256(out_zip),
        "manifest": manifest,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成脚本安装器 ZIP")
    ap.add_argument("--app", default=str(DEFAULT_APP))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    try:
        build_installer_zip(Path(args.app), Path(args.out))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[installer-zip] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
