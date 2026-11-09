"""Tkinter 桌面界面入口。

对外暴露两个函数（由 ``social_secretary.app.run_gui`` 调用）：

- ``create_main_window(service) -> AppWindow``：创建主窗口对象（默认隐藏窗口，不进入
  事件循环、不弹模态框、不关闭服务），便于测试与打包自检。
- ``run_ui(service) -> int``：创建窗口并显示、调度首次运行同意向导、进入 Tk 主循环，
  返回 0。服务的关闭由调用方负责（``app.py`` 的 ``finally: service.shutdown()``）。

技术选型说明：界面使用 Python 标准库 Tkinter（不使用 PySide6/PyQt）。
原因：目标用户本机安装的 Dell「Alienware Sound Center / Nahimic OSD」会向 Qt GUI 进程注入
DLL，导致任何 QApplication 初始化直接崩溃；Tkinter 不触发该注入，无需额外依赖，安装包更小。
实验性的 PySide6 版本归档在 ``social_secretary.ui_qt_experimental``（不参与打包）。
"""

from __future__ import annotations

from .main_window import AppWindow

__all__ = ["AppWindow", "create_main_window", "run_ui"]


def create_main_window(service, *, root=None) -> AppWindow:
    """创建主窗口对象（窗口默认隐藏，调用方需要时再 show）。

    ``root`` 仅供测试注入共享的 Tk 根窗口；业务代码不要传。
    """
    window = AppWindow(service, root=root)
    window.root.withdraw()
    return window


def run_ui(service, *, _mainloop=None) -> int:
    """启动桌面界面，进入 Tk 主循环并返回退出码（0）。

    ``_mainloop`` 仅供测试注入（默认调用 ``root.mainloop``），业务代码不要传。
    """
    window = create_main_window(service)
    window.show()
    window.root.after(0, window.maybe_show_first_run)
    if _mainloop is not None:
        return int(_mainloop())
    window.root.mainloop()
    return 0