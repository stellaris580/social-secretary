"""ExternalLLMClient 测试：全部使用 fake urlopen，绝不真实联网。"""

from __future__ import annotations

import json
import logging
import urllib.error

import pytest

import social_secretary.intel.external_ai as external_ai
from social_secretary.contracts import (
    ConsentScope,
    DraftContext,
    DraftKind,
    DraftProvider,
    ExternalAIConfig,
)
from social_secretary.intel.external_ai import (
    ConsentRequiredError,
    ExternalAIError,
    ExternalLLMClient,
    ExternalSummary,
    redact_for_upload,
)

MODEL = "test-model"
API_KEY = "sk-testplaceholder1234567890"
BASE_URL = "https://api.example.com/v1"


# ---------------------------------------------------------------------------
# fake urlopen
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        if isinstance(payload, bytes):
            self._raw = payload
        else:
            self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status

    def read(self, *args, **kwargs) -> bytes:
        return self._raw

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeUrlopen:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):
        self.calls.append({"request": request, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


@pytest.fixture
def install_fake(monkeypatch):
    def _install(responses) -> FakeUrlopen:
        opener = FakeUrlopen(responses)
        monkeypatch.setattr(external_ai.urllib.request, "urlopen", opener)
        return opener

    return _install


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _client(
    *,
    consent: bool = True,
    consent_fn=None,
    base_url: str = BASE_URL,
    model: str = MODEL,
    api_key: str = API_KEY,
    timeout: float = 5.0,
    max_retries: int = 2,
    **kwargs,
) -> tuple[ExternalLLMClient, list[float]]:
    sleeps: list[float] = []
    client = ExternalLLMClient(
        base_url,
        model,
        api_key,
        consent_fn or (lambda: consent),
        timeout=timeout,
        max_retries=max_retries,
        sleep=lambda seconds: sleeps.append(seconds),
        **kwargs,
    )
    return client, sleeps


def _success(content: str = "好的，我稍后回复你。") -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _context(*, style: str = "简洁", recent=()) -> DraftContext:
    return DraftContext(conversation_title="项目群", recent_messages=tuple(recent), style=style)


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(BASE_URL + "/chat/completions", status, "error", {}, None)


# ---------------------------------------------------------------------------
# 同意门控
# ---------------------------------------------------------------------------

def test_no_consent_raises_before_network(install_fake, make_message):
    opener = install_fake([])  # 若发起网络请求会 IndexError，测试即失败
    client, _ = _client(consent=False)
    with pytest.raises(ConsentRequiredError):
        client.propose(make_message("明天下午3点开会"), _context())
    assert opener.calls == []


def test_consent_check_exception_fails_closed(install_fake, make_message):
    opener = install_fake([])

    def boom() -> bool:
        raise RuntimeError("consent backend broken")

    client, _ = _client(consent_fn=boom)
    with pytest.raises(ConsentRequiredError):
        client.propose(make_message("明天下午3点开会"), _context())
    assert opener.calls == []


def test_consent_checked_before_config(install_fake, make_message):
    install_fake([])
    client, _ = _client(consent=False, api_key="")
    with pytest.raises(ConsentRequiredError):
        client.propose(make_message("明天开会"), _context())


# ---------------------------------------------------------------------------
# 请求格式与返回
# ---------------------------------------------------------------------------

def test_propose_success_and_request_format(install_fake, make_message):
    opener = install_fake([_success()])
    client, _ = _client()
    message = make_message("明天下午3点开会吗", sender="李雷")
    drafts = client.propose(message, _context(style="礼貌"))

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.kind is DraftKind.EXTERNAL_LLM
    assert draft.requires_external_consent is True
    assert draft.model_label == MODEL
    assert draft.text == "好的，我稍后回复你。"
    assert draft.message_id == message.id
    assert draft.style == "礼貌"
    assert draft.created_at is not None and draft.created_at.tzinfo is not None

    call = opener.calls[0]
    request = call["request"]
    assert request.full_url == BASE_URL + "/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert call["timeout"] == 5.0

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == MODEL
    assert isinstance(payload["messages"], list) and len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    user_content = payload["messages"][1]["content"]
    assert "明天下午3点开会吗" in user_content
    assert "礼貌" in user_content
    assert "项目群" in user_content


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.example.com/v1", "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com/v1/", "https://api.example.com/v1/chat/completions"),
        (
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/chat/completions",
        ),
    ],
)
def test_base_url_variants(install_fake, make_message, base_url, expected):
    opener = install_fake([_success()])
    client, _ = _client(base_url=base_url)
    client.propose(make_message("在吗"), _context())
    assert opener.calls[0]["request"].full_url == expected


