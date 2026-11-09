"""自研 Windows 安装/卸载包（不依赖 Inno Setup / Qt）。

包含：
- ``operations``：安装/卸载核心逻辑，依赖（注册表/快捷方式/文件/进程）全部可注入，
  便于在无 Windows 注册表权限的沙箱里用 fake 做单元测试；
- ``wizard``：Tkinter 安装向导 + 无界面（--silent）CLI + 卸载确认流程。

安装器本体由 ``packaging/installer.spec`` 打成 PyInstaller **onefile** GUI 程序，
内嵌 ``dist/AI社交秘书/`` 整个 onedir 作为 payload；安装出来的程序再通过
``SocialSecretary.exe --uninstall`` 走 ``wizard.main`` 执行卸载。
"""

from __future__ import annotations

from .operations import (
    APP_DATA_DIR_NAME,
    APP_EXE_NAME,
    APP_ID,
    APP_NAME,
    APP_VERSION,
    UNINSTALL_KEY_PATH,
    InstallOptions,
    InstallResult,
    InstallerOperations,
    UninstallOptions,
    UninstallResult,
    build_uninstall_entries,
    default_data_dir,
    default_install_dir,
)

__all__ = [
    "APP_DATA_DIR_NAME",
    "APP_EXE_NAME",
    "APP_ID",
    "APP_NAME",
    "APP_VERSION",
    "UNINSTALL_KEY_PATH",
    "InstallOptions",
    "InstallResult",
    "InstallerOperations",
    "UninstallOptions",
    "UninstallResult",
    "build_uninstall_entries",
    "default_data_dir",
    "default_install_dir",
]
