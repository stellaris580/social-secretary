"""P0-15 截图断言：截图由独立子进程生成（见 test_screenshots/screenshot_renderer）。"""

from __future__ import annotations

from test_screenshots import _assert_valid_image, _ensure_rendered

INBOX_FILES = (
    "inbox-demo.png",
    "inbox-real-detail.png",
    "inbox-empty.png",
    "inbox-connect.png",
    "inbox-summary-fallback.png",
)


def test_capture_inbox_demo_and_real(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "inbox-demo.png")
    _assert_valid_image(evidence_dir / "inbox-real-detail.png")


def test_capture_inbox_empty(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "inbox-empty.png")


def test_capture_inbox_connect_wizard(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "inbox-connect.png")


def test_capture_summary_fallback(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "inbox-summary-fallback.png")


def test_inbox_screenshots_exist(evidence_dir):
    _ensure_rendered(evidence_dir)
    for filename in INBOX_FILES:
        _assert_valid_image(evidence_dir / filename)