def test_content_list_format_is_supported(install_fake, make_message):
    install_fake([{"choices": [{"message": {"content": [{"type": "text", "text": "收到，稍后回复。"}]}}]}])
    client, _ = _client()
    drafts = client.propose(make_message("在吗"), _context())
    assert drafts[0].text == "收到，稍后回复。"


# ---------------------------------------------------------------------------
# 数据最小化 + 脱敏
# ---------------------------------------------------------------------------

def test_upload_is_minimized_and_redacted(install_fake, make_message):
    opener = install_fake([_success()])
    secrets = (
        "联系我 13800138000 或 test@example.com 或 https://secret.example.com/x "
        "身份证 110101199001011234 "
    )
    long_text = secrets + "很长的内容" * 100
    recent = (
        make_message("第一条上下文", message_id="m1", sender="甲"),
        make_message("第二条上下文", message_id="m2", sender="乙"),
        make_message("第三条上下文", message_id="m3", sender="丙"),
        make_message("当前消息", message_id="msg-1", sender="李雷"),
    )
    message = make_message(long_text, message_id="msg-1", sender="李雷")
    client, _ = _client(max_context_messages=2, max_message_chars=80)
    client.propose(message, _context(recent=recent))

    payload = json.loads(opener.calls[0]["request"].data.decode("utf-8"))
    user_content = payload["messages"][1]["content"]

    assert "13800138000" not in user_content
    assert "test@example.com" not in user_content
    assert "https://secret.example.com/x" not in user_content
    assert "110101199001011234" not in user_content
    assert "[电话已脱敏]" in user_content
    # 只保留最近 2 条上下文
    assert "第一条上下文" not in user_content
    assert "第二条上下文" in user_content
    assert "第三条上下文" in user_content
    # 当前消息被截断（脱敏后 80 字 + 省略号）
    latest_line = [line for line in user_content.splitlines() if line.startswith("【最新消息】")][0]
    assert latest_line.endswith("…")
    assert len(latest_line) <= 80 + len("【最新消息】") + 2


def test_recent_messages_default_limit(install_fake, make_message):
    opener = install_fake([_success()])
    recent = tuple(make_message(f"上下文{i}", message_id=f"m{i}") for i in range(5))
    client, _ = _client()
    client.propose(make_message("当前消息", message_id="msg-1"), _context(recent=recent))
    payload = json.loads(opener.calls[0]["request"].data.decode("utf-8"))
    user_content = payload["messages"][1]["content"]
    assert "上下文4" in user_content and "上下文3" in user_content
    assert "上下文2" not in user_content and "上下文0" not in user_content


# ---------------------------------------------------------------------------
# 日志零正文
# ---------------------------------------------------------------------------

def test_logs_contain_no_message_or_response_text(install_fake, make_message, caplog):
    caplog.set_level(logging.DEBUG, logger="social_secretary.intel.external_ai")
    api_key = "sk-testplaceholder1234567890"
    install_fake([_success("回复正文绝密ABC")])
    client, _ = _client(api_key=api_key)
    client.propose(make_message("私密消息绝密XYZ 13800138000"), _context())

    log_text = caplog.text
    assert "绝密XYZ" not in log_text
    assert "13800138000" not in log_text
    assert "回复正文绝密ABC" not in log_text
    assert api_key not in log_text
    assert "Authorization" not in log_text
    assert "Bearer" not in log_text


