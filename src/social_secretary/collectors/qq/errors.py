"""QQ UIA 采集器错误类型。

所有错误都带一个稳定的 ``code`` 字符串，方便核心层/UI 层做降级与用户提示，
而不依赖异常类名。错误消息只允许包含「用户自己配置的会话白名单名称」或
结构化信息（计数、长度、类名），**禁止包含聊天正文或非白名单会话的真实昵称**。
"""

from __future__ import annotations

from typing import Any


class QqError(RuntimeError):
    """QQ UIA 采集器所有可预期错误的基类。"""

    code: str = "qq_error"
    default_message: str = "QQ 读取失败"

    def __init__(self, message: str = "", *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message: str = message or self.default_message
        self.detail: dict[str, Any] = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


class QqNotRunning(QqError):
    code = "qq_not_running"
    default_message = "未检测到 QQ 进程，请先手动启动并登录 QQ"


class QqWindowUnavailable(QqError):
    code = "qq_window_unavailable"
    default_message = "未找到 QQ 主面板，请确认 QQ 已登录且主面板可正常打开"


class QqWindowLost(QqError):
    code = "window_lost"
    default_message = "读取过程中 QQ 窗口关闭或 UIA 树不可用"


class QqTreeUnavailable(QqError):
    code = "tree_unavailable"
    default_message = "无法读取 QQ 窗口的 UIA 控件树（可能窗口在后台或尚未完成渲染）"


class QqTreeNotReady(QqError):
    code = "tree_not_ready"
    default_message = (
        "QQ 的 UIA 无障碍树尚未就绪（可见节点太少：QQ 可能不在前台或树未重建）。"
        "本次读取不会被当作“没有新消息”，请重试；若持续失败，请在 Lead 授权后运行 "
        "tools/qq_probe.py 校准。"
    )


class QqForegroundNotAcknowledged(QqError):
    code = "foreground_not_acknowledged"
    default_message = "读取 QQ 需要短暂占用前台窗口，必须先由上层向用户确认（foreground_ack=True）"


class QqWhitelistViolation(QqError):
    code = "whitelist_violation"
    default_message = "目标会话不在白名单内，已拒绝读取（只读采集只允许读取用户授权的会话）"


class QqHeaderUnavailable(QqError):
    code = "header_unavailable"
    default_message = "未找到聊天头部联系人控件，无法校验当前会话，已取消读取"


class QqHeaderMismatch(QqError):
    code = "header_mismatch"
    default_message = "当前打开的会话与目标会话不一致，已取消读取（避免读错会话）"


class QqOpenChatFailed(QqError):
    code = "open_chat_failed"
    default_message = "无法打开目标会话，请确认该会话出现在 QQ 最近会话或搜索结果中"


class QqUnsupportedMode(QqError):
    code = "unsupported_mode"
    default_message = "无法识别 QQ 界面模式（经典/效率），请重新运行 qq_probe 校准"


class QqSafetyViolation(QqError):
    code = "safety_violation"
    default_message = "安全校验失败，已停止输入类操作（不会向消息编辑框输入任何内容）"


class QqProfileError(QqError):
    code = "profile_error"
    default_message = "QQ UIA 选择器配置缺失或格式错误，请重新运行 qq_probe 校准"


class QqUnavailable(QqError):
    """UIA 后端不可用（uiautomation 未安装 / COM 初始化失败等）。"""

    code = "uia_unavailable"
    default_message = "UIA 自动化环境不可用"


class QqClosed(QqError):
    code = "collector_closed"
    default_message = "QQ 采集器已关闭"


__all__ = [
    "QqError",
    "QqNotRunning",
    "QqWindowUnavailable",
    "QqWindowLost",
    "QqTreeUnavailable",
    "QqTreeNotReady",
    "QqForegroundNotAcknowledged",
    "QqWhitelistViolation",
    "QqHeaderUnavailable",
    "QqHeaderMismatch",
    "QqOpenChatFailed",
    "QqUnsupportedMode",
    "QqSafetyViolation",
    "QqProfileError",
    "QqUnavailable",
    "QqClosed",
]
