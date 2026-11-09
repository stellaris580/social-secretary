"""跨模块数据契约与接口协议。

本文件由 Lead 维护，是 UI / 核心 / 采集器 / 测试之间唯一共享的接口定义。
任何一方需要新增字段或方法，先发消息给 Lead，由 Lead 修改本文件并通知其他成员。

原则：
1. 本文件只依赖标准库，禁止 import PySide6 / uiautomation / keyring。
2. 所有对外数据对象尽量不可变（frozen dataclass），时间统一使用带时区的 datetime。
3. 采集器（QQ UIA / 邮件 IMAP）只负责把原始数据转换为 RawMessage，
   分类、去重、待办提取、草稿生成都在核心层完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

APP_NAME = "AI 社交秘书"
APP_NAME_EN = "SocialSecretary"
APP_ID = "SocialSecretary"
APP_VERSION = "0.2.5"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class AccountKind(str, Enum):
    QQ = "qq"
    EMAIL = "email"
    # 实验性：用户主动导入的聊天文本（不是自动读取 QQ，不参与扫描/采集）
    IMPORT = "import"


class ConversationKind(str, Enum):
    PRIVATE = "private"      # QQ 私聊
    GROUP = "group"          # QQ 群聊
    MAIL = "mail"            # 邮箱文件夹 / 发件人线程


class Category(str, Enum):
    IMPORTANT = "important"  # 重要
    NORMAL = "normal"        # 普通
    FYI = "fyi"              # 仅供了解


class MessageWorkStatus(str, Enum):
    """用户对一条消息的处理状态（本地手动维护，不影响 QQ/邮箱原始状态）。"""

    UNHANDLED = "unhandled"    # 未处理（默认）
    NEEDS_REPLY = "needs_reply"  # 待回复
    DONE = "done"              # 已处理
    NO_REPLY = "no_reply"      # 无需回复


class TimestampSource(str, Enum):
    UI = "ui"                # 直接从界面/邮件头读到的时间
    RELATIVE = "relative"    # 界面只给了相对时间（如 刚刚/昨天 09:12）
    COLLECTED = "collected"  # 无时间，使用采集时刻（必须标注，不伪装成真实时间）


class ScanStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"      # 部分会话成功
    FAILED = "failed"
    PAUSED = "paused"
    BLOCKED = "blocked"      # 环境不满足（QQ 未登录/不在前台/无权限）


class TodoStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class DraftKind(str, Enum):
    LOCAL_RULES = "local_rules"    # 本地规则草稿（默认，零上传）
    EXTERNAL_LLM = "external_llm"  # 用户显式授权后的外部 AI（可选）


class ConsentScope(str, Enum):
    QQ_READ = "qq_read"
    MAIL_READ = "mail_read"
    EXTERNAL_AI = "external_ai"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# ---------------------------------------------------------------------------
# 基础数据对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Account:
    id: str
    kind: AccountKind
    display_name: str
    enabled: bool = True
    created_at: datetime | None = None
    # 采集器需要的非敏感标识（如邮箱地址；绝不包含密码/授权码）
    identifier: str = ""


@dataclass(frozen=True)
class Conversation:
    id: str
    account_id: str
    kind: ConversationKind
    title: str
    external_key: str
    enabled: bool = True
    priority: Priority = Priority.NORMAL
    last_scanned_at: datetime | None = None
    last_message_at: datetime | None = None
    note: str = ""


@dataclass(frozen=True)
class RawMessage:
    """采集器输出的原始消息，尚未去重/分类。"""

    conversation_key: str
    sender: str
    is_self: bool
    text: str
    timestamp: datetime | None = None
    timestamp_source: TimestampSource = TimestampSource.COLLECTED
    raw_time_text: str | None = None
    external_id: str | None = None
    source_locator: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    id: str
    account_id: str
    conversation_id: str
    conversation_title: str
    conversation_kind: ConversationKind
    sender: str
    is_self: bool
    text: str
    timestamp: datetime | None
    timestamp_source: TimestampSource
    collected_at: datetime
    category: Category = Category.NORMAL
    category_reasons: tuple[str, ...] = ()
    work_status: MessageWorkStatus = MessageWorkStatus.UNHANDLED
    content_hash: str = ""
    source_locator: str | None = None
    external_id: str | None = None
    # 邮件专用：主题（正文仍在 text 中，text = subject + "\n\n" + body；UI 收件箱展示用）
    subject: str = ""
    # 是否为演示/合成数据（UI 必须与真实邮件明显区分，禁止混淆）
    is_demo: bool = False
    # 邮件专用：附件文件名（仅名称，程序不下载/保存附件内容；UI 展示用）
    attachment_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class TodoItem:
    id: str
    message_id: str
    conversation_id: str
    conversation_title: str
    text: str
    due_at: datetime | None = None
    due_text: str | None = None
    confidence: float = 0.5
    status: TodoStatus = TodoStatus.OPEN
    created_at: datetime | None = None


@dataclass(frozen=True)
class ReplyDraft:
    id: str
    message_id: str
    kind: DraftKind
    style: str
    text: str
    created_at: datetime | None = None
    model_label: str = ""
    edited_text: str | None = None
    requires_external_consent: bool = False


@dataclass(frozen=True)
class CollectionResult:
    scan_id: str
    account_id: str
    status: ScanStatus
    messages_seen: int = 0
    messages_new: int = 0
    conversations_scanned: int = 0
    error: str | None = None
    warnings: tuple[str, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    foreground_used: bool = False
    duration_ms: int = 0


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    detail: str
    latency_ms: int | None = None
    discovered: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceDetail:
    """“查看原始消息来源”所需的信息（脱敏，不含凭据）。"""

    message_id: str
    account_kind: AccountKind
    conversation_title: str
    sender: str
    raw_time_text: str | None
    collected_at: datetime
    timestamp_source: TimestampSource
    external_id: str | None
    locator: str | None
    # 例如 "QQ 前台聊天窗口 · 第 12 条可见气泡" / "IMAP INBOX UID 12345"
    origin_label: str


@dataclass(frozen=True)
class DailySummary:
    day: date
    generated_at: datetime
    counts: dict[str, int]
    important: tuple[Message, ...]
    normal: tuple[Message, ...]
    fyi: tuple[Message, ...]
    todos: tuple[TodoItem, ...]
    conversations_touched: int
    narrative: str
    caveats: tuple[str, ...] = ()
    scan_status: ScanStatus = ScanStatus.OK
    is_demo: bool = False


@dataclass(frozen=True)
class AppSettings:
    """非敏感设置。凭据（邮箱授权码 / AI API Key）只能进 Windows 凭据管理器。"""

    collection_paused: bool = True
    qq_read_consent: bool = False
    mail_read_consent: bool = False
    external_ai_consent: bool = False
    show_foreground_warning: bool = True
    manual_only: bool = True
    auto_scan_interval_minutes: int = 0
    external_ai_base_url: str = ""
    external_ai_model: str = ""
    data_retention_days: int = 30
    first_run_completed: bool = False
    log_level: str = "INFO"
    minimize_to_tray: bool = True
    theme: str = "light"
    # 实验性：QQ 客户端只读（默认关闭；需用户显式开启 + 授权会话 + QQ 前台）
    qq_read_experimental: bool = False

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass(frozen=True)
class ExternalAIConfig:
    base_url: str
    model: str
    key_ref: str = "external_ai"  # Windows 凭据管理器中的条目名，不是密钥本身


# ---------------------------------------------------------------------------
# 采集器协议（QQ UIA / 邮件 IMAP / 演示数据）
# ---------------------------------------------------------------------------

@runtime_checkable
class MessageSource(Protocol):
    """只读消息源。实现方禁止发送消息、禁止修改远端数据。"""

    kind: AccountKind

    def test_connection(self) -> ConnectionCheck: ...

    def list_conversations(self) -> list[Conversation]: ...

    def fetch_messages(
        self,
        conversation: Conversation,
        *,
        since: datetime | None = None,
        limit: int = 100,
        foreground_ack: bool = False,
    ) -> list[RawMessage]: ...

    def close(self) -> None: ...


@runtime_checkable
class DraftProvider(Protocol):
    """回复草稿生成器。默认实现是本地规则，不联网。"""

    kind: DraftKind

    def propose(self, message: Message, context: "DraftContext") -> list[ReplyDraft]: ...


@dataclass(frozen=True)
class CategoryDecision:
    category: Category
    reasons: tuple[str, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class ExtractedTodo:
    """待办提取结果（尚未分配 id，由核心层落库时补齐）。"""

    text: str
    due_at: datetime | None = None
    due_text: str | None = None
    confidence: float = 0.5


@dataclass(frozen=True)
class DraftContext:
    conversation_title: str
    recent_messages: tuple[Message, ...] = ()
    style: str = "简洁"
    user_note: str = ""


@runtime_checkable
class Classifier(Protocol):
    """消息分类器（本地规则或用户授权后的模型）。默认实现不得联网。"""

    def classify(
        self,
        raw: RawMessage,
        *,
        conversation_title: str,
        conversation_kind: ConversationKind,
        conversation_priority: Priority = Priority.NORMAL,
    ) -> CategoryDecision: ...


@runtime_checkable
class TodoExtractor(Protocol):
    """从消息中提取待办与截止时间。"""

    def extract(self, raw: RawMessage) -> list[ExtractedTodo]: ...


@runtime_checkable
class CredentialStore(Protocol):
    """系统安全凭据存储（Windows 凭据管理器）。实现方不得把密钥写入普通文件或日志。"""

    def set_secret(self, key: str, secret: str, *, label: str = "") -> None: ...

    def get_secret(self, key: str) -> str | None: ...

    def delete_secret(self, key: str) -> bool: ...

    def has_secret(self, key: str) -> bool: ...


# ---------------------------------------------------------------------------
# UI 依赖的服务门面（由核心层实现，UI 只依赖本协议 + 数据对象）
# ---------------------------------------------------------------------------

@runtime_checkable
class ServiceAPI(Protocol):
    """UI 调用的全部业务能力。实现必须保证：任何方法都不会发送 QQ 消息或邮件。"""

    # --- 账号 ---
    def list_accounts(self) -> list[Account]: ...
    def add_qq_account(self, display_name: str, allowlist: list[str]) -> Account: ...
    def add_mail_account(self, email: str, display_name: str, folder: str = "INBOX") -> Account: ...
    def set_account_enabled(self, account_id: str, enabled: bool) -> None: ...
    def remove_account(self, account_id: str, delete_messages: bool = True) -> None: ...

    # --- 会话授权 ---
    def list_conversations(self, account_id: str | None = None) -> list[Conversation]: ...
    def set_conversation_enabled(self, conversation_id: str, enabled: bool) -> None: ...
    def set_conversation_priority(self, conversation_id: str, priority: Priority) -> None: ...
    def refresh_conversations(self, account_id: str) -> ConnectionCheck: ...

    # --- 采集 ---
    def test_account(self, account_id: str) -> ConnectionCheck: ...
    def run_scan(
        self,
        account_id: str | None = None,
        *,
        trigger: str = "manual",
        foreground_ack: bool = False,
    ) -> list[CollectionResult]: ...
    def collection_status(self) -> dict[str, Any]: ...
    def pause_collection(self) -> None: ...
    def resume_collection(self) -> None: ...

    # --- 一键整理所有已授权消息（QQ 批量 + 邮箱同步 + 今日简报；不发送任何内容）---
    # 返回：{ok, configured:{qq,mail}, results:[CollectionResult], summary:DailySummary|None,
    #        qq_success, qq_failed, mail_success, mail_failed, errors:[...], duration_ms}
    def organize_all(self, *, foreground_ack: bool = False) -> dict[str, Any]: ...

    # --- 实验性：一键读取当前打开的 QQ 聊天（只读；用户点一次即完成授权/账号/白名单/刷新/扫描）---
    def quick_qq_read(self, *, user_confirmed: bool = False) -> dict[str, Any]: ...

    # --- 实验性：用户在本机选择要读取的 QQ 会话（隐私安全白名单；标题只显示不落库/不写日志）---
    def list_qq_visible_sessions(self, *, user_confirmed: bool = False) -> dict[str, Any]: ...
    def apply_qq_session_selection(self, titles: list[str]) -> dict[str, Any]: ...

    # --- 摘要（本地规则）---
    def generate_daily_summary(self, day: date | None = None) -> DailySummary: ...
    def get_cached_summary(self, day: date | None = None) -> DailySummary | None: ...

    # --- 真正 AI 语义整理（可选外部 AI；默认本地规则；返回结构化 dict，含来源 message_id）---
    # 返回结构约定（所有模式都含 provider 字段，明确 local_rules / external_llm / unavailable）：
    # conversation: {ok, provider, conversation_title, overview, items:[{kind,text,source_message_ids,
    #   uncertain,due_hint}], questions, requests, deadlines, promises, unresolved, chitchat,
    #   uncertainty_notes, usage, error}
    # daily: {ok, provider, day, sections:{must_today, soon, batch_reply, fyi}, citations, caveats, usage, error}
    # draft: {ok, provider, message_id, style, text, context_message_ids, uncertainty_notes, usage, error}
    def generate_conversation_semantic(
        self, conversation_id: str, *, use_external: bool = False
    ) -> dict[str, Any]: ...
    def generate_daily_brief(
        self, *, day: date | None = None, use_external: bool = False
    ) -> dict[str, Any]: ...
    def generate_semantic_draft(
        self, message_id: str, *, style: str = "友好", use_external: bool = False
    ) -> dict[str, Any]: ...

    # --- 邮箱收件箱（真实邮件；GUI 端到端验收核心路径）---
    def list_mail_accounts(self) -> list[Account]: ...
    def sync_mail(
        self,
        account_id: str | None = None,
        *,
        user_confirmed: bool = False,
    ) -> list[CollectionResult]: ...
    def list_inbox_messages(
        self,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[Message]: ...
    def generate_latest_mail_summary(self) -> DailySummary | None: ...
    def mail_inbox_status(self, account_id: str | None = None) -> dict[str, Any]: ...

    # --- 消息 / 来源 ---
    def list_messages(
        self,
        day: date | None = None,
        category: Category | None = None,
        limit: int = 200,
        work_status: MessageWorkStatus | None = None,
    ) -> list[Message]: ...
    def set_message_work_status(self, message_id: str, status: MessageWorkStatus) -> None: ...
    def set_message_category(self, message_id: str, category: Category) -> None: ...
    def message_work_status_counts(self) -> dict[str, int]: ...
    def get_source_detail(self, message_id: str) -> SourceDetail: ...

    # --- 待办 ---
    def list_todos(self, day: date | None = None, include_done: bool = False) -> list[TodoItem]: ...
    def update_todo_status(self, todo_id: str, status: TodoStatus) -> None: ...

    # --- 草稿 ---
    def generate_drafts(self, message_id: str, styles: list[str] | None = None) -> list[ReplyDraft]: ...
    def save_draft_edit(self, draft_id: str, text: str) -> None: ...
    def list_drafts(self, message_id: str) -> list[ReplyDraft]: ...

    # --- 设置 / 权限 / 隐私 ---
    def get_settings(self) -> AppSettings: ...
    def update_settings(self, **changes: Any) -> AppSettings: ...
    def grant_consent(self, scope: ConsentScope, *, confirmation_text: str = "") -> None: ...
    def revoke_consent(self, scope: ConsentScope) -> None: ...
    def list_consent_history(self) -> list[dict[str, Any]]: ...

    # --- 邮箱凭据（用户本人在本机输入；实现方返回后立即存入系统凭据管理器）---
    def set_mail_credential(self, email: str, auth_code: str) -> None: ...
    def has_mail_credential(self, email: str) -> bool: ...
    def delete_mail_credential(self, email: str) -> None: ...
    def test_mail_credential(self, email: str) -> ConnectionCheck: ...

    # --- 外部 AI（默认关闭；必须显式同意）---
    def test_external_ai(self) -> ConnectionCheck: ...
    def has_external_ai_key(self) -> bool: ...
    def set_external_ai_key(self, api_key: str) -> None: ...

    # --- 演示/测试 ---
    def load_demo_data(self) -> None: ...
    def clear_demo_data(self) -> None: ...
    def is_demo_active(self) -> bool: ...

    # --- 实验性：用户主动导入聊天文本（不联网、不读 QQ 数据库、不等于自动读取 QQ）---
    def import_chat_text(
        self,
        text: str,
        *,
        source_name: str = "",
        options: dict[str, Any] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    # --- 本地数据 ---
    def get_storage_stats(self) -> dict[str, Any]: ...
    def delete_all_local_data(self) -> None: ...
    def export_diagnostics(self) -> str: ...
    def shutdown(self) -> None: ...
