"""可选外部 AI 草稿 / 实验性摘要适配器（默认关闭，必须显式同意）。

安全边界（与 AGENTS.md 铁律 4/5 对齐）
------------------------------------
1. **强制同意门控**：构造时必须传入 ``consent_check: Callable[[], bool]``；
   ``propose()`` / ``summarize()`` 在发起任何网络请求前先调用它，返回 False 直接抛
   :class:`ConsentRequiredError`，绝不“先上传再询问”。
2. **数据最小化**：草稿只上传「最新一条消息 + 最多 2 条必要上下文」；实验性摘要最多上传
   最近 10 条消息、每条截断 200 字。上传前统一做 URL/邮箱/手机号/证件号/长数字/疑似密钥脱敏。
3. **日志零正文**：日志只记录模型名、字数、HTTP 状态码、耗时等元数据，
   绝不记录请求/响应正文、API Key、Authorization 头。
4. **只读**：本模块只有生成草稿/摘要的能力，没有任何发送消息/邮件的代码路径。

对外接口
--------
- ``ExternalLLMClient(base_url, model, api_key, consent_check, timeout=30)``
  OpenAI-compatible ``/v1/chat/completions``；``kind = DraftKind.EXTERNAL_LLM``，
  实现 :class:`social_secretary.contracts.DraftProvider`。
  核心层集成时也可用 ``ExternalLLMClient(config=ExternalAIConfig(...),
  credential_store=..., consent_manager=...)`` 的等价构造方式（从凭据管理器取 Key、
  从 ConsentManager 取 EXTERNAL_AI 同意状态；缺少同意管理器时拒绝构造，fail closed）。
- ``summarize(messages, *, consent_check=None, max_chars=300) -> ExternalSummary``：
  **实验性**中文摘要（产品默认摘要仍是本地规则，不联网）；与草稿共用同一套同意门控、
  最小化/脱敏、错误码与重试策略；支持可选的“本次操作额外确认”回调。
- ``complete(system_prompt, user_prompt) -> str``：通用单轮补全（v0.2.0 语义整理复用），
  同样强制同意门控/配置检查/日志零正文；token 用量通过 ``last_usage`` 暴露；
  只返回文本，不做 JSON 假设、不发送任何消息。
- ``ConsentRequiredError``：未同意却尝试调用时抛出（``code='consent_required'``）。
- ``ExternalAIError``：配置缺失、网络失败、HTTP 错误、返回格式异常时抛出
  （异常消息只含状态码/类型，不含响应正文；带 ``code`` / ``http_status`` / ``attempts`` 便于上层结构化处理）。
- ``redact_for_upload(text)``：上传前脱敏（测试与文档用）。
- 诊断字段（验收/证据用，不含敏感内容）：``last_usage``、``last_latency_ms``、
  ``last_http_status``、``last_attempts``、``last_response_model``。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..contracts import (
    ConnectionCheck,
    ConsentScope,
    DraftContext,
    DraftKind,
    ExternalAIConfig,
    Message,
    ReplyDraft,
)

__all__ = [
    "ExternalLLMClient",
    "ExternalAIError",
    "ExternalSummary",
    "ConsentRequiredError",
    "redact_for_upload",
    "SYSTEM_PROMPT",
    "SUMMARY_SYSTEM_PROMPT",
]

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是中文社交秘书的回复草稿助手。只根据用户提供的会话内容，生成一条可直接编辑的简体中文回复草稿。"
    "要求：1) 不发送任何消息，只输出草稿正文；2) 不编造对方没有提供的事实；3) 控制在 120 个汉字以内；"
    "4) 语气自然、礼貌；5) 信息不足时表示稍后确认。"
)

SUMMARY_SYSTEM_PROMPT = (
    "你是中文社交秘书的「实验性」摘要助手。只根据用户提供的最少量消息内容，生成一段简体中文摘要。"
    "要求：1) 不超过 150 字；2) 不编造未出现的事实；3) 不要逐条复述消息原文；"
    "4) 信息不足时直接说明“信息不足，无法摘要”；5) 不输出任何发送动作。"
)

DEFAULT_STYLE = "简洁"
_MAX_TITLE_CHARS = 40
_MAX_NAME_CHARS = 16
_MAX_CONTEXT_CHARS = 120
# 实验性摘要的数据最小化上限（见 docs/design/ai-acceptance.md）
SUMMARY_MAX_MESSAGES = 10
SUMMARY_MAX_MESSAGE_CHARS = 200
SUMMARY_MAX_TOTAL_CHARS = 2000


# ---------------------------------------------------------------------------
# 上传前脱敏
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_KEY_RE = re.compile(r"\b(?:sk|api|key|token)[-_][A-Za-z0-9]{8,}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")


def redact_for_upload(text: str) -> str:
    """上传外部 AI 前的最小化脱敏（链接/邮箱/证件号/手机号/长数字/疑似密钥）。"""
    if not text:
        return ""
    result = _URL_RE.sub("[链接已脱敏]", text)
    result = _EMAIL_RE.sub("[邮箱已脱敏]", result)
    result = _ID_CARD_RE.sub("[证件号已脱敏]", result)
    result = _PHONE_RE.sub("[电话已脱敏]", result)
    result = _KEY_RE.sub("[密钥已脱敏]", result)
    result = _LONG_DIGITS_RE.sub("[数字已脱敏]", result)
    return result


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _safe_label(text: str, limit: int) -> str:
    label = re.sub(r"[\s\r\n]+", "", redact_for_upload(text or ""))
    return _truncate(label, limit)


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _extract_usage(data: dict[str, Any]) -> dict[str, int | None] | None:
    """从 OpenAI-compatible 响应中提取 token 用量；缺失时返回 None（不估算、不伪造）。"""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _usage_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _usage_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _usage_int(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    if prompt is None and completion is None and total is None:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _read_secret_from_store(credential_store: Any, key: str) -> str:
    """从核心层凭据存储读取 API Key；读取失败一律按“未配置”处理（fail closed）。"""
    getter = getattr(credential_store, "get_secret", None)
    if not callable(getter):
        return ""
    try:
        value = getter(key)
    except Exception:  # noqa: BLE001 - 凭据管理器异常不泄漏细节、不中断构造
        logger.warning("读取外部 AI API Key 失败，已按未配置处理")
        return ""
    return value if isinstance(value, str) else ""


def _consent_check_from_manager(consent_manager: Any) -> Callable[[], bool] | None:
    """把核心层 ConsentManager 适配成 ``() -> bool`` 同意检查；无法适配返回 None（fail closed）。"""
    for attribute in ("is_granted", "consent_is_granted"):
        checker = getattr(consent_manager, attribute, None)
        if not callable(checker):
            continue

        def _check(checker: Callable[..., Any] = checker) -> bool:
            try:
                return bool(checker(ConsentScope.EXTERNAL_AI))
            except TypeError:
                try:
                    return bool(checker(ConsentScope.EXTERNAL_AI.value))
                except Exception:  # noqa: BLE001
                    return False
            except Exception:  # noqa: BLE001
                return False

        return _check
    return None


# ---------------------------------------------------------------------------
# 异常（带结构化字段，便于 UI/验收脚本做机器可读的失败处理）
# ---------------------------------------------------------------------------

class ConsentRequiredError(PermissionError):
    """未获得用户明确同意就尝试使用外部 AI 时抛出。"""

    code = "consent_required"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "consent_required"


class ExternalAIError(RuntimeError):
    """外部 AI 调用失败（配置错误/网络错误/HTTP 错误/返回格式异常）。

    结构化字段（错误消息本身不含敏感内容）：
    - ``code``：``config_missing_base_url`` / ``config_missing_api_key`` /
      ``config_missing_model`` / ``http_error`` / ``network_error`` /
      ``invalid_response`` / ``empty_response`` / ``empty_input`` 等；
    - ``http_status``：HTTP 失败时的状态码（无则为 None）；
    - ``attempts``：本次调用实际尝试次数（含重试）。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "external_ai_error",
        http_status: int | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.attempts = attempts


