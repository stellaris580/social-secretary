"""自研安装程序（PyInstaller **onefile** GUI）入口。

注意：安装器不经过 ``social_secretary.app``（避免拉入 GUI/核心层依赖），
直接调用 ``social_secretary.installer.wizard.main``；内嵌的应用 payload 位于
onefile 解包目录 ``sys._MEIPASS/payload``，由 wizard 自动识别。
"""

from __future__ import annotations

import sys

from social_secretary.installer.wizard import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
