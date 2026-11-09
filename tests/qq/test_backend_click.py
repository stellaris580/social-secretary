"""RealNode.click 的离线单元测试：不启动 QQ、不真实点击。

只验证真实后端节点暴露受控 click（供采集器点击搜索结果/会话列表项）；
具体控件类型与安全边界由 collector 的调用点负责。
"""

from __future__ import annotations

from social_secretary.collectors.qq.backend import RealNode


class _FakeControl:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def Click(self) -> None:  # noqa: N802 - 模拟 uiautomation Control API
        self.calls += 1
        if self.fail:
            raise RuntimeError("click failed")


class _FakeUia:
    class PatternId:
        ValuePattern = 10002


def test_real_node_click_returns_true_and_calls_control() -> None:
    control = _FakeControl()
    node = RealNode(control, _FakeUia())
    assert node.click() is True
    assert control.calls == 1


def test_real_node_click_returns_false_on_exception() -> None:
    node = RealNode(_FakeControl(fail=True), _FakeUia())
    assert node.click() is False
