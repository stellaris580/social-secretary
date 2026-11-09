"""截图证据：父测试只负责“调用独立子进程渲染 + 断言产物”，避免原生崩溃。

背景（Lead 定位）：截图需要使用 Tk 窗口 + Windows `PrintWindow`/GDI；如果在 pytest 主进程里、
在大量 Tk 窗口创建之后执行，本机偶发 native 崩溃（exit -1、无 traceback），且单文件跑不会出现。
因此所有截图生成都放到 ``screenshot_renderer.py`` 的独立子进程里，父进程不再创建窗口/截图，
只校验文件存在、尺寸和颜色数（非空白）。

所有数据来自 tests/ui/fake_service.py 的**合成演示数据**，不含任何真实聊天/邮件内容。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]

EXPECTED_FILES = (
    "01-today-summary.png",
    "02-todos.png",
    "03-reply-drafts.png",
    "04-message-sources.png",
    "05-settings-accounts.png",
    "06-settings-privacy.png",
    "07-auth-code-help.png",
    "08-first-run-consent.png",
    "09-foreground-warning.png",
    "10-delete-data-confirm.png",
    "11-external-ai-confirm.png",
    "12-source-detail.png",
    "13-message-detail.png",
    "inbox-demo.png",
    "inbox-real-detail.png",
    "inbox-empty.png",
    "inbox-connect.png",
    "inbox-summary-fallback.png",
    "import-preview.png",
    "import-imported.png",
    "05b-settings-qq-experimental.png",
    "quick-qq-confirm.png",
    "qq-session-picker.png",
    "qq-session-picker-selected.png",
    "message-center-status.png",
    "ai-semantic-summary.png",
    "one-click-organize.png",
    "error-feedback.png",
)

_rendered = False


def _ensure_rendered(evidence_dir: Path) -> None:
    """在独立子进程中生成全部截图（每次 pytest 会话只跑一次）。"""
    global _rendered
    if _rendered:
        return
    renderer = HERE / "screenshot_renderer.py"
    assert renderer.exists(), f"缺少截图渲染脚本：{renderer}"
    env = os.environ.copy()
    paths = [str(REPO_ROOT / "src"), str(HERE)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    last_error = ""
    for attempt in range(3):
        proc = subprocess.run(
            [sys.executable, str(renderer), str(evidence_dir)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        # subprocess.run 返回的 CompletedProcess 已完成（无需也不能再 wait）。
        if proc.returncode == 0:
            # 清理可能残留的临时文件（渲染器使用原子替换，正常不会留下）。
            for stale in evidence_dir.glob("*.png.tmp-*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            _rendered = True
            return
        last_error = (
            "截图子进程失败（这是独立进程，避免主进程原生崩溃）：\n"
            f"exit={proc.returncode}\n"
            f"stdout:\n{proc.stdout[-2000:]}\n"
            f"stderr:\n{proc.stderr[-4000:]}"
        )
        # 常见偶发：窗口第一次渲染为空白；隔一会儿换全新子进程再试。
        time.sleep(1.5)
    raise AssertionError(last_error)


def _assert_valid_image(path: Path) -> None:
    # 父进程已 wait 子进程；这里再加一次短轮询，防止杀毒/文件系统延迟导致
    # 断言先看到旧的 1KB 空白文件（Lead 复跑过的瞬时失败）。
    size = 0
    for _attempt in range(5):
        if path.exists():
            size = path.stat().st_size
            if size > 3_000:
                break
        time.sleep(0.2)
    assert path.exists(), f"缺少截图：{path.name}"
    assert size > 3_000, f"截图文件过小：{path.name}（{size} B）"
    with Image.open(path) as image:
        image.load()
        assert image.width >= 300 and image.height >= 200, f"截图尺寸异常：{path.name} {image.size}"
        colors = image.convert("RGB").getcolors(maxcolors=200_000)
        assert colors is not None and len(colors) >= 30, f"截图疑似空白：{path.name}"


def test_capture_main_window_pages(evidence_dir):
    _ensure_rendered(evidence_dir)
    for filename in EXPECTED_FILES[:7]:
        _assert_valid_image(evidence_dir / filename)


def test_capture_dialogs(evidence_dir):
    _ensure_rendered(evidence_dir)
    for filename in EXPECTED_FILES[7:13]:
        _assert_valid_image(evidence_dir / filename)


def test_import_screenshots(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "import-preview.png")
    _assert_valid_image(evidence_dir / "import-imported.png")


def test_qq_experimental_setting_screenshot(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "05b-settings-qq-experimental.png")


def test_quick_qq_confirm_screenshot(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "quick-qq-confirm.png")


def test_qq_session_picker_screenshots(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "qq-session-picker.png")
    _assert_valid_image(evidence_dir / "qq-session-picker-selected.png")


def test_message_center_status_screenshot(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "message-center-status.png")


def test_ai_semantic_summary_screenshot(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "ai-semantic-summary.png")


def test_one_click_organize_screenshot(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "one-click-organize.png")


def test_error_feedback_screenshot(evidence_dir):
    _ensure_rendered(evidence_dir)
    _assert_valid_image(evidence_dir / "error-feedback.png")


def test_all_expected_screenshots_exist(evidence_dir):
    _ensure_rendered(evidence_dir)
    for filename in EXPECTED_FILES:
        _assert_valid_image(evidence_dir / filename)
