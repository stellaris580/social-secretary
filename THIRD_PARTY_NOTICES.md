# 第三方组件与许可声明

本项目原创代码按 MIT License 发布。运行时/构建期使用了以下第三方组件；它们**没有**被声明为本项目原创代码。

| 组件 | 用途 | 许可证 | 上游 |
|---|---|---|---|
| Python 3.12 | 运行时 | PSF License | https://www.python.org/ |
| Tkinter / Tcl/Tk | 桌面界面 | PSF / Tcl/Tk License | https://www.tcl.tk/ |
| uiautomation | Windows UI Automation 只读读取 | Apache-2.0 | https://pypi.org/project/uiautomation/ |
| comtypes | uiautomation 的 COM 依赖 | MIT | https://pypi.org/project/comtypes/ |
| pywin32 | Windows API 绑定 | PSF License | https://pypi.org/project/pywin32/ |
| keyring | Windows 凭据管理器访问 | MIT | https://pypi.org/project/keyring/ |
| cryptography | 凭据加密/安全原语 | Apache-2.0 / BSD-3-Clause | https://pypi.org/project/cryptography/ |
| Pillow | 构建期图标/截图处理 | HPND | https://pypi.org/project/Pillow/ |
| PyInstaller | 构建 Windows 包（构建期） | GPLv2-or-later with special exception | https://pyinstaller.org/ |

## 重要说明

- 本项目**不包含**腾讯/QQ 客户端代码，也不包含 QQ 自动化参考仓库的源代码；
- QQ UIA 选择器和交互方案参考了公开的 `treers2/qq-operations`（MIT）等资料；若未来直接复制其代码，必须在该代码文件中保留原版权声明和 MIT 许可文本；
- 分发本项目二进制包时，随包的 `NOTICE.txt` 保留第三方组件声明；
- 如果上游许可证发生变化，以对应版本的上游许可证文本为准；无法确认许可证的代码不得合并。
