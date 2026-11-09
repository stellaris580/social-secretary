"""白名单与标题规范化的离线测试。"""

from __future__ import annotations

import pytest

from social_secretary.collectors.qq.whitelist import TitleWhitelist, normalize_title
from social_secretary.contracts import ConversationKind

from .fake_uia import FakeBackend, make_window, panel_root


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("测试会话", "测试会话"),
        ("  测试 会话  ", "测试 会话"),
        ("测试会话等3个会话", "测试会话"),
        ("测试会话等 12 个会话", "测试会话"),
        ("ＡＢＣ", "abc"),
        ("A\u200bB", "ab"),
        ("\ufeff张三", "张三"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_title(raw, expected) -> None:  # noqa: ANN001 - 参数化
    assert normalize_title(raw) == expected


def test_exact_allowlist_only() -> None:
    whitelist = TitleWhitelist(["测试会话"])
    assert whitelist.allows("测试会话")
    assert whitelist.allows("测试会话等2个会话")
    assert not whitelist.allows("测试会话2")
    assert not whitelist.allows("别人的会话")
    assert not whitelist.allows("")
    assert whitelist.canonical("测试会话") == "测试会话"
    assert whitelist.canonical("别人的会话") is None


def test_fuzzy_allowlist_is_configurable() -> None:
    whitelist = TitleWhitelist(["测试会话"], fuzzy=True)
    assert whitelist.allows("测试会话（3）")
    assert whitelist.allows("前缀测试会话")
    assert not whitelist.allows("完全无关")


def test_empty_whitelist_rejects_everything() -> None:
    whitelist = TitleWhitelist([])
    assert not whitelist
    assert not whitelist.allows("测试会话")


def test_dedupe_normalized_entries() -> None:
    whitelist = TitleWhitelist(["测试会话", "测试会话", " 测试会话 ", "测试会话等3个会话"])
    assert whitelist.entries == ("测试会话",)


def _source(backend: FakeBackend, allowlist=("测试会话", "群聊A"), kinds=None):  # noqa: ANN001
    from social_secretary.collectors.qq.collector import QqMessageSource
    from social_secretary.collectors.qq.profile import load_profile

    return QqMessageSource(
        backend=backend,
        profile=load_profile(),
        allowlist=allowlist,
        conversation_kinds=kinds,
        sleep_scale=0,
    )


def test_list_conversations_only_returns_allowlist_without_touching_qq() -> None:
    backend = FakeBackend(windows=[])
    source = _source(backend, kinds={"群聊a": ConversationKind.GROUP})
    conversations = source.list_conversations()
    assert [conversation.title for conversation in conversations] == ["测试会话", "群聊A"]
    assert [conversation.external_key for conversation in conversations] == ["测试会话", "群聊A"]
    assert conversations[0].kind is ConversationKind.PRIVATE
    assert conversations[1].kind is ConversationKind.GROUP
    assert conversations[0].account_id == "qq-local"
    assert conversations[0].id.startswith("qq:")
    assert backend.calls == []  # 未访问 QQ


def test_fetch_messages_rejects_non_allowlisted_conversation_before_any_window_access() -> None:
    from social_secretary.collectors.qq.errors import QqWhitelistViolation
    from social_secretary.contracts import Conversation

    backend = FakeBackend(windows=[make_window(100, "QQ", root=panel_root())])
    source = _source(backend, allowlist=("测试会话",))
    conversation = Conversation(
        id="qq:other",
        account_id="qq-local",
        kind=ConversationKind.PRIVATE,
        title="别人的会话",
        external_key="别人的会话",
    )
    with pytest.raises(QqWhitelistViolation) as excinfo:
        source.fetch_messages(conversation, foreground_ack=True)
    assert excinfo.value.code == "whitelist_violation"
    assert backend.calls == []


def test_fetch_messages_rejects_empty_target() -> None:
    from social_secretary.collectors.qq.errors import QqWhitelistViolation
    from social_secretary.contracts import Conversation

    source = _source(FakeBackend(windows=[]), allowlist=("测试会话",))
    conversation = Conversation(
        id="qq:empty",
        account_id="qq-local",
        kind=ConversationKind.PRIVATE,
        title="",
        external_key="",
    )
    with pytest.raises(QqWhitelistViolation):
        source.fetch_messages(conversation, foreground_ack=True)


def test_fetch_messages_requires_foreground_ack() -> None:
    from social_secretary.collectors.qq.collector import QqMessageSource
    from social_secretary.collectors.qq.errors import QqForegroundNotAcknowledged
    from social_secretary.collectors.qq.profile import load_profile
    from social_secretary.contracts import Conversation

    backend = FakeBackend(windows=[make_window(100, "QQ", root=panel_root())])
    source = QqMessageSource(
        backend=backend,
        profile=load_profile(),
        allowlist=("测试会话",),
        sleep_scale=0,
    )
    conversation = Conversation(
        id="qq:test",
        account_id="qq-local",
        kind=ConversationKind.PRIVATE,
        title="测试会话",
        external_key="测试会话",
    )
    with pytest.raises(QqForegroundNotAcknowledged):
        source.fetch_messages(conversation, limit=10, foreground_ack=False)
    assert backend.calls == []
