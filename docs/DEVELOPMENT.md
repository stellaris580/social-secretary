# 开发与构建

## 环境

- Windows 11
- Python 3.12（仅开发者需要；最终用户使用 Release 不需要 Python）
- 不要求管理员权限

## 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 测试

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest
```

测试使用 faketime/fake IMAP/合成 UI 数据，不连接真实 QQ、邮箱或付费 AI。Windows GUI 测试需要桌面会话；没有桌面时应标记跳过，不能把跳过说成通过。

## 运行

```powershell
.\.venv\Scripts\python.exe -m social_secretary
```

首次运行需要用户在界面中自行配置账号；不要提交真实聊天、邮箱或凭据用于开发。

## 构建 Windows 包

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Clean
```

产物在 `dist/`：

- `dist/AI社交秘书/SocialSecretary.exe`（onedir）
- `dist/installer/...安装包.zip`
- `dist/SocialSecretary-...-portable.zip`

构建脚本需要 PyInstaller；安装器不依赖管理员权限。

## 隐私规则

- 不把真实聊天/邮件/截图/数据库/凭据提交到 Git；
- 新增测试使用合成数据；
- 发现凭据泄露请按 `SECURITY.md` 报告；
- 不要为开发方便削弱只读/同意/发送边界。
