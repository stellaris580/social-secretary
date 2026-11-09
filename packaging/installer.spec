# -*- mode: python ; coding: utf-8 -*-
"""自研安装程序打包配置：PyInstaller **onefile** GUI（Tkinter），内嵌应用 payload。

背景（2026-10-31 Lead 决策）：
本机 Windows 11 25H2 (build 26200) 上所有 Inno Setup 运行时都无法初始化（我们的
SocialSecretary-Setup、最小 Inno 6.0.5 安装器、官方 innosetup-7.1.1-x64.exe 均
exit=1/挂起且无日志；full-access 下 HKCU 可写仍然如此），因此安装器改为完全自研：

- 安装器 = 本 onefile exe（Python + Tkinter 向导；目标体积 ~20-25MB）；
- payload = 已构建的 ``dist/AI社交秘书/`` 经 ``packaging/make_payload_zip.py`` 预压缩成
  **LZMA 的 payload.zip**（体积约为 onedir 的 1/3），作为 datas 内嵌到 ``payload.zip``；
  安装时由 ``installer.operations`` 流式解压到安装目录（布局与 onedir 完全一致）。
  预压缩原因：PyInstaller onefile 只做 zlib 级压缩，直接内嵌 47MB onedir 会让安装器 ~35MB；
  改用 ZIP+LZMA 后安装器约 20MB 量级。
- 安装出来的程序通过 ``SocialSecretary.exe --uninstall`` 调用同一套
  ``social_secretary.installer.wizard`` 代码完成卸载（无需安装器在场）。

构建前置：先运行 ``packaging/build.ps1``（会依次完成主程序构建 + payload.zip + 安装器），
或手工保证 ``build/payload.zip`` 已存在。产物：``dist/installer/AI社交秘书-安装程序-0.2.5.exe``。

用法：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean ^
        --distpath dist\\installer --workpath build\\installer packaging\\installer.spec
"""
"""【已废弃 / DEPRECATED】2026-11-01：PyInstaller onefile GUI 安装器在本机运行时解包失败（exit=-1，_MEI 临时解包被安全限制），--selfcheck 也无法运行。
最终交付改为 ZIP + PowerShell 脚本安装器（packaging/installer_zip/ + make_installer_zip.py），
本文件仅保留作历史参考，不再参与 build.ps1 构建。



import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821  (PyInstaller 注入的 SPECPATH)
ROOT = SPEC_DIR.parent
SRC = ROOT / "src"
ENTRY = SPEC_DIR / "installer_main.py"
ICON = SPEC_DIR / "assets" / "social_secretary.ico"
VERSION_FILE = SPEC_DIR / "installer_version_info.txt"
PAYLOAD_ZIP = ROOT / "build" / "payload.zip"

INSTALLER_NAME = "AI社交秘书-安装程序-0.2.5"

sys.path.insert(0, str(SRC))

if not PAYLOAD_ZIP.is_file():
    raise SystemExit(
        f"[installer.spec] 缺少应用 payload.zip：{PAYLOAD_ZIP}\n"
        "请先运行 packaging/build.ps1（会生成 build/payload.zip），"
        "或单独运行 packaging/make_payload_zip.py。"
    )
print(f"[installer.spec] payload.zip = {PAYLOAD_ZIP} ({PAYLOAD_ZIP.stat().st_size} bytes)")

# payload.zip 作为 datas 内嵌到 onefile 根目录（wizard.resolve_payload_dir 读取 _MEIPASS/payload.zip）
datas = [(str(PAYLOAD_ZIP), ".")]


hiddenimports = [
    # 快捷方式使用 WScript.Shell COM（pywin32）；静态分析不一定覆盖，显式声明
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "win32com.shell",
]

# 安装器只需要标准库 + Tkinter + pywin32；把主程序才用的大依赖全部排除
excludes = [
    # 主程序 GUI/核心/采集/规则层都不需要（安装器只调用 installer 包）
    "social_secretary.app",
    "social_secretary.ui",
    "social_secretary.core",
    "social_secretary.collectors",
    "social_secretary.intel",
    "social_secretary.security",
    # Qt / 其它 GUI 绑定（本机 Qt 进程必崩且不需要）
    "PySide6",
    "PySide2",
    "PyQt5",
    "PyQt6",
    "shiboken6",
    "shiboken2",
    "qtpy",
    # 其它未使用的大包
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "IPython",
    "jedi",
    "pytest",
    "_pytest",
    "setuptools",
    "pip",
    "PIL.ImageTk",
]

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(SRC), str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=INSTALLER_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 安装向导；--silent 时用退出码/JSON 结果文件，不依赖控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
    version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
)

PACKAGING_INFO = {  # noqa: F841
    "installer_name": INSTALLER_NAME,
    "payload_zip": str(PAYLOAD_ZIP),
    "payload_zip_bytes": PAYLOAD_ZIP.stat().st_size,
    "entry": str(ENTRY),
    "python": sys.version,
}
print(f"[installer.spec] onefile installer = {INSTALLER_NAME}.exe")