# ---------------------------------------------------------------------------
# 错误处理 / 重试
# ---------------------------------------------------------------------------

def test_http_401_raises_without_retry(install_fake, make_message):
    opener = install_fake([_http_error(401)])
    client, sleeps = _client(max_retries=2)
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert "401" in str(excinfo.value)
    assert excinfo.value.code == "http_error"
    assert excinfo.value.http_status == 401
    assert excinfo.value.attempts == 1
    assert len(opener.calls) == 1
    assert sleeps == []
    assert client.last_attempts == 1
    assert client.last_http_status == 401


def test_http_500_retries_then_succeeds(install_fake, make_message):
    opener = install_fake([_http_error(500), _http_error(502), _success()])
    client, sleeps = _client(max_retries=2)
    drafts = client.propose(make_message("明天开会"), _context())
    assert drafts[0].text
    assert len(opener.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_http_500_exhausted_raises(install_fake, make_message):
    opener = install_fake([_http_error(500), _http_error(500), _http_error(500)])
    client, sleeps = _client(max_retries=2)
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert "500" in str(excinfo.value)
    assert excinfo.value.code == "http_error"
    assert excinfo.value.http_status == 500
    assert excinfo.value.attempts == 3
    assert len(opener.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_network_error_retries_then_raises(install_fake, make_message):
    opener = install_fake(
        [urllib.error.URLError("boom"), urllib.error.URLError("boom"), urllib.error.URLError("boom")]
    )
    client, _ = _client(max_retries=2)
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert "网络" in str(excinfo.value)
    assert excinfo.value.code == "network_error"
    assert excinfo.value.attempts == 3
    assert len(opener.calls) == 3


def test_invalid_json_raises(install_fake, make_message):
    install_fake([b"not-a-json"])
    client, _ = _client()
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert "JSON" in str(excinfo.value)
    assert excinfo.value.code == "invalid_response"


def test_empty_choices_raises(install_fake, make_message):
    install_fake([{"choices": []}])
    client, _ = _client()
    with pytest.raises(ExternalAIError):
        client.propose(make_message("明天开会"), _context())


def test_empty_content_raises(install_fake, make_message):
    install_fake([{"choices": [{"message": {"content": "   "}}]}])
    client, _ = _client()
    with pytest.raises(ExternalAIError):
        client.propose(make_message("明天开会"), _context())


@pytest.mark.parametrize(
    ("base_url", "model", "api_key", "keyword", "expected_code"),
    [
        ("", MODEL, API_KEY, "base_url", "config_missing_base_url"),
        (BASE_URL, "", API_KEY, "模型", "config_missing_model"),
        (BASE_URL, MODEL, "", "API Key", "config_missing_api_key"),
    ],
)
def test_incomplete_config_raises(
    install_fake, make_message, base_url, model, api_key, keyword, expected_code
):
    opener = install_fake([])
    client, _ = _client(base_url=base_url, model=model, api_key=api_key)
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert keyword in str(excinfo.value)
    assert excinfo.value.code == expected_code
    assert opener.calls == []


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

def test_test_connection_without_consent(install_fake):
    opener = install_fake([])
    client, _ = _client(consent=False)
    check = client.test_connection()
    assert check.ok is False
    assert "同意" in check.detail
    assert opener.calls == []


def test_test_connection_missing_config(install_fake):
    opener = install_fake([])
    client, _ = _client(api_key="")
    check = client.test_connection()
    assert check.ok is False
    assert "配置" in check.detail
    assert opener.calls == []


def test_test_connection_ok(install_fake):
    opener = install_fake([{"data": [{"id": MODEL}]}])
    client, _ = _client(timeout=7.0)
    check = client.test_connection()
    assert check.ok is True
    assert check.latency_ms is not None and check.latency_ms >= 0
    assert check.discovered.get("model") == MODEL
    call = opener.calls[0]
    assert call["request"].get_method() == "GET"
    assert call["request"].full_url == BASE_URL + "/models"
    assert call["timeout"] == 7.0


@pytest.mark.parametrize(
    ("status", "keyword"),
    [(401, "401"), (403, "403"), (404, "404"), (429, "429"), (500, "500")],
)
def test_test_connection_http_errors(install_fake, status, keyword):
    install_fake([_http_error(status)])
    client, _ = _client()
    check = client.test_connection()
    assert check.ok is False
    assert keyword in check.detail
    assert check.latency_ms is not None


def test_test_connection_network_error(install_fake):
    install_fake([urllib.error.URLError("down")])
    client, _ = _client()
    check = client.test_connection()
    assert check.ok is False
    assert "网络" in check.detail


# ---------------------------------------------------------------------------
# 其他安全细节
# ---------------------------------------------------------------------------

def test_repr_hides_api_key():
    client, _ = _client()
    text = repr(client)
    assert API_KEY not in text
    assert "***" in text


def test_is_protocol():
    client, _ = _client()
    assert isinstance(client, DraftProvider)
    assert client.kind is DraftKind.EXTERNAL_LLM


@pytest.mark.parametrize(
    ("text", "sensitive", "marker"),
    [
        ("电话 13800138000 请回电", "13800138000", "[电话已脱敏]"),
        ("邮箱 test@example.com", "test@example.com", "[邮箱已脱敏]"),
        ("链接 https://a.example.com/b", "https://a.example.com/b", "[链接已脱敏]"),
        ("身份证 110101199001011234", "110101199001011234", "[证件号已脱敏]"),
        ("密钥 sk-testplaceholder1234567890", "sk-testplaceholder1234567890", "[密钥已脱敏]"),
    ],
)
def test_redact_for_upload(text, sensitive, marker):
    redacted = redact_for_upload(text)
    assert sensitive not in redacted
    assert marker in redacted
    assert redact_for_upload("") == ""


def test_consent_check_must_be_callable():
    with pytest.raises(TypeError):
        ExternalLLMClient(BASE_URL, MODEL, API_KEY, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 核心层集成口径：ExternalAIConfig + CredentialStore + ConsentManager
# ---------------------------------------------------------------------------

class _FakeCredentialStore:
    def __init__(self, secrets: dict[str, str] | None = None, *, raises: bool = False) -> None:
        self._secrets = dict(secrets or {})
        self._raises = raises

    def get_secret(self, key: str) -> str | None:
        if self._raises:
            raise RuntimeError("credential backend unavailable")
        return self._secrets.get(key)


class _FakeConsentManager:
    def __init__(self, granted: bool) -> None:
        self.granted = bool(granted)

    def is_granted(self, scope) -> bool:
        return scope in (ConsentScope.EXTERNAL_AI, ConsentScope.EXTERNAL_AI.value) and self.granted


def _config() -> ExternalAIConfig:
    return ExternalAIConfig(base_url=BASE_URL, model=MODEL, key_ref="external_ai")


def test_core_style_construction_works(install_fake, make_message):
    opener = install_fake([_success()])
    client = ExternalLLMClient(
        config=_config(),
        credential_store=_FakeCredentialStore({"external_ai": API_KEY}),
        consent_manager=_FakeConsentManager(True),
        timeout=5.0,
        sleep=lambda seconds: None,
    )
    drafts = client.propose(make_message("明天下午3点开会"), _context())
    assert drafts[0].text
    request = opener.calls[0]["request"]
    assert request.full_url == BASE_URL + "/chat/completions"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"


def test_core_attempt_order_like_service(install_fake, make_message):
    """复刻 app-core ``service._make_external_provider`` 的 kwargs 尝试顺序。"""
    opener = install_fake([_success()])
    config = _config()
    consent = _FakeConsentManager(True)
    credentials = _FakeCredentialStore({"external_ai": API_KEY})
    attempts = (
        {"config": config, "credential_store": credentials, "consent_manager": consent},
        {"config": config, "credential_store": credentials},
        {"config": config},
        {},
    )
    client = None
    for kwargs in attempts:
        try:
            client = ExternalLLMClient(**kwargs)
            break
        except Exception:  # noqa: BLE001 - 与核心层一致：接口不匹配就试下一种
            continue
    assert client is not None
    assert client.model == MODEL
    assert client.propose(make_message("明天开会"), _context())[0].text
    assert opener.calls


def test_core_style_construction_without_consent_rejected(install_fake):
    install_fake([])
    with pytest.raises(TypeError):
        ExternalLLMClient(config=_config(), credential_store=_FakeCredentialStore({}))


def test_noargs_construction_rejected():
    with pytest.raises(TypeError):
        ExternalLLMClient()


def test_core_style_missing_secret_is_not_usable(install_fake, make_message):
    opener = install_fake([])
    client = ExternalLLMClient(
        config=_config(),
        credential_store=_FakeCredentialStore({}),
        consent_manager=_FakeConsentManager(True),
    )
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert "API Key" in str(excinfo.value)
    assert opener.calls == []


def test_core_style_credential_store_failure_fails_closed(install_fake, make_message):
    install_fake([])
    client = ExternalLLMClient(
        config=_config(),
        credential_store=_FakeCredentialStore(raises=True),
        consent_manager=_FakeConsentManager(True),
    )
    with pytest.raises(ExternalAIError) as excinfo:
        client.propose(make_message("明天开会"), _context())
    assert "API Key" in str(excinfo.value)


def test_core_style_denied_consent_blocks_network(install_fake, make_message):
    opener = install_fake([])
    client = ExternalLLMClient(
        config=_config(),
        credential_store=_FakeCredentialStore({"external_ai": API_KEY}),
        consent_manager=_FakeConsentManager(False),
    )
    with pytest.raises(ConsentRequiredError):
        client.propose(make_message("明天开会"), _context())
    assert opener.calls == []


# ---------------------------------------------------------------------------
# 诊断字段与实验性摘要 summarize()
# ---------------------------------------------------------------------------

def _summary_response(
    content: str = "合成摘要：明天有测试任务，需要按时提交。",
    *,
    prompt_tokens: int = 120,
    completion_tokens: int = 18,
) -> dict:
    return {
        "model": MODEL,
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def test_propose_records_diagnostics(install_fake, make_message):
    install_fake([_summary_response()])
    client, _ = _client()
    client.propose(make_message("明天开会"), _context())
    assert client.last_usage == {
        "prompt_tokens": 120,
        "completion_tokens": 18,
        "total_tokens": 138,
    }
    assert client.last_http_status == 200
    assert client.last_attempts == 1
    assert isinstance(client.last_latency_ms, int)
    assert client.last_response_model == MODEL


def test_summarize_requires_constructor_consent(install_fake):
    opener = install_fake([])
    client, _ = _client(consent=False)
    with pytest.raises(ConsentRequiredError):
        client.summarize(["【合成】明天下午3点测试"])
    assert opener.calls == []


def test_summarize_per_call_consent_blocked(install_fake):
    opener = install_fake([])
    client, _ = _client()
    with pytest.raises(ConsentRequiredError) as excinfo:
        client.summarize(["【合成】明天下午3点测试"], consent_check=lambda: False)
    assert excinfo.value.code == "consent_required"
    assert opener.calls == []


def test_summarize_per_call_consent_exception_fails_closed(install_fake):
    opener = install_fake([])
    client, _ = _client()

    def broken() -> bool:
        raise RuntimeError("consent ui crashed")

    with pytest.raises(ConsentRequiredError):
        client.summarize(["【合成】测试"], consent_check=broken)
    assert opener.calls == []


def test_summarize_success_and_payload(install_fake):
    opener = install_fake([_summary_response()])
    client, _ = _client()
    summary = client.summarize(
        ["【合成】明天下午3点前提交测试报告", "【合成】收到，谢谢"],
        consent_check=lambda: True,
    )
    assert isinstance(summary, ExternalSummary)
    assert summary.experimental is True
    assert summary.requires_external_consent is True
    assert summary.model_label == MODEL
    assert summary.text.startswith("合成摘要")
    assert summary.prompt_tokens == 120
    assert summary.completion_tokens == 18
    assert summary.total_tokens == 138
    assert isinstance(summary.latency_ms, int)
    assert client.last_response_model == MODEL

    request = opener.calls[0]["request"]
    assert request.full_url == BASE_URL + "/chat/completions"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == MODEL
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    user_content = payload["messages"][1]["content"]
    assert "实验性" in payload["messages"][0]["content"]
    assert "明天下午3点前提交测试报告" in user_content
    assert "收到，谢谢" in user_content


def test_summarize_accepts_mixed_input_forms(install_fake, make_message):
    opener = install_fake([_summary_response()])
    client, _ = _client()
    message = make_message("【合成】我来处理报告", sender="测试同事A", message_id="m-1")
    client.summarize(
        [
            message,
            {"sender": "合成联系人B", "is_self": False, "text": "【合成】明天开会"},
            {"is_self": True, "text": "【合成】好的"},
            "【合成】纯字符串消息",
        ]
    )
    payload = json.loads(opener.calls[0]["request"].data.decode("utf-8"))
    user_content = payload["messages"][1]["content"]
    assert "测试同事A" in user_content
    assert "合成联系人B" in user_content
    assert "我：" in user_content
    assert "纯字符串消息" in user_content


def test_summarize_minimizes_and_redacts(install_fake):
    opener = install_fake([_summary_response()])
    client, _ = _client()
    items = [f"消息{i}：" + "合成内容" * 60 for i in range(15)]
    items.append("隐私样例 13800138000 邮箱 test@example.com")
    client.summarize(items)
    payload = json.loads(opener.calls[0]["request"].data.decode("utf-8"))
    user_content = payload["messages"][1]["content"]
    # 只保留最近 SUMMARY_MAX_MESSAGES(10) 条
    assert "消息0" not in user_content
    assert "消息5" not in user_content
    assert "消息14" in user_content
    # 逐条截断到 200 字（带省略号）
    line = [item for item in user_content.splitlines() if item.startswith("对方：消息14")][0]
    assert line.endswith("…")
    assert len(line) <= len("对方：") + 200 + 1
    # 脱敏
    assert "13800138000" not in user_content
    assert "test@example.com" not in user_content
    assert "[电话已脱敏]" in user_content
    assert "[邮箱已脱敏]" in user_content
    # 总长度有上限
    assert len(user_content) <= 2000 + len("请把下面这些消息概括成一段简体中文摘要（只输出摘要正文）：\n")


@pytest.mark.parametrize("messages", [[], [""], [{"text": ""}], [{"text": None}]])
def test_summarize_empty_input_raises(install_fake, messages):
    opener = install_fake([])
    client, _ = _client()
    with pytest.raises(ExternalAIError) as excinfo:
        client.summarize(messages)
    assert excinfo.value.code == "empty_input"
    assert opener.calls == []


def test_summarize_empty_response_raises(install_fake):
    install_fake([{"choices": []}])
    client, _ = _client()
    with pytest.raises(ExternalAIError) as excinfo:
        client.summarize(["【合成】测试"])
    assert excinfo.value.code == "empty_response"


def test_summarize_max_chars_truncates(install_fake):
    install_fake([_summary_response(content="字" * 100)])
    client, _ = _client()
    summary = client.summarize(["【合成】测试"], max_chars=50)
    assert len(summary.text) == 51
    assert summary.text.endswith("…")


def test_summarize_logs_no_body(install_fake, caplog):
    caplog.set_level(logging.DEBUG, logger="social_secretary.intel.external_ai")
    secret = "合成绝密消息XYZ"
    response_secret = "摘要正文绝密ABC"
    install_fake([_summary_response(content=response_secret)])
    client, _ = _client()
    client.summarize([f"【合成】{secret} 13800138000"])
    log_text = caplog.text
    assert secret not in log_text
    assert "13800138000" not in log_text
    assert response_secret not in log_text
    assert API_KEY not in log_text


def test_diagnostics_reset_between_calls(install_fake):
    install_fake([_summary_response()])
    consent = [True]
    client, _ = _client(consent_fn=lambda: consent[0])
    client.summarize(["【合成】第一次调用"])
    assert client.last_usage is not None
    consent[0] = False
    with pytest.raises(ConsentRequiredError):
        client.summarize(["【合成】第二次调用"])
    assert client.last_usage is None
    assert client.last_latency_ms is None
    assert client.last_http_status is None
    assert client.last_attempts == 0


# ---------------------------------------------------------------------------
# v0.2.0 通用补全 complete()
# ---------------------------------------------------------------------------

def _completion_response(content: str = "OK", *, usage: bool = True) -> dict:
    payload = {
        "model": MODEL,
        "choices": [{"message": {"content": content}}],
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}
    return payload


def test_complete_success_returns_text_and_usage(install_fake):
    opener = install_fake([_completion_response("你好，这是补全结果")])
    client, _ = _client()
    text = client.complete("系统提示：只输出 JSON", "用户提示：整理聊天")
    assert text == "你好，这是补全结果"
    assert client.last_usage == {
        "prompt_tokens": 42,
        "completion_tokens": 7,
        "total_tokens": 49,
    }
    assert client.last_http_status == 200
    assert client.last_attempts == 1

    request = opener.calls[0]["request"]
    assert request.full_url == BASE_URL + "/chat/completions"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == MODEL
    assert payload["messages"][0] == {"role": "system", "content": "系统提示：只输出 JSON"}
    assert payload["messages"][1] == {"role": "user", "content": "用户提示：整理聊天"}
    assert payload["max_tokens"] >= 1
    assert 0.0 <= payload["temperature"] <= 1.0


def test_complete_consent_gate_blocks_network(install_fake):
    opener = install_fake([])
    client, _ = _client(consent=False)
    with pytest.raises(ConsentRequiredError):
        client.complete("system", "user")
    assert opener.calls == []


def test_complete_missing_config_blocks_network(install_fake):
    opener = install_fake([])
    client, _ = _client(api_key="")
    with pytest.raises(ExternalAIError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.code == "config_missing_api_key"
    assert opener.calls == []


def test_complete_rejects_empty_prompts(install_fake):
    opener = install_fake([])
    client, _ = _client()
    with pytest.raises(ValueError):
        client.complete("   ", "user")
    with pytest.raises(ValueError):
        client.complete("system", "")
    assert opener.calls == []


def test_complete_empty_response_raises(install_fake):
    install_fake([{"choices": []}])
    client, _ = _client()
    with pytest.raises(ExternalAIError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.code == "empty_response"


def test_complete_logs_no_prompt_or_response_body(install_fake, caplog):
    caplog.set_level(logging.DEBUG, logger="social_secretary.intel.external_ai")
    install_fake([_completion_response("回复正文绝密ABC")])
    client, _ = _client()
    client.complete("系统提示绝密SYS", "用户内容绝密USER 13800138000")
    log_text = caplog.text
    assert "绝密SYS" not in log_text
    assert "绝密USER" not in log_text
    assert "回复正文绝密ABC" not in log_text
    assert "13800138000" not in log_text
    assert API_KEY not in log_text


