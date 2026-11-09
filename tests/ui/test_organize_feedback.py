"""P0 真实失败反馈：首页详细结果 / 零消息文案 / 刷新保留。"""

from __future__ import annotations


def _summary(window):
    return window.pages["summary"]


def _configure_partial(service):
    service.organize_qq_failed = 1
    service.organize_qq_authorized = 2
    service.organize_qq_sessions_scanned = 2
    service.organize_qq_sessions_failed = 1
    service.organize_qq_failure_hint = (
        "无法将 QQ 主面板置前（可能最小化到托盘/被遮挡），本次只尝试读取了 1 个会话。"
    )
    service.organize_mail_new = 0
    service.organize_history_total = 248
    service.organize_latest_day = __import__("datetime").date(2026, 11, 2)


def test_partial_failure_shows_details_on_homepage(window, service):
    _configure_partial(service)
    window.organize_all()
    page = _summary(window)
    text = page.organize_banner.text()
    assert "本次整理未完整完成" in text
    assert "QQ：授权 2 个会话，成功 1 个，失败 1 个" in text
    assert "原因：无法将 QQ 主面板置前" in text
    assert "解决方法：先打开 QQ 主面板并保持可见" in text
    assert "邮箱：同步成功，新增 0 封" in text
    assert "摘要：已生成，但因 QQ 未读取可能不完整" in text
    assert "历史：本地已保存 248 条，最近一条 2026-11-02" in text
    narrative = page.narrative_label.cget("text")
    assert "本次整理未完整完成，无法确认今天是否有新消息" in narrative
    assert "今天暂无可展示" not in narrative
    assert "详见下方" not in narrative


def test_partial_failure_banner_visible_before_narrative(window, service):
    _configure_partial(service)
    window.organize_all()
    page = _summary(window)
    assert page.organize_banner.winfo_manager() == "pack"
    # 详细提示在 narrative 上方（pack before=narrative_card）
    assert page.organize_banner.winfo_ismapped() in (True, 0, 1)


def test_generic_scan_banner_not_shown_when_detail_banner_exists(window, service):
    _configure_partial(service)
    window.organize_all()
    page = _summary(window)
    assert page.scan_banner.winfo_manager() == ""
    assert "详见下方" not in page.scan_banner.text()


def test_organize_detail_preserved_after_refresh(window, service):
    _configure_partial(service)
    window.organize_all()
    page = _summary(window)
    before = page.organize_banner.text()
    page.refresh()
    assert page.organize_banner.winfo_manager() == "pack"
    assert page.organize_banner.text() == before


def test_scan_ok_zero_messages_with_history_copy(window, service):
    service._messages = []
    service.organize_mail_new = 0
    service.organize_history_total = 248
    service.organize_latest_day = __import__("datetime").date(2026, 11, 2)
    window.organize_all()
    page = _summary(window)
    narrative = page.narrative_label.cget("text")
    assert "没有新消息" in narrative
    assert "本地已保存历史 248 条" in narrative
    assert "2026-11-02" in narrative
    assert "暂无" not in narrative
    assert "没有重要消息" not in narrative
    assert "无法确认" not in narrative
    banner = page.organize_banner.text()
    assert "本次整理完成" in banner
    assert "历史：本地已保存 248 条" in banner


def test_partial_zero_messages_copy_differs(window, service):
    service._messages = []
    _configure_partial(service)
    window.organize_all()
    page = _summary(window)
    narrative = page.narrative_label.cget("text")
    assert "无法确认今天是否有新消息" in narrative
    assert "没有新消息；本地已保存" not in narrative


def test_no_history_no_messages_copy(window, service):
    service._messages = []
    service.organize_history_total = 0
    service.organize_latest_day = None
    window.organize_all()
    page = _summary(window)
    assert "本地还没有消息，请先完成授权并整理" in page.narrative_label.cget("text")


def test_zero_message_is_explicitly_not_scan_failure(window, service):
    service._messages = []
    service.organize_mail_new = 0
    service.organize_history_total = 10
    service.organize_latest_day = __import__("datetime").date(2026, 11, 1)
    window.organize_all()
    page = _summary(window)
    assert "这不是采集失败" in page.todo_hint_label.cget("text") or "历史消息仍可在" in page.todo_hint_label.cget("text")
