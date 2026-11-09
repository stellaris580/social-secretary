"""安全边界与对外导入面测试。

对应用户铁律：本地规则层零网络、只读、无发送能力；外部 AI 仅存在于单独模块且默认需同意。
"""

from __future__ import annotations

import inspect

from social_secretary.contracts import Classifier, DraftProvider, DraftKind, TodoExtractor
from social_secretary.intel import (
    ClassifierConfig,
    ConsentRequiredError,
    DRAFT_STYLES,
    ExternalAIError,
    ExternalLLMClient,
    LocalDraftProvider,
    ParsedDue,
    RuleClassifier,
    RuleTodoExtractor,
    TodoConfig,
    detect_intent,
    draft_styles,
    parse_due_text,
    redact_for_upload,
)
from social_secretary.intel import drafts as drafts_module
from social_secretary.intel import external_ai, rules, semantic as semantic_module
from social_secretary.intel.semantic import (
    SemanticAnalyzer,
    local_conversation_summary,
    local_daily_brief,
    local_semantic_draft,
)

FORBIDDEN_IN_LOCAL_MODULES = (
    "import urllib",
    "import requests",
    "from requests",
    "import http.client",
    "from http import client",
    "import socket",
    "import smtplib",
    "import aiohttp",
    "import httpx",
    "import websocket",
    "import pyautogui",
    "import uiautomation",
    "import keyring",
    "import PySide6",
    "from PySide6",
)


def test_local_modules_have_no_network_or_gui_imports():
    for module in (rules, drafts_module, semantic_module):
        source = inspect.getsource(module)
        for token in FORBIDDEN_IN_LOCAL_MODULES:
            assert token not in source, f"{module.__name__} 不应包含 {token}"


def test_semantic_has_no_sending_capability():
    source = inspect.getsource(semantic_module)
    for token in ("smtplib", "SendKeys", "pyautogui", "uiautomation", "发送消息"):
        assert token not in source, f"semantic 不应包含发送能力：{token}"


def test_external_ai_has_no_sending_capability():
    source = inspect.getsource(external_ai)
    for token in ("smtplib", "SendKeys", "pyautogui", "uiautomation"):
        assert token not in source, f"external_ai 不应包含发送/自动化能力：{token}"


def test_no_print_debug_leftover():
    for module in (rules, drafts_module, external_ai, semantic_module):
        source = inspect.getsource(module)
        assert "print(" not in source, f"{module.__name__} 不应留有 print 调试输出"


def test_protocol_conformance():
    assert isinstance(RuleClassifier(), Classifier)
    assert isinstance(RuleTodoExtractor(), TodoExtractor)
    assert isinstance(LocalDraftProvider(), DraftProvider)
    client = ExternalLLMClient(
        "https://api.example.com/v1", "m", "key", lambda: False, timeout=1
    )
    assert isinstance(client, DraftProvider)
    assert client.kind is DraftKind.EXTERNAL_LLM
    assert LocalDraftProvider().kind is DraftKind.LOCAL_RULES


def test_rule_tables_are_centralized_and_explainable():
    assert len(rules.IMPORTANT_SIGNAL_RULES) >= 5
    assert len(rules.FYI_SIGNAL_RULES) >= 5
    for rule in rules.IMPORTANT_SIGNAL_RULES + rules.FYI_SIGNAL_RULES:
        assert rule.name
        assert rule.reason
        assert isinstance(rule.weight, float)
        assert rule.pattern is not None


def test_public_import_surface():
    # 这些名字必须能从包根直接 import（app-core 集成口径）
    assert RuleClassifier is not None
    assert RuleTodoExtractor is not None
    assert LocalDraftProvider is not None
    assert draft_styles() == DRAFT_STYLES
    assert callable(detect_intent)
    assert callable(parse_due_text)
    assert callable(redact_for_upload)
    assert ParsedDue is not None
    assert ClassifierConfig is not None
    assert TodoConfig is not None
    assert issubclass(ConsentRequiredError, PermissionError)
    assert issubclass(ExternalAIError, RuntimeError)


def test_semantic_public_surface():
    """v0.2.0 语义接口：app-core 可直接从 intel.semantic import 这些名字。"""
    assert callable(local_conversation_summary)
    assert callable(local_daily_brief)
    assert callable(local_semantic_draft)
    assert SemanticAnalyzer is not None
    # 本地函数必须无参之外也可调用（空消息不崩溃）
    assert local_conversation_summary([])["provider"] == "local_rules"
    assert local_daily_brief([])["provider"] == "local_rules"

