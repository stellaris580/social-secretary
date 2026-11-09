"""页面包：今日摘要、收件箱、待办、回复草稿、消息来源、聊天导入（实验）、设置与隐私。"""

from __future__ import annotations

from .drafts_page import DraftsPage
from .import_page import ImportPage
from .inbox_page import InboxPage
from .settings_page import SettingsPage
from .sources_page import SourcesPage
from .summary_page import SummaryPage
from .todo_page import TodoPage

__all__ = [
    "DraftsPage",
    "ImportPage",
    "InboxPage",
    "SettingsPage",
    "SourcesPage",
    "SummaryPage",
    "TodoPage",
]
