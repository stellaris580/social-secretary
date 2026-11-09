"""importer.py 测试：MessageSink 注入、dry-run、本地 JSONL sink、异常收敛。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from social_secretary.contracts import RawMessage
from social_secretary.importing import (
    ImportedConversation,
    JsonlMessageSink,
    ImportFormat,
    ImportOptions,
    import_result,
    parse_chat_text,
    run_import,
    validate_sink,
)

from .conftest import FIXED_NOW

SAMPLE_TEXT = "2024-01-01 12:30:00 张三: 你好\n2024-01-01 12:31:00 我: 收到"
SECRET = "私密内容不要写入证据"

AUTH_FLAG = object()


def build_result(text: str = SAMPLE_TEXT, **options: Any):
    options.setdefault("now", FIXED_NOW)
    options.setdefault("source_name", "test_chat.txt")
    return parse_chat_text(text, options=ImportOptions(**options))


class FakeSink:
    def __init__(self) -> None:
        self.conversations: list[ImportedConversation] = []
        self.written: list[RawMessage] = []
        self.calls: list[str] = []

    def ensure_conversation(self, conversation: ImportedConversation) -> None:
        self.calls.append("ensure_conversation")
        self.conversations.append(conversation)

    def write_messages(
        self, conversation: ImportedConversation, messages: Sequence[RawMessage]
    ) -> int:
        self.calls.append("write_messages")
        self.written.extend(messages)
        return len(messages)


def test_run_import_writes_through_sink() -> None:
    result = build_result()
    sink = FakeSink()
    report = run_import(result, sink)
    assert report.ok is True
    assert report.dry_run is False
    assert report.written_count == 2
    assert report.message_count == 2
    assert len(sink.conversations) == 1
    assert [raw.text for raw in sink.written] == ["你好", "收到"]
    assert sink.calls == ["ensure_conversation", "write_messages"]


def test_import_result_alias_works() -> None:
    sink = FakeSink()
    report = import_result(build_result(), sink)
    assert report.ok is True
    assert report.written_count == 2


def test_dry_run_does_not_touch_sink() -> None:
    sink = FakeSink()
    report = run_import(build_result(), sink, dry_run=True)
    assert report.ok is True
    assert report.dry_run is True
    assert report.written_count == 0
    assert sink.calls == []


def test_dry_run_allows_none_sink() -> None:
    report = run_import(build_result(), None, dry_run=True)
    assert report.ok is True
    assert report.sink == "dry-run"


def test_missing_sink_fails_without_crash() -> None:
    report = run_import(build_result(), None)
    assert report.ok is False
    assert "MessageSink" in (report.error or "")


def test_incomplete_sink_rejected() -> None:
    class OnlyEnsure:
        def ensure_conversation(self, conversation: ImportedConversation) -> None:
            pass

    report = run_import(build_result(), OnlyEnsure())  # type: ignore[arg-type]
    assert report.ok is False
    assert "write_messages" in (report.error or "")
    assert validate_sink(OnlyEnsure()) == ["write_messages"]
    assert validate_sink(None) == ["ensure_conversation", "write_messages"]


def test_sink_exception_is_reported_not_raised() -> None:
    class BrokenSink:
        def ensure_conversation(self, conversation: ImportedConversation) -> None:
            pass

        def write_messages(
            self, conversation: ImportedConversation, messages: Sequence[RawMessage]
        ) -> int:
            raise RuntimeError("模拟写库失败")

    report = run_import(build_result(), BrokenSink())
    assert report.ok is False
    assert "RuntimeError" in (report.error or "")
    assert "模拟写库失败" in (report.error or "")


def test_sink_return_is_clamped() -> None:
    class LyingSink(FakeSink):
        def write_messages(
            self, conversation: ImportedConversation, messages: Sequence[RawMessage]
        ) -> int:
            self.calls.append("write_messages")
            return 999

    report = run_import(build_result(), LyingSink())
    assert report.ok is True
    assert report.written_count == 2


def test_empty_result_import_fails_gracefully() -> None:
    result = parse_chat_text("", options=ImportOptions(now=FIXED_NOW, source_name="x.txt"))
    report = run_import(result, FakeSink())
    assert report.ok is False
    assert "没有可导入" in (report.error or "")


def test_report_to_dict_masks_title() -> None:
    result = build_result(conversation_title="家庭群-私密")
    report = run_import(result, FakeSink())
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "家庭群-私密" not in payload
    data = json.loads(payload)
    assert data["title"]["length"] == len("家庭群-私密")
    assert data["written_count"] == 2


# ---------------------------------------------------------------------------
# JsonlMessageSink：独立本地测试目录，默认脱敏
# ---------------------------------------------------------------------------

def test_jsonl_sink_writes_redacted_files(tmp_path: Path) -> None:
    result = build_result(text=f"2024-01-01 12:30:00 张三: {SECRET}", source_name="secret_file.txt")
    target = tmp_path / "import_data"
    sink = JsonlMessageSink(target)
    report = run_import(result, sink)
    assert report.ok is True
    assert (target / "conversation.json").is_file()
    assert (target / "messages.jsonl").is_file()

    raw_content = (target / "messages.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw_content, "默认 sink 不得保存消息正文明文"
    assert "张三" not in raw_content, "默认 sink 不得保存发送者明文"
    assert "secret_file.txt" not in raw_content, "默认 sink 不得保存源文件名明文"
    lines = [json.loads(line) for line in raw_content.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["text_length"] == len(SECRET)
    assert len(lines[0]["extras"]["text_sha256_16"]) == 16
    assert lines[0]["timestamp"].startswith("2024-01-01T12:30:00")

    conversation = json.loads((target / "conversation.json").read_text(encoding="utf-8"))
    assert conversation["conversation_key"] == result.conversation.conversation_key
    assert conversation["experimental"] is True
    conversation_text = json.dumps(conversation, ensure_ascii=False)
    assert "张三" not in conversation_text
    assert "secret_file.txt" not in conversation_text


def test_jsonl_sink_optionally_stores_text(tmp_path: Path) -> None:
    result = build_result(text=f"2024-01-01 12:30:00 张三: {SECRET}")
    target = tmp_path / "import_data_raw"
    sink = JsonlMessageSink(target, include_text=True)
    run_import(result, sink)
    raw_content = (target / "messages.jsonl").read_text(encoding="utf-8")
    assert SECRET in raw_content
    assert "张三" in raw_content


def test_jsonl_sink_read_messages_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "import_data_roundtrip"
    run_import(build_result(), JsonlMessageSink(target))
    items = JsonlMessageSink.read_messages(target)
    assert len(items) == 2
    assert items[0]["timestamp_source"] == "ui"
    assert items[0]["is_self"] is False
    assert items[1]["is_self"] is True
    assert JsonlMessageSink.read_messages(tmp_path / "not-exists") == []


def test_jsonl_sink_is_local_only_and_deterministic(tmp_path: Path) -> None:
    target = tmp_path / "import_data_twice"
    sink = JsonlMessageSink(target)
    run_import(build_result(), sink)
    first = (target / "messages.jsonl").read_text(encoding="utf-8")
    run_import(build_result(), sink)
    second = (target / "messages.jsonl").read_text(encoding="utf-8")
    assert first == second, "同一输入重复导入应产生相同内容（不追加脏数据）"
