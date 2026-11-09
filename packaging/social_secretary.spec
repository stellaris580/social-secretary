# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：AI 社交秘书（Windows 11, onedir, 无控制台）。

产物：``dist/AI社交秘书/SocialSecretary.exe`` + 同目录运行时依赖（onedir）。
为什么用 onedir 而不是 onefile：
- 启动快、不需要每次解压到 %TEMP%（沙箱/杀软环境更稳）；
- 便于安装包按目录安装、卸载时整目录删除；
- 便于校验"安装后文件是否完整"。
安装包另由 Inno Setup 编译（packaging/social_secretary.iss）。

技术栈与冻结模式说明（2026-10-31 Lead 决策）：
- UI 使用标准库 **Tkinter**，不打包 PySide6/Qt（本机 Qt 进程创建 QApplication 必崩，
  见 docs/delivery/build-and-install-verification.md；同时安装包体积从 ~120MB 降到 ~30MB）。
- QQ UIA 采集保留 uiautomation/comtypes，但由 app-core 放到**独立子进程**运行；
  冻结模式下 ``sys.executable`` 就是 SocialSecretary.exe，worker 子进程复用同一个 exe
  （``SocialSecretary.exe --collector-worker ...``），入口见 packaging/entrypoint.py。
- P0-16：正式 exe 需支持 ``--gui-e2e``（GUI 端到端验收）/邮件收件箱能力，spec 已显式
  声明 core / mail collector / Pillow / pywin32 等运行时依赖。
- 冻结模式 ``config.project_root()`` = exe 所在目录，因此 ``config/qq_ui_profile.json``
  必须位于 exe 同目录；datas 里的副本落在 _internal/，build.ps1 会额外复制到 dist 根目录。

构建方式（推荐用 packaging/build.ps1 统一设置环境变量）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean ^
        --distpath dist --workpath build packaging/social_secretary.spec
