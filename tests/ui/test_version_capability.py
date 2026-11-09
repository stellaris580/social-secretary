"""v0.1.1 验收：版本显示、能力状态说明、失败文案（不出现「成功」）。"""

from __future__ import annotations

import tkinter as tk

from social_secretary.contracts import APP_VERSION, ConsentScope


def _collect_texts(widget) -> list[str]:
    texts: list[str] = []

    def walk(node):
        for child in node.winfo_children():
            try:
                text = child.cget("text")
                if text:
                    texts.append(str(text))
            except Exception:  # noqa: BLE001
                pass
            walk(child)

    walk(widget)
    return texts


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------
def test_version_is_current_in_sidebar_and_about(window):
    assert APP_VERSION == "0.2.5"
    assert f"版本 {APP_VERSION}" in window.version_label.cget("text")

    settings = window.pages["settings"]
    about = settings.about_label.cget("text")
    assert APP_VERSION in about
    assert f"版本：{APP_VERSION}" in about


# ---------------------------------------------------------------------------
# 能力状态（开箱即用 / 需要额外配置 / 当前不可用）
# ---------------------------------------------------------------------------
def test_capability_card_explains_three_groups(window):
    settings = window.pages["settings"]
    texts = "\n".join(_collect_texts(settings))
    for keyword in (
        f"能力状态（版本 {APP_VERSION}）",
        "开箱即用",
        "今日摘要",
        "分类",
        "待办",
        "本机规则回复草稿",
        "消息来源",
        "隐私控制",
        "需要额外配置",
        "QQ 邮箱",
        "授权码",
        "外部 AI",
        "API Key",
        "实验性（默认关闭）",
        "QQ 客户端读取（实验）",
        "仍无任何发送能力",
        "未完成全部真机内容验收",
    ):
        assert keyword in texts, f"能力状态缺少说明：{keyword}"


def test_capability_status_reflects_mail_credential_and_ai_key(window, service):
    settings = window.pages["settings"]
    assert "还没有配置授权码" in settings.mail_capability_label.cget("text")
    assert "还没有配置 API Key" in settings.ai_capability_label.cget("text")

    # 配置邮箱授权码后，能力状态应显示已保存
    service.set_mail_credential("demo@qq.com", "x" * 16)
    settings.refresh()
    assert "已保存授权码" in settings.mail_capability_label.cget("text")

    # 配置外部 AI Key 但未同意时，明确说明仍不可用
    service.set_external_ai_key("sk-demo")
    settings.refresh()
    assert "已保存 API Key" in settings.ai_capability_label.cget("text")
    assert "仍不可用" in settings.ai_capability_label.cget("text")

    # 同意后显示已配置
    service.grant_consent(ConsentScope.EXTERNAL_AI)
    settings.refresh()
    assert "已保存密钥并同意上传" in settings.ai_capability_label.cget("text")


# ---------------------------------------------------------------------------
# 失败文案：明确失败 + 不出现「成功」
# ---------------------------------------------------------------------------
def test_mail_test_failure_copy_has_no_success_word(window, service, notify_records):
    settings = window.pages["settings"]
    settings.mail_address_input.delete(0, "end")
    settings.mail_address_input.insert(0, "demo@qq.com")
    # FakeService 默认没有保存授权码 → test_mail_credential 返回 ok=False
    settings.test_mail_credential_button.invoke()
    window.root.update()

    status = settings.mail_status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status
    assert "授权码" in status  # 给出可操作建议


def test_external_ai_test_failure_copy_has_no_success_word(window, service, notify_records):
    settings = window.pages["settings"]
    settings.test_external_ai_button.invoke()
    window.root.update()

    status = settings.external_status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status
    assert "密钥" in status or "服务地址" in status


def test_summary_failure_status_has_no_success_word(window, service, notify_records):
    service.fail_method("generate_daily_summary", "演示故障")
    assert window.generate_summary() is None
    status = window.status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status
    # 状态栏失败时用醒目颜色（danger），而不是默认灰字
    assert window.status_label.cget("fg") == window.theme.color("danger")


def test_scan_failure_status_has_no_success_word(window, service, notify_records):
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=False, show_foreground_warning=False)
    service.fail_method("run_scan", "演示读取失败")
    assert window.run_scan() is None
    status = window.status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status


def test_draft_generation_failure_copy_has_no_success_word(window, service, notify_records):
    drafts = window.pages["drafts"]
    service.fail_method("generate_drafts", "演示草稿失败")
    drafts.generate_local_button.invoke()
    window.root.update()

    status = drafts.status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status
    assert notify_records["error"]


def test_external_ai_draft_failure_copy_has_no_success_word(window, service, monkeypatch, notify_records):
    from social_secretary.ui.pages import drafts_page as drafts_page_module

    service.update_settings(external_ai_consent=True)
    drafts = window.pages["drafts"]

    class AcceptedUpload:
        def __init__(self, window_, **kwargs):
            pass

        def show(self):
            return True

    monkeypatch.setattr(drafts_page_module, "ExternalAIUploadDialog", AcceptedUpload)
    service.fail_method("generate_drafts", "演示外部 AI 失败")
    drafts.generate_external_button.invoke()
    window.root.update()

    status = drafts.status_label.cget("text")
    assert "失败" in status
    assert "成功" not in status


def test_first_run_next_steps_mention_capability(window, service, notify_records):
    window.apply_first_run_consents([ConsentScope.QQ_READ, ConsentScope.MAIL_READ])
    status = window.status_label.cget("text")
    assert "同意已保存" in status
    assert "读取 QQ 当前聊天（实验）" in status
    assert "一次即可" in status
    assert "授权码" in status


def test_summary_empty_state_gives_first_run_guidance(window):
    page = window.pages["summary"]
    page.show_summary(None)
    text = page.narrative_label.cget("text")
    assert "加载演示数据" in text
    assert "一键整理所有消息" in text
    assert "QQ/邮箱授权" in text
    assert "QQ 客户端读取为实验性" in text
    assert "读取 QQ 当前聊天（实验）" in text
