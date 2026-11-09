"""QQ NT 只读 UIA 采集器。

公开接口：
- ``QqMessageSource``: 实现 ``contracts.MessageSource`` 的只读消息源；
- ``load_profile`` / ``QqUiProfile`` / ``QqUiMode``: 选择器配置与模式；
- ``probe`` 子模块 / ``tools/qq_probe.py``: 只读结构探针。

安全边界：本包不存在任何发送消息、点击发送按钮、向消息编辑框输入文本的代码路径。
导入本包不会加载 uiautomation（真实后端延迟初始化），离线测试可安全运行。
"""

from __future__ import annotations

from .collector import ChatTarget, QqMessageSource, QqRunStats
from .errors import (
    QqClosed,
    QqError,
    QqForegroundNotAcknowledged,
    QqHeaderMismatch,
    QqHeaderUnavailable,
    QqNotRunning,
    QqOpenChatFailed,
    QqProfileError,
    QqSafetyViolation,
    QqTreeNotReady,
    QqTreeUnavailable,
    QqUnavailable,
    QqUnsupportedMode,
    QqWhitelistViolation,
    QqWindowLost,
    QqWindowUnavailable,
)
from .profile import QqUiMode, QqUiProfile, load_profile

__all__ = [
    "ChatTarget",
    "QqMessageSource",
    "QqRunStats",
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
    "QqUiMode",
    "QqUiProfile",
    "load_profile",
]