# ---------------------------------------------------------------------------
# 实验性摘要结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExternalSummary:
    """``ExternalLLMClient.summarize()`` 的返回值（实验性，非产品默认摘要）。"""

    text: str
    model_label: str = ""
    experimental: bool = True
    requires_external_consent: bool = True
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class ExternalLLMClient:
    """OpenAI-compatible 外部 AI 草稿客户端（可选的“用户显式同意后”能力）。

    用法::

        client = ExternalLLMClient(
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key=secret,                      # 来自 Windows 凭据管理器，不落普通文件
            consent_check=lambda: settings.external_ai_consent,
            timeout=30,
        )
        drafts = client.propose(message, context)   # 未同意 -> ConsentRequiredError

    核心层等价构造（app-core 集成口径，见 ``core/service.py``）::

        ExternalLLMClient(
            config=ExternalAIConfig(base_url=..., model=..., key_ref="external_ai"),
            credential_store=store,        # 实现 contracts.CredentialStore
            consent_manager=manager,       # 实现 is_granted(ConsentScope)
        )

    ``base_url`` 建议写到 ``/v1``（如 ``https://api.openai.com/v1``）；
    也兼容直接传入完整的 ``.../v1/chat/completions``。缺少同意检查时拒绝构造（fail closed）。
    """

    kind = DraftKind.EXTERNAL_LLM

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        consent_check: Callable[[], bool] | None = None,
        timeout: float = 30.0,
        *,
        config: ExternalAIConfig | None = None,
        credential_store: Any | None = None,
        consent_manager: Any | None = None,
        max_retries: int = 2,
        sleep: Callable[[float], None] | None = None,
        max_message_chars: int = 500,
        max_context_messages: int = 2,
        max_context_chars: int = _MAX_CONTEXT_CHARS,
    ) -> None:
        # 核心层集成口径：从 ExternalAIConfig + 凭据管理器 + 同意管理器构造
        if config is not None:
            base_url = base_url or config.base_url
            model = model or config.model
            if not api_key and credential_store is not None:
                api_key = _read_secret_from_store(credential_store, config.key_ref)
            if consent_check is None and consent_manager is not None:
                consent_check = _consent_check_from_manager(consent_manager)
        if consent_check is None:
            raise TypeError(
                "必须提供 consent_check（或 config + consent_manager）："
                "未获得外部 AI 同意时禁止构造客户端（fail closed）"
            )
        if not callable(consent_check):
            raise TypeError("consent_check 必须是可调用对象（返回 bool 的同意检查函数）")
        self.base_url = (base_url or "").strip()
        self.model = (model or "").strip()
        self._api_key = (api_key or "").strip()
        self._consent_check = consent_check
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep
        self.max_message_chars = max(1, int(max_message_chars))
        self.max_context_messages = max(0, int(max_context_messages))
        self.max_context_chars = max(1, int(max_context_chars))
        # 诊断字段（验收/证据用，只记录元数据，不含正文/Key）
        self.last_usage: dict[str, int | None] | None = None
        self.last_latency_ms: int | None = None
        self.last_http_status: int | None = None
        self.last_attempts: int = 0
        self.last_response_model: str | None = None

    # -- 安全：任何地方都不得打印/返回 API Key -------------------------
    def __repr__(self) -> str:  # pragma: no cover - 防御性测试会覆盖
        return (
            f"ExternalLLMClient(base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key='***', timeout={self.timeout!r})"
        )

    # -- 实现 contracts.DraftProvider ----------------------------------
    def propose(self, message: Message, context: DraftContext) -> list[ReplyDraft]:
        self._reset_diagnostics()
        self._require_consent()
        self._require_config(need_model=True)
        payload = self._build_payload(message, context)
        logger.info(
            "外部 AI 草稿请求：model=%s 正文字数=%d 上下文条数=%d",
            self.model,
            len(message.text or ""),
            len(context.recent_messages or ()),
        )
        data, _latency_ms = self._post_json(self._chat_url(), payload)
        content = self._extract_content(data)
        style = (context.style or DEFAULT_STYLE).strip() or DEFAULT_STYLE
        return [
            ReplyDraft(
                id=f"ext-{int(time.time() * 1000)}-{abs(hash((message.id, content[:16]))) % 100000}",
                message_id=message.id,
                kind=DraftKind.EXTERNAL_LLM,
                style=style,
                text=content,
                created_at=datetime.now().astimezone(),
                model_label=self.model,
                requires_external_consent=True,
            )
        ]

    # -- 实验性摘要（产品默认摘要仍是本地规则）----------------------------
    def summarize(
        self,
        messages: Any,
        *,
        consent_check: Callable[[], bool] | None = None,
        max_chars: int = 300,
    ) -> ExternalSummary:
        """用外部 AI 生成**实验性**中文摘要（不改变产品默认的本地规则摘要）。

        ``messages`` 支持：单条字符串 / 字符串列表 / ``Message`` 列表 /
        带 ``text``、``sender``、``is_self`` 字段的 dict 列表。
        ``consent_check`` 是**本次操作**的额外确认回调（例如 UI 二次确认框）；
        即使构造时已同意，本回调返回 False 也会抛 :class:`ConsentRequiredError`。
        上传内容与草稿一样做脱敏、截断（最多 10 条、每条 200 字）。
        """
        self._reset_diagnostics()
        self._require_consent()
        if consent_check is not None:
            if not callable(consent_check):
                raise TypeError("consent_check 必须是可调用对象（返回 bool 的确认函数）")
            try:
                allowed = bool(consent_check())
            except Exception:  # noqa: BLE001 - 回调异常按未同意处理（fail closed）
                allowed = False
            if not allowed:
                raise ConsentRequiredError(
                    "本次外部 AI 摘要操作未获得额外确认，已阻止上传任何内容。"
                )
        self._require_config(need_model=True)
        lines = self._normalize_summary_messages(messages)
        if not lines:
            raise ExternalAIError("没有可摘要的消息内容。", code="empty_input")

        user_content = (
            "请把下面这些消息概括成一段简体中文摘要（只输出摘要正文）：\n"
            + "\n".join(lines)
        )
        payload = {
            "model": self.model,
            "temperature": 0.3,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        logger.info(
            "外部 AI 摘要请求：model=%s 消息数=%d 总字数=%d",
            self.model,
            len(lines),
            len(user_content),
        )
        data, _latency_ms = self._post_json(self._chat_url(), payload)
        content = self._extract_content(data)
        limit = max(1, int(max_chars))
        if len(content) > limit:
            content = _truncate(content, limit)
        usage = self.last_usage or {}
        return ExternalSummary(
            text=content,
            model_label=self.model,
            experimental=True,
            requires_external_consent=True,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            latency_ms=self.last_latency_ms,
        )

    # -- v0.2.0 通用补全（语义整理复用；只生成文本，不发送）---------------
    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 1600,
        temperature: float = 0.2,
    ) -> str:
        """通用单轮补全（OpenAI-compatible ``/chat/completions``）。

        供 v0.2.0 语义整理等上层复用；与 ``propose`` / ``summarize`` 完全相同的
        同意门控、配置检查、HTTP 重试/结构化错误码与“日志不记录正文”策略。

        - 返回模型输出文本；token 用量见 ``self.last_usage``（服务未返回 usage 时为 ``None``）；
        - 只生成文本：**不解析 JSON、不执行任何指令、没有发送能力**；
        - 调用方负责解析/校验模型输出（例如语义整理的严格 JSON 校验 + 本地回退）。
        """
        self._reset_diagnostics()
        self._require_consent()
        self._require_config(need_model=True)
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("user_prompt 不能为空")
        payload = {
            "model": self.model,
            "temperature": max(0.0, float(temperature)),
            "max_tokens": max(1, int(max_tokens)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        logger.info(
            "外部 AI 通用补全请求：model=%s system_chars=%d user_chars=%d",
            self.model,
            len(system_prompt),
            len(user_prompt),
        )
        data, _latency_ms = self._post_json(self._chat_url(), payload)
        return self._extract_content(data)

    def test_connection(self) -> ConnectionCheck:
        """探测外部 AI 服务连通性；未同意时直接返回失败且绝不发起网络请求。"""
        if not self._consent_ok():
            return ConnectionCheck(
                ok=False,
                detail="未获得外部 AI 使用同意，已阻止网络请求。请先在设置中显式开启外部 AI 同意。",
            )
        if not self.base_url or not self._api_key:
            return ConnectionCheck(ok=False, detail="外部 AI 配置不完整：缺少 base_url 或 API Key。")

        started = time.monotonic()
        try:
            self._get_json(self._models_url())
        except urllib.error.HTTPError as exc:  # pragma: no cover - status 分支由测试覆盖
            status = int(getattr(exc, "code", 0) or 0)
            logger.warning("外部 AI 连通性探测失败：HTTP %s", status)
            return ConnectionCheck(
                ok=False,
                detail=self._http_failure_detail(status),
                latency_ms=self._elapsed_ms(started),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("外部 AI 连通性探测失败：%s", type(exc).__name__)
            return ConnectionCheck(
                ok=False,
                detail="外部 AI 网络不可达，请检查网络和 base_url 配置。",
                latency_ms=self._elapsed_ms(started),
            )
        logger.info("外部 AI 连通性探测成功：model=%s", self.model)
        detail = "连接成功，外部 AI 服务可访问。"
        discovered: dict[str, Any] = {}
        if self.model:
            discovered["model"] = self.model
        return ConnectionCheck(
            ok=True, detail=detail, latency_ms=self._elapsed_ms(started), discovered=discovered
        )

    # -- 构造请求（数据最小化 + 脱敏）----------------------------------
    def _build_payload(self, message: Message, context: DraftContext) -> dict[str, Any]:
        style = (context.style or DEFAULT_STYLE).strip() or DEFAULT_STYLE
        title = _safe_label(context.conversation_title or message.conversation_title, _MAX_TITLE_CHARS)
        name = _safe_label(message.sender, _MAX_NAME_CHARS) or "对方"
        latest = _truncate(redact_for_upload(message.text or ""), self.max_message_chars)

        lines = [
            f"【回复风格】{style}",
            f"【会话】{title}",
            f"【对方】{name}",
            f"【最新消息】{latest}",
        ]
        recent = [
            m
            for m in (context.recent_messages or ())
            if m.id != message.id and (m.text or "").strip()
        ]
        recent = recent[-self.max_context_messages :] if self.max_context_messages else []
        if recent:
            lines.append("【最近上下文（最多 2 条，已截断脱敏）】")
            for item in recent:
                speaker = "我" if item.is_self else (_safe_label(item.sender, _MAX_NAME_CHARS) or "对方")
                snippet = _truncate(redact_for_upload(item.text or ""), self.max_context_chars)
                lines.append(f"{speaker}：{snippet}")
        lines.append("请给出 1 条可直接发送的简体中文回复草稿（不含“草稿：”等前缀）。")

        return {
            "model": self.model,
            "temperature": 0.4,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ],
        }

    # -- HTTP 底层 -----------------------------------------------------
    def _api_root(self) -> str:
        url = self.base_url.rstrip("/")
        for suffix in ("/chat/completions", "/models"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        return url

    def _chat_url(self) -> str:
        return self._api_root() + "/chat/completions"

    def _models_url(self) -> str:
        return self._api_root() + "/models"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "SocialSecretary/0.1 (+local-only-draft)",
        }

    def _post_json(self, url: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempts = 1 + self.max_retries
        last_failure = ""
        for attempt in range(1, attempts + 1):
            self.last_attempts = attempt
            request = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                self.last_http_status = status
                last_failure = f"HTTP {status}"
                retryable = status == 429 or 500 <= status <= 599
                logger.warning(
                    "外部 AI 请求失败：HTTP %s（第 %s/%s 次）", status, attempt, attempts
                )
                if retryable and attempt < attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ExternalAIError(
                    self._http_failure_detail(status),
                    code="http_error",
                    http_status=status,
                    attempts=attempt,
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_failure = type(exc).__name__
                logger.warning(
                    "外部 AI 网络请求失败：%s（第 %s/%s 次）", last_failure, attempt, attempts
                )
                if attempt < attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ExternalAIError(
                    "外部 AI 网络请求失败，请检查网络与 base_url 配置。",
                    code="network_error",
                    attempts=attempt,
                ) from None
            latency_ms = self._elapsed_ms(started)
            self.last_latency_ms = latency_ms
            self.last_http_status = 200
            logger.info(
                "外部 AI 请求成功：model=%s latency_ms=%s bytes=%s",
                self.model,
                latency_ms,
                len(raw),
            )
            data = self._decode_json(raw)
            self._record_response_meta(data)
            return data, latency_ms
        # 理论上不可达，防御性兜底
        raise ExternalAIError(  # pragma: no cover
            f"外部 AI 请求失败：{last_failure}", code="network_error", attempts=self.last_attempts
        )

    def _record_response_meta(self, data: dict[str, Any]) -> None:
        """记录响应元数据（token 用量/模型名），绝不记录正文。"""
        self.last_usage = _extract_usage(data)
        response_model = data.get("model")
        self.last_response_model = response_model if isinstance(response_model, str) else None

    def _reset_diagnostics(self) -> None:
        self.last_usage = None
        self.last_latency_ms = None
        self.last_http_status = None
        self.last_attempts = 0
        self.last_response_model = None

    def _normalize_summary_messages(self, messages: Any) -> list[str]:
        """把多种输入形态转成「说话人：文本」列表，并做上传前最小化/脱敏。"""
        if isinstance(messages, str):
            raw_items: list[Any] = [messages]
        else:
            try:
                raw_items = list(messages)
            except TypeError:
                raise TypeError(
                    "messages 必须是字符串、字符串列表、Message 列表或 dict 列表"
                ) from None
        raw_items = raw_items[-SUMMARY_MAX_MESSAGES:]
        lines: list[str] = []
        total_chars = 0
        for item in raw_items:
            line = self._summary_line(item)
            if not line:
                continue
            if total_chars + len(line) > SUMMARY_MAX_TOTAL_CHARS:
                break
            lines.append(line)
            total_chars += len(line)
        return lines

    @staticmethod
    def _summary_line(item: Any) -> str | None:
        speaker = "对方"
        text = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, Message):
            text = item.text or ""
            speaker = "我" if item.is_self else (_safe_label(item.sender, _MAX_NAME_CHARS) or "对方")
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or ""
            if item.get("is_self"):
                speaker = "我"
            else:
                sender = item.get("sender")
                if isinstance(sender, str):
                    speaker = _safe_label(sender, _MAX_NAME_CHARS) or "对方"
        else:
            return None
        if not isinstance(text, str):
            return None
        cleaned = _truncate(redact_for_upload(text.strip()), SUMMARY_MAX_MESSAGE_CHARS)
        if not cleaned:
            return None
        return f"{speaker}：{cleaned}"

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None  # 探测场景只关心“能连通”，不解析正文

    @staticmethod
    def _decode_json(raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ExternalAIError(
                "外部 AI 返回内容无法解析为 JSON。", code="invalid_response"
            ) from None
        if not isinstance(data, dict):
            raise ExternalAIError("外部 AI 返回格式异常。", code="invalid_response")
        return data

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ExternalAIError("外部 AI 未返回候选结果。", code="empty_response")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):  # 兼容分段 content 的实现
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise ExternalAIError("外部 AI 返回内容为空。", code="empty_response")
        return content.strip()

    # -- 同意 / 配置 / 工具 ---------------------------------------------
    def _consent_ok(self) -> bool:
        try:
            return bool(self._consent_check())
        except Exception:  # 同意检查自身异常时按“未同意”处理（fail closed）
            logger.warning("外部 AI 同意检查抛异常，按未同意处理")
            return False

    def _require_consent(self) -> None:
        if not self._consent_ok():
            raise ConsentRequiredError(
                "未获得外部 AI 使用同意（consent_check 返回 False），已阻止上传任何聊天/邮件内容。"
                "请先在设置中显式开启「外部 AI」同意。"
            )

    def _require_config(self, *, need_model: bool) -> None:
        if not self.base_url:
            raise ExternalAIError(
                "外部 AI 配置不完整：缺少 base_url。", code="config_missing_base_url"
            )
        if not self._api_key:
            raise ExternalAIError(
                "外部 AI 配置不完整：缺少 API Key（应从 Windows 凭据管理器读取）。",
                code="config_missing_api_key",
            )
        if need_model and not self.model:
            raise ExternalAIError(
                "外部 AI 配置不完整：缺少模型名。", code="config_missing_model"
            )

    @staticmethod
    def _http_failure_detail(status: int) -> str:
        if status == 401:
            return "外部 AI 认证失败（HTTP 401）：API Key 无效或未授权。"
        if status == 403:
            return "外部 AI 拒绝访问（HTTP 403）：请检查账号权限。"
        if status == 404:
            return "外部 AI 接口不存在（HTTP 404）：请检查 base_url 是否包含 /v1。"
        if status == 429:
            return "外部 AI 请求过于频繁（HTTP 429）：请稍后重试。"
        if 500 <= status <= 599:
            return f"外部 AI 服务暂时不可用（HTTP {status}），请稍后重试。"
        return f"外部 AI 服务返回错误（HTTP {status}）。"

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * (2 ** (attempt - 1)), 2.0)
