# 架构与安全边界

## 模块

- `src/social_secretary/collectors/qq/`：Windows UIA 只读 QQ 采集器；
- `src/social_secretary/collectors/mail/`：IMAP 只读 QQ 邮箱采集器；
- `src/social_secretary/core/`：SQLite 存储、去重/流水线、服务门面、摘要；
- `src/social_secretary/security/`：同意、凭据、日志脱敏、审计；
- `src/social_secretary/intel/`：本地规则、外部 AI 适配、草稿；
- `src/social_secretary/ui/`：Tkinter 桌面界面；
- `src/social_secretary/installer/`：脚本安装/卸载操作。

## 数据边界

- QQ/QQ 邮箱只读；没有发送/删除/撤回路径；
- 不读取 QQ 本地消息数据库；
- 外部 AI 默认关闭，需显式同意；
- 凭据只使用 Windows 凭据管理器；
- 日志/证据不写消息正文和凭据。

## QQ 采集的已知技术限制

Windows UI Automation 只暴露当前可见/可访问的 QQ 气泡；没有稳定的消息级 ID。采集器采用内容+时间桶+可见序号构造去重键，存在两类已知风险：

1. 合法相同正文消息被误去重（已通过可见序号/occurrence 缓解，但不能保证所有场景）；
2. 同一条消息在时间戳可见性变化时可能重复入库（A1 预检 FAIL）。

本仓库不声称已解决消息身份问题；不要将 QQ 摘要当作完整事实来源。
