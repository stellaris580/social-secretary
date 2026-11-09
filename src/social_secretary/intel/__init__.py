"""智能规则层：中文分类、待办提取、本地草稿、可选外部 AI 适配。

默认全部本地、零上传；外部 AI 只有在用户显式同意（consent_check 返回 True）时才会联网。

常用入口::

    from social_secretary.intel import (
        RuleClassifier, RuleTodoExtractor, LocalDraftProvider,
        draft_styles, ExternalLLMClient, ConsentRequiredError,
    )

也可以按模块导入：``social_secretary.intel.rules`` / ``.drafts`` / ``.external_ai``。
"""

from __future__ import annotations

from .drafts import DRAFT_STYLES, LocalDraftProvider, detect_intent, draft_styles
from .external_ai import (
    ConsentRequiredError,
    ExternalAIError,
    ExternalLLMClient,
    ExternalSummary,
    redact_for_upload,
)
from .rules import (
    ClassifierConfig,
    ParsedDue,
    RuleClassifier,
    RuleTodoExtractor,
    TodoConfig,
    parse_due_text,
)

__all__ = [
    # 分类 / 待办
    "RuleClassifier",
    "RuleTodoExtractor",
    "ClassifierConfig",
    "TodoConfig",
    "ParsedDue",
    "parse_due_text",
    # 本地草稿
    "LocalDraftProvider",
    "draft_styles",
    "DRAFT_STYLES",
    "detect_intent",
    # 外部 AI（可选，默认关）
    "ExternalLLMClient",
    "ExternalSummary",
    "ExternalAIError",
    "ConsentRequiredError",
    "redact_for_upload",
]