"""

import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821  (PyInstaller 注入的 SPECPATH)
ROOT = SPEC_DIR.parent
SRC = ROOT / "src"
ENTRY = SPEC_DIR / "entrypoint.py"
ICON = SPEC_DIR / "assets" / "social_secretary.ico"
VERSION_FILE = SPEC_DIR / "version_info.txt"

APP_EXE_NAME = "SocialSecretary"
DIST_NAME = "AI社交秘书"

# 把 src 加入模块搜索路径，保证 PyInstaller 能 import social_secretary。
sys.path.insert(0, str(SRC))

def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


# --- 运行时数据文件 ---------------------------------------------------------
datas: list[tuple[str, str]] = []

readme = ROOT / "README.md"
if readme.is_file():
    datas.append((str(readme), "."))

notice = SPEC_DIR / "NOTICE.txt"
if notice.is_file():
    datas.append((str(notice), "."))

license_txt = SPEC_DIR / "LICENSE.txt"
if license_txt.is_file():
    datas.append((str(license_txt), "."))

# QQ UIA 选择器配置（由 uia-qq 提供；存在才打包，缺失时程序按内置默认值运行）
qq_profile = ROOT / "config" / "qq_ui_profile.json"
if qq_profile.is_file():
    datas.append((str(qq_profile), "config"))

# 应用图标（GUI 窗口/托盘可从 assets 读取；exe 图标由 icon= 指定）
if ICON.is_file():
    datas.append((str(ICON), "assets"))

# 打包所有非 .py 的运行时资源（ui 的 qss/图片等），没有则跳过
ui_res = SRC / "social_secretary" / "ui" / "resources"
if ui_res.is_dir():
    for p in sorted(ui_res.rglob("*")):
        if p.is_file():
            datas.append((str(p), "assets/ui/" + str(p.relative_to(ui_res).parent).replace("\\", "/")))

# --- P0-16 修复：Codex 定制 Python 运行时的 ssl CA 数据文件 -------------------
# 本机 Python 的 ssl.py 补丁会在 SSLContext.__new__ 中强制加载 stdlib 同目录的
# codex-rejected-cas.pem；PyInstaller 不会自动打包该 .pem，导致冻结版所有 TLS
# （QQ 邮箱 IMAPS、HTTPS）create_default_context 抛 FileNotFoundError。
# 解决：把该文件作为数据文件放到 _MEIPASS 根目录（与冻结版 ssl 模块 __file__ 同目录）。
# 非 Codex 运行时的机器上没有该文件 → 安全跳过（不影响构建/运行）。
try:
    import ssl as _ssl_mod

    _codex_ca = Path(_ssl_mod.__file__).resolve().with_name("codex-rejected-cas.pem")
except Exception:  # noqa: BLE001 - 极端环境 import ssl 失败时跳过
    _codex_ca = Path("/nonexistent/codex-rejected-cas.pem")
if _codex_ca.is_file():
    datas.append((str(_codex_ca), "."))
    print(f"[spec] bundled codex SSL CA data: {_codex_ca} -> _MEIPASS")
else:
    print("[spec] codex-rejected-cas.pem not found; skip (non-Codex runtime, frozen TLS 不受影响)")

# --- 动态导入 / 插件式导入的模块 -------------------------------------------
hiddenimports: list[str] = [
    # app.run_selftest 用 importlib.import_module("social_secretary.ui") 动态导入
    "social_secretary.ui",
    # 核心层（ServiceAPI 实现；P0-16 起含邮件收件箱/同步等新方法，显式声明防懒 import 遗漏）
    "social_secretary.core",
    "social_secretary.core.service",
    # 冻结模式 QQ 采集 worker：app.py 收到 --collector-worker 后转发到
    # core.collector_worker.main；这里显式声明，避免懒 import 被分析遗漏。
    "social_secretary.core.collector_worker",
    "social_secretary.collectors.qq",
    "social_secretary.collectors.mail",
    "social_secretary.collectors.mail.collector",
    # keyring 的 Windows 后端是运行时按平台加载的，静态分析扫不到
    "keyring.backends.Windows",
    # 冻结版 --gui-e2e 验收（截图/窗口抓取）可能用到 Pillow 与 pywin32；
    # 这些模块体积小，显式声明保证 UI 端到端验收在冻结版可用。
    # P0-19：QQ worker 在冻结版子进程内**懒加载**探测/解析模块；PyInstaller 静态分析可能漏掉，
    # 导致安装版 QQ 一键读取/扫描出现 ImportError。这里显式收集 collectors.qq 全部子模块
    # （probe/profile/foreground/nodes/backend/bubbles/timeparse/whitelist/errors 等）。
    "PIL.Image",
    "PIL.ImageGrab",
    "win32gui",
    "win32ui",
    "win32con",
    "win32api",
]

# --- P0-19：QQ worker 懒加载子模块显式收集（防冻结版 ImportError） -----------
QQ_WORKER_SUBMODULES = [
    "social_secretary.collectors.qq.probe",
    "social_secretary.collectors.qq.profile",
    "social_secretary.collectors.qq.foreground",
    "social_secretary.collectors.qq.nodes",
    "social_secretary.collectors.qq.backend",
    "social_secretary.collectors.qq.bubbles",
    "social_secretary.collectors.qq.timeparse",
    "social_secretary.collectors.qq.whitelist",
    "social_secretary.collectors.qq.errors",
]
_qq_pkg = SRC / "social_secretary" / "collectors" / "qq"
if _qq_pkg.is_dir():
    try:
        from PyInstaller.utils.hooks import collect_submodules as _collect_qq  # noqa: PLC0415

        hiddenimports += _collect_qq("social_secretary.collectors.qq")
        print("[spec] 已 collect_submodules social_secretary.collectors.qq（QQ worker 懒加载模块）")
    except Exception as _exc:  # noqa: BLE001
        print(f"[spec] collect_submodules(collectors.qq) 失败，使用显式兜底列表: {_exc}")
for _m in QQ_WORKER_SUBMODULES:
    if _m not in hiddenimports:
        hiddenimports.append(_m)
print(f"[spec] QQ worker 子模块 hiddenimports 已就绪（{len(QQ_WORKER_SUBMODULES)} 项兜底）")

# --- v0.2.0：语义摘要/外部 AI 模块（Service 函数内 lazy import，防冻结版缺失） ------
for _m in ("social_secretary.intel.semantic", "social_secretary.intel.external_ai"):
    if _m not in hiddenimports:
        hiddenimports.append(_m)
print("[spec] 已显式加入 intel.semantic / intel.external_ai hiddenimports（v0.2.0 语义链路）")

# --- 一键 QQ 读取验收模块（--qq-quick-selftest）必须打包 ---------------------
_quick_selftest = SRC / "social_secretary" / "acceptance" / "qq_quick_selftest.py"
if _quick_selftest.is_file():
    _m = "social_secretary.acceptance.qq_quick_selftest"
    if _m not in hiddenimports:
        hiddenimports.append(_m)
    print(f"[spec] 已显式加入 hiddenimports: {_m}")
else:
    print("[spec] acceptance/qq_quick_selftest.py 不存在，跳过显式 hiddenimport（--qq-quick-selftest 未启用）")

# 若 Lead 新增了 social_secretary.acceptance（--gui-e2e 验收包），自动收集其全部子模块；
# 包尚不存在时跳过（构建不失败）。
_acceptance_pkg = SRC / "social_secretary" / "acceptance"
if _acceptance_pkg.is_dir():
    from PyInstaller.utils.hooks import collect_submodules  # noqa: PLC0415

    hiddenimports += collect_submodules("social_secretary.acceptance")
    print(f"[spec] 已收集 social_secretary.acceptance 子模块")
else:
    print("[spec] social_secretary.acceptance 暂不存在，跳过（P0-16 构建时如已新增会自动收集）")

# --- 排除不需要的大模块（减小体积）-----------------------------------------
# 技术栈决策（2026-10-31 Lead）：UI 改用标准库 Tkinter，**不再打包 PySide6/Qt**。
# 原因：本机任何 Qt 进程创建 QApplication 必崩（NahimicOSD.dll 注入，PySide6 6.12/6.8、
# PyQt5 均可复现，沙箱外也崩），而 Tkinter 正常；同时安装包从 ~120MB 降到 ~30MB。
# QQ UIA 采集仍保留 uiautomation/comtypes，但由 app-core 放到**独立子进程**运行，
# 避免 comtypes 与 GUI 同进程。
excludes: list[str] = [
    # Qt / 其它 GUI 绑定：本程序不使用，全部排除（防误打包 + 显著减小体积）
    "PySide6",
    "PySide2",
    "PyQt5",
    "PyQt6",
    "shiboken6",
    "shiboken2",
    "qtpy",
    # 已归档的 PySide6 实验版 UI（源码保留在 social_secretary/ui_qt_experimental，不参与打包）
    "social_secretary.ui_qt_experimental",
    # 标准库里桌面程序用不到的
    "unittest",
    "pydoc",
    "doctest",
    "pytest",
    "_pytest",
    "setuptools",
    "pip",
    # 未使用的科学计算/交互式大包
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "IPython",
    "jedi",
    "PIL.ImageTk",
]
# 注意：
# - 不要排除 tkinter（UI 技术栈）；
# - 不要排除 uiautomation / comtypes / pywin32（QQ 采集子进程使用）。


a = Analysis(  # noqa: F821  (PyInstaller 注入)
    [str(ENTRY)],  # PyInstaller 6.x 要求 scripts 是列表（传字符串会被逐字符迭代）
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
    [],
    exclude_binaries=True,
    name=APP_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
    version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=DIST_NAME,
)

# 构建信息，供 build.ps1 写证据（PyInstaller 会忽略未知的顶层变量）
PACKAGING_INFO = {  # noqa: F841
    "entry": rel(ENTRY),
    "icon": rel(ICON) if ICON.is_file() else None,
    "version_file": rel(VERSION_FILE) if VERSION_FILE.is_file() else None,
    "datas": [[src, dst] for src, dst in datas],
    "app_exe_name": APP_EXE_NAME,
    "dist_name": DIST_NAME,
    "python": sys.version,
}
print(f"[spec] datas={len(datas)} hiddenimports={hiddenimports} excludes={len(excludes)}")
