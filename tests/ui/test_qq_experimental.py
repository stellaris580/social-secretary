"""P0-23：QQ 客户端读取实验开关（Tkinter）测试。"""

from __future__ import annotations

from social_secretary.contracts import ConsentScope


def _settings(window):
    return window.pages["settings"]


def _all_texts(widget) -> str:
    texts: list[str] = []

    def walk(node):
        for child in node.winfo_children():
            try:
                value = child.cget("text")
                if value:
                    texts.append(str(value))
            except Exception:  # noqa: BLE001
                pass
            walk(child)

    walk(widget)
    return "\n".join(texts)


def test_qq_experimental_default_off_and_public_hooks(window):
    page = _settings(window)
    assert hasattr(page, "qq_experimental_var")
    assert page.qq_experimental_var.get() is False
    assert callable(page.toggle_qq_experimental)
    assert page.qq_experimental_check.cget("state") == "normal"
    assert "当前：关闭" in page.qq_experimental_status.cget("text")
    assert window.service.get_settings().qq_read_experimental is False


def test_capability_card_qq_is_experimental_default_off(window):
    texts = _all_texts(_settings(window))
    assert "实验性（默认关闭）" in texts
    assert "QQ 客户端读取（实验）" in texts
    assert "默认关闭" in texts
    assert "仅只读你在白名单中授权的会话" in texts
    assert "扫描时需 QQ 主窗口在前台" in texts
    assert "仍无任何发送能力" in texts
    assert "未完成全部真机内容验收，可能失败，失败会明确提示" in texts
    # 旧的「当前不可用」说法不再用于 QQ 客户端读取
    assert "QQ 客户端消息读取：当前版本暂不可用" not in texts


def test_toggle_qq_experimental_calls_update_settings(window, service):
    page = _settings(window)
    page.qq_experimental_var.set(True)
    page.toggle_qq_experimental()
    calls = service.calls_to("update_settings")
    assert calls
    assert calls[-1][2]["qq_read_experimental"] is True
    assert service.get_settings().qq_read_experimental is True
    assert "当前：已开启" in page.qq_experimental_status.cget("text")

    page.qq_experimental_var.set(False)
    page.toggle_qq_experimental()
    assert service.get_settings().qq_read_experimental is False
    assert "当前：关闭" in page.qq_experimental_status.cget("text")


def test_toggle_qq_experimental_failure_rolls_back(window, service, notify_records):
    page = _settings(window)
    service.fail_method("update_settings", "演示设置保存失败")
    page.qq_experimental_var.set(True)
    page.toggle_qq_experimental()
    assert page.qq_experimental_var.get() is False  # 回滚
    assert "失败" in page.qq_experimental_status.cget("text")
    assert "成功" not in page.qq_experimental_status.cget("text")
    assert notify_records["error"]


def test_run_scan_warns_qq_experimental_off_but_continues(window, service, notify_records):
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=False, qq_read_experimental=False)
    service.reset_calls()
    results = window.run_scan()
    assert results is not None, "未开启 QQ 实验读取时仍应继续读取邮箱"
    warning_texts = "\n".join(str(item) for item in notify_records["warning"])
    assert "QQ 实验读取未启用" in warning_texts
    assert "只会读取邮箱" in warning_texts
    assert "邮箱读取不受影响" in warning_texts
    assert service.called("run_scan")


def test_run_scan_no_experimental_warning_when_enabled(window, service, notify_records, monkeypatch):
    from social_secretary.ui import main_window as main_window_module

    class FakeWarning:
        def __init__(self, window_):
            pass

        def show(self):
            return True

    monkeypatch.setattr(main_window_module, "ForegroundWarningDialog", FakeWarning)
    service.grant_consent(ConsentScope.QQ_READ)
    service.update_settings(collection_paused=False, qq_read_experimental=True)
    window.run_scan()
    warning_texts = "\n".join(str(item) for item in notify_records["warning"])
    assert "QQ 实验读取未启用" not in warning_texts


def test_summary_and_first_run_copy_mention_experimental_qq(window):
    page = window.pages["summary"]
    page.show_summary(None)
    text = page.narrative_label.cget("text")
    assert "QQ 客户端读取为实验性" in text
    assert "读取 QQ 当前聊天（实验）" in text
    assert "QQ 前台" in text

    window.apply_first_run_consents([ConsentScope.QQ_READ])
    status = window.status_label.cget("text")
    assert "读取 QQ 当前聊天（实验）" in status
    assert "一次即可" in status
