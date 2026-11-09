"""PyInstaller 打包入口脚本。

不作为 ``social_secretary`` 包的一部分安装（放在 packaging/ 下），只给 PyInstaller
当 ``__main__`` 入口用：这样 ``--selftest`` / GUI / 采集 worker 子进程都走
``app.main()``，与 ``python -m social_secretary`` 行为一致。

冻结模式下的两点注意：
1. ``sys.executable`` 会指向 ``SocialSecretary.exe``，因此 app-core 用
   ``subprocess`` 启动 QQ 采集 worker（例如 ``SocialSecretary.exe --collector-worker``）
   时，子进程同样是打包好的程序，不依赖开发机 Python。
2. 若 worker 用 ``multiprocessing``（spawn）而不是 subprocess，必须在入口最早处
   调用 ``multiprocessing.freeze_support()``，否则冻结后会无限重入主程序。
   PyInstaller 官方要求该调用放在 ``if __name__ == "__main__":`` 的第一行。
"""

from __future__ import annotations

import multiprocessing
import sys

from social_secretary.app import main

if __name__ == "__main__":
    # 冻结版多进程支持（QQ 采集 worker 若使用 multiprocessing 时必须）
    multiprocessing.freeze_support()
    sys.exit(main())
