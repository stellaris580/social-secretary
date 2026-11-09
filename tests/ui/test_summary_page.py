"""今日摘要页测试：分类计数、叙述、caveats、演示标记、点击消息。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fake_service import FakeService
from social_secretary.contracts import Category, ScanStatus
from social_secretary.ui.widgets import Banner


def _page(window):
    return window.pages["summary"]


def test_demo_summary_is_stable_across_midnight():
    """演示时钟可控：即使在 23:50 生成数据、真实日期已跨天，摘要仍有内容。

    回归背景：演示消息原本按「当前时刻往前推」生成，00:0x 运行时会落到前一天，
    摘要按“今天”过滤后计数为 0。
    """
    demo_now = datetime(2026, 10, 31, 23, 50, tzinfo=timezone(timedelta(hours=8)))
    service = FakeService(now=demo_now)

    messages = service.list_messages()
    assert messages
    assert all(
        (message.timestamp or message.collected_at).astimezone().date() == demo_now.date()
        for message in messages
    )

    summary = service.generate_daily_summary()
    assert summary.day == demo_now.date()
    assert len(summary.important) == 2
    assert len(summary.normal) == 1
    assert len(summary.fyi) == 2
    assert summary.counts["important"] == 2


def test_empty_state_before_summary(window):
    page = _page(window)
    assert page.category_counts() == {"important": 0, "normal": 0, "fyi": 0}
    assert "还没有整理过消息" in page.narrative_label.cget("text")
    assert "一键整理所有消息" in page.narrative_label.cget("text")
    assert page.demo_badge.winfo_manager() == ""


def test_show_summary_renders_counts_and_lists(window, service):
    summary = service.generate_daily_summary()
    page = _page(window)
    page.show_summary(summary)

    assert page.category_counts() == {"important": 2, "normal": 1, "fyi": 2}
    assert page.cards[Category.IMPORTANT].count_label.cget("text") == "2 条"
    assert page.cards[Category.NORMAL].listbox.size() == 1
    assert page.cards[Category.FYI].listbox.size() == 2
    assert "重要的事" in page.narrative_label.cget("text")
    assert "待办" in page.todo_hint_label.cget("text")
    assert page.demo_badge.winfo_manager() == "pack"


def test_summary_renders_caveats_as_warning_banners(window, service):
    summary = service.generate_daily_summary()
    page = _page(window)
    page.show_summary(summary)

    banners = [child for child in page.caveats_frame.winfo_children() if isinstance(child, Banner)]
    assert len(banners) == len(summary.caveats)
    assert "前台" in banners[0].text() or "打断" in banners[0].text()


def test_clicking_message_opens_detail(window, service, monkeypatch):
    summary = service.generate_daily_summary()
    page = _page(window)
    page.show_summary(summary)

    opened: list[str] = []
    monkeypatch.setattr(window, "open_message_detail", lambda message_id: opened.append(message_id))
    card = page.cards[Category.IMPORTANT]
    card.listbox.selection_set(0)
    card._on_select()
    window.root.update()
    assert opened == [card.messages[0].id]


def test_refresh_reads_cached_summary(window, service):
    page = _page(window)
    assert page.category_counts()["important"] == 0

    service.generate_daily_summary()
    page.refresh()
    assert page.category_counts()["important"] == 2


def test_refresh_error_shows_banner(window, service):
    page = _page(window)
    service.fail_method("get_cached_summary", "演示读取失败")
    page.refresh()
    assert page.error_banner.winfo_manager() == "pack"
    assert "失败" in page.error_banner.text()


def test_non_ok_scan_status_shows_banner(window, service):
    summary = service.generate_daily_summary()
    page = _page(window)
    page.show_summary(replace(summary, scan_status=ScanStatus.PARTIAL))
    assert page.scan_banner.winfo_manager() == "pack"
    assert "部分完成" in page.scan_banner.text()
